"""Tests for guardrails integration."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import BaseModel
from typing_extensions import override

from penguiflow.catalog import build_catalog
from penguiflow.node import Node
from penguiflow.planner import PlannerFinish, PlannerPause, ReactPlanner
from penguiflow.planner.guardrails import (
    AsyncRuleEvaluator,
    GuardrailAction,
    GuardrailDecision,
    GuardrailEvent,
    GuardrailGateway,
    GuardrailSeverity,
    PauseSpec,
    RuleCost,
    RuleRegistry,
    SecretRedactionRule,
    ToolAllowlistRule,
)
from penguiflow.planner.guardrails.protocols import GuardrailRule
from penguiflow.planner.memory import MemoryKey, ShortTermMemoryConfig
from penguiflow.planner.models import JSONLLMClient
from penguiflow.registry import ModelRegistry
from penguiflow.steering import InMemoryGuardInbox

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence


class _StubClient(JSONLLMClient):
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self._responses = [json.dumps(item) for item in responses]

    @override
    async def complete(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        response_format: Mapping[str, Any] | None = None,
        stream: bool = False,
        on_stream_chunk: Callable[[str, bool], None] | None = None,
    ) -> tuple[str, float]:
        if not self._responses:
            msg = "No stub responses left"
            raise AssertionError(msg)
        return self._responses.pop(0), 0.0


class ToolArgs(BaseModel):
    """Arguments of the demo tools."""

    value: str


class ToolOut(BaseModel):
    """Output of the demo tools."""

    result: str


async def demo_tool(args: ToolArgs, _ctx: object) -> ToolOut:
    """Echo the input as the output."""
    return ToolOut(result=f"echo:{args.value}")


async def secret_tool(_args: ToolArgs, _ctx: object) -> ToolOut:
    """Return a payload that holds a secret."""
    return ToolOut(result="sk-abc12345678901234567890")


class _ConversationHistoryRule(GuardrailRule):
    rule_id = "history"
    version = "1"
    supports_event_types = frozenset({"llm_before"})
    enabled = True
    cost = RuleCost.FAST
    severity = GuardrailSeverity.LOW

    def __init__(self, *, expected_user: str) -> None:
        self._expected_user = expected_user

    @override
    async def evaluate(
        self, event: GuardrailEvent, context_snapshot: object
    ) -> GuardrailDecision | None:
        history = event.payload.get("conversation_history")
        assert isinstance(history, list)
        assert history, (
            "expected conversation_history to be populated from "
            "short-term memory"
        )
        assert history[-1]["user"] == self._expected_user
        return GuardrailDecision(
            action=GuardrailAction.ALLOW, rule_id=self.rule_id, reason="ok"
        )


@pytest.mark.asyncio
async def test_guardrail_payload_has_conversation_history() -> None:
    """Guardrail payload includes conversation history from memory."""
    registry = ModelRegistry()
    catalog = build_catalog([], registry)

    guard_registry = RuleRegistry()
    guard_registry.register(_ConversationHistoryRule(expected_user="first"))
    gateway = GuardrailGateway(
        registry=guard_registry,
        guard_inbox=InMemoryGuardInbox(AsyncRuleEvaluator(guard_registry)),
    )

    planner = ReactPlanner(
        llm_client=_StubClient(
            [
                {
                    "thought": "answer",
                    "next_node": "final_response",
                    "args": {"answer": "one"},
                },
                {
                    "thought": "answer",
                    "next_node": "final_response",
                    "args": {"answer": "two"},
                },
            ]
        ),
        catalog=catalog,
        max_iters=1,
        short_term_memory=ShortTermMemoryConfig(strategy="truncation"),
        guardrail_gateway=gateway,
        guardrail_conversation_history_turns=2,
    )

    key = MemoryKey(tenant_id="t", user_id="u", session_id="s")
    await planner.run("first", memory_key=key)
    await planner.run("second", memory_key=key)


@pytest.mark.asyncio
async def test_guardrail_tool_allowlist_stop() -> None:
    """Guardrail tool allowlist stop."""
    registry = ModelRegistry()
    registry.register("demo", ToolArgs, ToolOut)
    catalog = build_catalog([Node(demo_tool, name="demo")], registry)

    guard_registry = RuleRegistry()
    guard_registry.register(
        ToolAllowlistRule(denied_tools=frozenset({"demo"}))
    )
    gateway = GuardrailGateway(
        registry=guard_registry,
        guard_inbox=InMemoryGuardInbox(AsyncRuleEvaluator(guard_registry)),
    )

    planner = ReactPlanner(
        llm_client=_StubClient(
            [
                {
                    "thought": "call demo",
                    "next_node": "demo",
                    "args": {"value": "hi"},
                }
            ]
        ),
        catalog=catalog,
        max_iters=1,
        guardrail_gateway=gateway,
    )

    result = await planner.run("hi")

    assert result.reason == "no_path"
    assert "guardrail" in result.metadata
    assert result.metadata["guardrail"]["rule_id"] == "tool-allowlist"
    assert "unable" in str(result.payload.get("raw_answer")).lower()


@pytest.mark.asyncio
async def test_guardrail_secret_redaction_on_tool_result() -> None:
    """Guardrail secret redaction on tool result."""
    registry = ModelRegistry()
    registry.register("secret", ToolArgs, ToolOut)
    catalog = build_catalog([Node(secret_tool, name="secret")], registry)

    guard_registry = RuleRegistry()
    guard_registry.register(SecretRedactionRule())
    gateway = GuardrailGateway(
        registry=guard_registry,
        guard_inbox=InMemoryGuardInbox(AsyncRuleEvaluator(guard_registry)),
    )

    planner = ReactPlanner(
        llm_client=_StubClient(
            [
                {
                    "thought": "call secret",
                    "next_node": "secret",
                    "args": {"value": "hi"},
                }
            ]
        ),
        catalog=catalog,
        max_iters=1,
        guardrail_gateway=gateway,
    )

    result = await planner.run("hi")
    assert isinstance(result, PlannerFinish)
    steps = result.metadata.get("steps", [])
    assert steps
    observation = steps[0].get("observation") or {}
    assert "OPENAI_KEY" in str(observation.get("result"))


class _ApprovalRule(GuardrailRule):
    rule_id = "approval"
    version = "1"
    enabled = True
    cost = RuleCost.FAST
    severity = GuardrailSeverity.HIGH

    def __init__(self, event_type: str) -> None:
        self.supports_event_types = frozenset({event_type})
        self.events: list[str] = []

    @override
    async def evaluate(
        self, event: GuardrailEvent, context_snapshot: object
    ) -> GuardrailDecision | None:
        self.events.append(event.event_type)
        return GuardrailDecision(
            action=GuardrailAction.PAUSE,
            rule_id=self.rule_id,
            reason="needs approval",
            pause=PauseSpec(
                approver_roles=("finance", "admin"),
                prompt="Approve the demo call?",
                timeout_s=60.0,
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["tool_call_start", "tool_call_result"])
async def test_guardrail_pause_payload_carries_pause_spec(
    event_type: str,
) -> None:
    """A PAUSE decision pauses the run and carries its PauseSpec."""
    registry = ModelRegistry()
    registry.register("demo", ToolArgs, ToolOut)
    catalog = build_catalog([Node(demo_tool, name="demo")], registry)

    rule = _ApprovalRule(event_type)
    guard_registry = RuleRegistry()
    guard_registry.register(rule)
    gateway = GuardrailGateway(
        registry=guard_registry,
        guard_inbox=InMemoryGuardInbox(AsyncRuleEvaluator(guard_registry)),
    )
    planner = ReactPlanner(
        llm_client=_StubClient(
            [
                {
                    "thought": "call demo",
                    "next_node": "demo",
                    "args": {"value": "hi"},
                }
            ]
        ),
        catalog=catalog,
        max_iters=2,
        guardrail_gateway=gateway,
    )

    result = await planner.run("hi")

    assert isinstance(result, PlannerPause)
    assert rule.events == [event_type]
    guardrail = result.payload["guardrail"]
    assert guardrail["action"] == "PAUSE"
    assert guardrail["pause"] == {
        "scope": "tool_call",
        "approver_roles": ["finance", "admin"],
        "prompt": "Approve the demo call?",
        "timeout_s": 60.0,
    }


class _LateDangerRule(GuardrailRule):
    rule_id = "late-danger"
    version = "1"
    supports_event_types = frozenset({"llm_before"})
    enabled = True
    cost = RuleCost.DEEP
    severity = GuardrailSeverity.HIGH

    def __init__(self, delay_s: float) -> None:
        self.delay_s = delay_s
        self.run_ids: list[str] = []

    @override
    async def evaluate(
        self, event: GuardrailEvent, context_snapshot: object
    ) -> GuardrailDecision | None:
        self.run_ids.append(event.run_id)
        await asyncio.sleep(self.delay_s)
        if "danger" not in (event.text_content or ""):
            return None
        return GuardrailDecision(
            action=GuardrailAction.STOP,
            rule_id=self.rule_id,
            reason="late danger",
        )


async def slow_tool(args: ToolArgs, _ctx: object) -> ToolOut:
    """Take long enough for a pending DEEP rule to finish."""
    await asyncio.sleep(0.05)
    return ToolOut(result=f"slow:{args.value}")


def _late_danger_planner(
    rule: _LateDangerRule, responses: list[dict[str, object]]
) -> ReactPlanner:
    registry = ModelRegistry()
    registry.register("slow", ToolArgs, ToolOut)
    catalog = build_catalog([Node(slow_tool, name="slow")], registry)
    guard_registry = RuleRegistry()
    guard_registry.register(rule)
    gateway = GuardrailGateway(
        registry=guard_registry,
        guard_inbox=InMemoryGuardInbox(AsyncRuleEvaluator(guard_registry)),
    )
    return ReactPlanner(
        llm_client=_StubClient(responses),
        catalog=catalog,
        max_iters=3,
        guardrail_gateway=gateway,
    )


_CALL_SLOW: dict[str, object] = {
    "thought": "call",
    "next_node": "slow",
    "args": {"value": "x"},
}


def _answer(text: str) -> dict[str, object]:
    return {
        "thought": "answer",
        "next_node": "final_response",
        "args": {"answer": text},
    }


@pytest.mark.asyncio
async def test_late_deep_stop_ends_the_run_that_triggered_it() -> None:
    """A DEEP STOP that arrives after the wait stops the same run."""
    rule = _LateDangerRule(delay_s=0.0)
    planner = _late_danger_planner(rule, [_CALL_SLOW, _answer("done")])

    result = await planner.run("danger ahead")

    assert isinstance(result, PlannerFinish)
    assert result.metadata["guardrail"]["rule_id"] == "late-danger"


@pytest.mark.asyncio
async def test_late_deep_stop_never_reaches_the_next_run() -> None:
    """Each run gets its own guardrail run id; late STOPs don't cross."""
    rule = _LateDangerRule(delay_s=0.05)
    planner = _late_danger_planner(
        rule, [_answer("first"), _CALL_SLOW, _answer("second")]
    )

    first = await planner.run("danger ahead")
    await asyncio.sleep(0.1)
    second = await planner.run("all safe")

    assert isinstance(first, PlannerFinish)
    assert isinstance(second, PlannerFinish)
    assert second.reason == "answer_complete"
    assert "guardrail" not in second.metadata
    assert len(set(rule.run_ids)) == 2
