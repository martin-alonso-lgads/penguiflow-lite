"""Tests for short term memory."""

from __future__ import annotations

import asyncio
import json
import threading
from typing import TYPE_CHECKING, Any

import pytest

from penguiflow.planner.memory import (
    ConversationTurn,
    DefaultShortTermMemory,
    MemoryBudget,
    MemoryBudgetExceeded,
    MemoryHealth,
    MemoryIsolation,
    MemoryKey,
    ShortTermMemoryConfig,
    default_token_estimator,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Mapping


def test_memory_key_composite() -> None:
    """Memory key composite."""
    key = MemoryKey(tenant_id="t", user_id="u", session_id="s")
    assert key.composite() == "t:u:s"


def test_default_token_estimator_minimum_one() -> None:
    """Default token estimator minimum one."""
    assert default_token_estimator("") == 1


def test_default_token_estimator_heuristic() -> None:
    """Default token estimator heuristic."""
    assert default_token_estimator("abcd") == 2
    assert default_token_estimator("abcdefgh") == 3


def test_memory_budget_validates_non_negative() -> None:
    """Memory budget validates non negative."""
    with pytest.raises(ValueError, match="full_zone_turns must be >= 0"):
        MemoryBudget(full_zone_turns=-1)
    with pytest.raises(ValueError, match="summary_max_tokens must be >= 0"):
        MemoryBudget(summary_max_tokens=-1)
    with pytest.raises(ValueError, match="total_max_tokens must be >= 0"):
        MemoryBudget(total_max_tokens=-1)


def test_memory_budget_validates_summary_within_total() -> None:
    """Memory budget validates summary within total."""
    with pytest.raises(
        ValueError, match="summary_max_tokens must be <= total_max_tokens"
    ):
        MemoryBudget(summary_max_tokens=11, total_max_tokens=10)


def test_short_term_memory_config_validates_strategy() -> None:
    """Short term memory config validates strategy."""
    with pytest.raises(ValueError, match="strategy must be one of"):
        # The strategy arrives untyped, as when read from JSON config.
        ShortTermMemoryConfig(**json.loads('{"strategy": "wat"}'))


def test_memory_isolation_defaults_fail_closed() -> None:
    """Memory isolation defaults fail closed."""
    iso = MemoryIsolation()
    assert iso.require_explicit_key is True
    assert iso.tenant_key == "tenant_id"
    assert iso.user_key == "user_id"
    assert iso.session_key == "session_id"


def test_memory_isolation_requires_non_empty_paths() -> None:
    """Memory isolation requires non empty paths."""
    with pytest.raises(
        ValueError, match="tenant_key must be a non-empty string"
    ):
        MemoryIsolation(tenant_key="")
    with pytest.raises(
        ValueError, match="user_key must be a non-empty string"
    ):
        MemoryIsolation(user_key="")
    with pytest.raises(
        ValueError, match="session_key must be a non-empty string"
    ):
        MemoryIsolation(session_key="")


async def test_truncation_keeps_last_n_turns() -> None:
    """Truncation keeps last n turns."""
    memory = DefaultShortTermMemory(
        config=ShortTermMemoryConfig(
            strategy="truncation",
            budget=MemoryBudget(full_zone_turns=2),
        )
    )
    await memory.add_turn(
        ConversationTurn(user_message="u1", assistant_response="a1")
    )
    await memory.add_turn(
        ConversationTurn(user_message="u2", assistant_response="a2")
    )
    await memory.add_turn(
        ConversationTurn(user_message="u3", assistant_response="a3")
    )

    ctx = await memory.get_llm_context()
    recent = ctx["conversation_memory"]["recent_turns"]
    assert [t["user"] for t in recent] == ["u2", "u3"]


async def test_rolling_summary_moves_oldest_to_pending_without_blocking() -> (
    None
):
    """Rolling summary moves oldest to pending without blocking."""
    gate = asyncio.Event()

    async def summarizer(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        await gate.wait()
        assert "turns" in payload
        return {"summary": "<session_summary>ok</session_summary>"}

    memory = DefaultShortTermMemory(
        config=ShortTermMemoryConfig(
            strategy="rolling_summary",
            budget=MemoryBudget(full_zone_turns=2),
            retry_attempts=0,
            retry_backoff_base_s=0.0,
        ),
        summarizer=summarizer,
    )

    await asyncio.wait_for(
        memory.add_turn(
            ConversationTurn(user_message="u1", assistant_response="a1")
        ),
        timeout=0.2,
    )
    await asyncio.wait_for(
        memory.add_turn(
            ConversationTurn(user_message="u2", assistant_response="a2")
        ),
        timeout=0.2,
    )
    await asyncio.wait_for(
        memory.add_turn(
            ConversationTurn(user_message="u3", assistant_response="a3")
        ),
        timeout=0.2,
    )

    ctx = await memory.get_llm_context()
    mem = ctx["conversation_memory"]
    assert [t["user"] for t in mem["recent_turns"]] == ["u2", "u3"]
    assert [t["user"] for t in mem["pending_turns"]] == ["u1"]

    gate.set()
    await memory.flush()

    ctx2 = await memory.get_llm_context()
    mem2 = ctx2["conversation_memory"]
    assert "pending_turns" not in mem2
    assert mem2["summary"] == "<session_summary>ok</session_summary>"


async def test_summarizer_failure_degrades_and_falls_back_to_truncation() -> (
    None
):
    """Summarizer failure degrades and falls back to truncation."""

    async def failing_summarizer(
        _payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        msg = "boom"
        raise RuntimeError(msg)

    memory = DefaultShortTermMemory(
        config=ShortTermMemoryConfig(
            strategy="rolling_summary",
            budget=MemoryBudget(full_zone_turns=2),
            retry_attempts=0,
            retry_backoff_base_s=0.0,
            degraded_retry_interval_s=0.0,
        ),
        summarizer=failing_summarizer,
    )

    await memory.add_turn(
        ConversationTurn(user_message="u1", assistant_response="a1")
    )
    await memory.add_turn(
        ConversationTurn(user_message="u2", assistant_response="a2")
    )
    await memory.add_turn(
        ConversationTurn(user_message="u3", assistant_response="a3")
    )
    await memory.flush()

    assert memory.health == MemoryHealth.DEGRADED
    ctx = await memory.get_llm_context()
    mem = ctx["conversation_memory"]
    assert "summary" not in mem
    assert "pending_turns" not in mem
    assert [t["user"] for t in mem["recent_turns"]] == ["u2", "u3"]


async def test_overflow_policy_error_raises() -> None:
    """Overflow policy error raises."""
    memory = DefaultShortTermMemory(
        config=ShortTermMemoryConfig(
            strategy="truncation",
            budget=MemoryBudget(
                full_zone_turns=10,
                summary_max_tokens=0,
                total_max_tokens=1,
                overflow_policy="error",
            ),
        )
    )

    with pytest.raises(MemoryBudgetExceeded):
        await memory.add_turn(
            ConversationTurn(
                user_message="x" * 1000, assistant_response="y" * 1000
            )
        )


async def test_persist_hydrate_roundtrip_duck_typed_store() -> None:
    """Persist hydrate roundtrip duck typed store."""

    class Store:
        def __init__(self) -> None:
            self._db: dict[str, dict[str, Any]] = {}

        async def save_memory_state(
            self, key: str, state: dict[str, Any]
        ) -> None:
            self._db[key] = state

        async def load_memory_state(self, key: str) -> dict[str, Any] | None:
            return self._db.get(key)

    store = Store()
    key = MemoryKey(tenant_id="t", user_id="u", session_id="s").composite()

    memory = DefaultShortTermMemory(
        config=ShortTermMemoryConfig(
            strategy="truncation",
            budget=MemoryBudget(full_zone_turns=2),
        )
    )
    await memory.add_turn(
        ConversationTurn(user_message="u1", assistant_response="a1")
    )
    await memory.persist(store, key)

    restored = DefaultShortTermMemory(
        config=ShortTermMemoryConfig(
            strategy="truncation",
            budget=MemoryBudget(full_zone_turns=2),
        )
    )
    await restored.hydrate(store, key)

    ctx = await restored.get_llm_context()
    assert [t["user"] for t in ctx["conversation_memory"]["recent_turns"]] == [
        "u1"
    ]


async def test_persist_hydrate_noop_when_store_missing_methods() -> None:
    """Persist hydrate noop when store missing methods."""

    class Store:
        pass

    memory = DefaultShortTermMemory(
        config=ShortTermMemoryConfig(strategy="truncation")
    )
    await memory.add_turn(
        ConversationTurn(user_message="u1", assistant_response="a1")
    )
    await memory.persist(Store(), "k")

    other = DefaultShortTermMemory(
        config=ShortTermMemoryConfig(strategy="truncation")
    )
    await other.hydrate(Store(), "k")
    ctx = await other.get_llm_context()
    assert ctx["conversation_memory"]["recent_turns"] == []


def _run_with_deadline(
    coro_factory: Callable[[], Coroutine[Any, Any, None]], seconds: float
) -> None:
    """Run a coroutine in a thread; fail if it blocks past ``seconds``."""
    errors: list[BaseException] = []

    def _target() -> None:
        try:
            asyncio.run(coro_factory())
        except BaseException as exc:  # Re-raised in the test thread.
            errors.append(exc)

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), "memory budget enforcement never returned"
    if errors:
        raise errors[0]


def test_truncate_summary_makes_progress_when_summary_fits() -> None:
    """A summary within its own budget never stalls the total budget."""

    async def keep_summary(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"summary": payload["previous_summary"]}

    async def scenario() -> None:
        memory = DefaultShortTermMemory(
            config=ShortTermMemoryConfig(
                strategy="rolling_summary",
                budget=MemoryBudget(
                    full_zone_turns=10,
                    summary_max_tokens=200,
                    total_max_tokens=200,
                    overflow_policy="truncate_summary",
                ),
            ),
            summarizer=keep_summary,
        )
        state = memory.to_dict()
        state["summary"] = "s" * 100
        memory.from_dict(state)
        await memory.add_turn(
            ConversationTurn(
                user_message="x" * 400, assistant_response="y" * 400
            )
        )
        assert memory.estimate_tokens() <= 200

    _run_with_deadline(scenario, seconds=5)


async def test_rolling_summary_error_policy_still_summarizes() -> None:
    """With ``overflow_policy="error"`` pending turns still get summarized."""
    calls: list[int] = []

    async def summarizer(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        calls.append(len(payload["turns"]))
        return {"summary": "<session_summary>ok</session_summary>"}

    memory = DefaultShortTermMemory(
        config=ShortTermMemoryConfig(
            strategy="rolling_summary",
            budget=MemoryBudget(
                full_zone_turns=1,
                summary_max_tokens=20,
                total_max_tokens=100,
                overflow_policy="error",
            ),
            retry_attempts=0,
            retry_backoff_base_s=0.0,
        ),
        summarizer=summarizer,
    )
    for index in range(3):
        await memory.add_turn(
            ConversationTurn(
                user_message=f"u{index} " + "x" * 200,
                assistant_response="a",
            )
        )
        await memory.flush()

    ctx = await memory.get_llm_context()
    assert calls == [1, 1]
    assert ctx["conversation_memory"]["summary"].startswith(
        "<session_summary>"
    )
    assert "pending_turns" not in ctx["conversation_memory"]


async def test_error_policy_raise_leaves_memory_unchanged() -> None:
    """A turn rejected by the budget is not kept in memory."""
    memory = DefaultShortTermMemory(
        config=ShortTermMemoryConfig(
            strategy="truncation",
            budget=MemoryBudget(
                full_zone_turns=10,
                summary_max_tokens=0,
                total_max_tokens=50,
                overflow_policy="error",
            ),
        )
    )
    await memory.add_turn(
        ConversationTurn(user_message="u1", assistant_response="a1")
    )
    before = memory.to_dict()

    with pytest.raises(MemoryBudgetExceeded):
        await memory.add_turn(
            ConversationTurn(
                user_message="x" * 1000, assistant_response="y" * 1000
            )
        )

    assert memory.to_dict() == before
