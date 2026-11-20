"""Tests for guardrails integration."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal

import pytest
from pydantic import BaseModel
from typing_extensions import override

from penguiflow.catalog import build_catalog
from penguiflow.node import Node
from penguiflow.planner import PlannerFinish, PlannerPause, ReactPlanner
from penguiflow.planner.guardrails import (
    AsyncRuleEvaluator,
    GatewayConfig,
    GuardrailAction,
    GuardrailDecision,
    GuardrailEvent,
    GuardrailGateway,
    GuardrailSeverity,
    PauseSpec,
    RedactionSpec,
    RuleCost,
    RuleRegistry,
    SecretRedactionRule,
    StopSpec,
    ToolAllowlistRule,
)
from penguiflow.planner.guardrails.protocols import GuardrailRule
from penguiflow.planner.memory import MemoryKey, ShortTermMemoryConfig
from penguiflow.planner.models import JSONLLMClient
from penguiflow.registry import ModelRegistry
from penguiflow.steering import InMemoryGuardInbox

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from penguiflow.catalog import NodeSpec


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


class _RecordingStubClient(_StubClient):
    """Stub client that also records the messages of every call."""

    def __init__(self, responses: list[dict[str, object]]) -> None:
        super().__init__(responses)
        self.calls: list[list[Mapping[str, Any]]] = []

    @override
    async def complete(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        response_format: Mapping[str, Any] | None = None,
        stream: bool = False,
        on_stream_chunk: Callable[[str, bool], None] | None = None,
    ) -> tuple[str, float]:
        self.calls.append(list(messages))
        return await super().complete(
            messages=messages,
            response_format=response_format,
            stream=stream,
            on_stream_chunk=on_stream_chunk,
        )


class _FixedDecisionRule(GuardrailRule):
    """FAST rule returning one fixed decision for the given events."""

    version = "1"
    enabled = True
    cost = RuleCost.FAST
    severity = GuardrailSeverity.HIGH

    def __init__(
        self,
        rule_id: str,
        event_types: frozenset[str],
        decide: Callable[[GuardrailEvent], GuardrailDecision | None],
    ) -> None:
        self.rule_id = rule_id
        self.supports_event_types = event_types
        self._decide = decide
        self.events: list[str] = []

    @override
    async def evaluate(
        self, event: GuardrailEvent, context_snapshot: object
    ) -> GuardrailDecision | None:
        self.events.append(event.event_type)
        return self._decide(event)


def _gateway(
    rule: GuardrailRule, *, mode: Literal["shadow", "enforce"] = "enforce"
) -> GuardrailGateway:
    guard_registry = RuleRegistry()
    guard_registry.register(rule)
    return GuardrailGateway(
        registry=guard_registry,
        guard_inbox=InMemoryGuardInbox(AsyncRuleEvaluator(guard_registry)),
        config=GatewayConfig(mode=mode),
    )


def _demo_catalog() -> list[NodeSpec]:
    registry = ModelRegistry()
    registry.register("demo", ToolArgs, ToolOut)
    return build_catalog([Node(demo_tool, name="demo")], registry)


def _stop(event: GuardrailEvent) -> GuardrailDecision:
    return GuardrailDecision(
        action=GuardrailAction.STOP,
        rule_id="blocker",
        reason=f"blocked {event.event_type}",
        stop=StopSpec(user_message="Blocked by policy."),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["tool_call_start", "tool_call_result"])
async def test_guardrail_pause_without_pause_support_stops(
    event_type: str,
) -> None:
    """With pause disabled, a guardrail PAUSE ends the run as a STOP."""
    rule = _ApprovalRule(event_type)
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
        catalog=_demo_catalog(),
        max_iters=2,
        pause_enabled=False,
        guardrail_gateway=_gateway(rule),
    )

    result = await planner.run("hi")

    assert isinstance(result, PlannerFinish)
    assert result.reason == "no_path"
    guardrail = result.metadata["guardrail"]
    assert guardrail["action"] == "STOP"
    assert guardrail["requested_action"] == "PAUSE"
    assert guardrail["pause_disabled"] is True
    assert result.payload["raw_answer"] == "Approve the demo call?"


@pytest.mark.asyncio
async def test_llm_before_stop_records_guardrail_metadata() -> None:
    """An ``llm_before`` STOP finishes like a tool STOP, with metadata."""
    client = _RecordingStubClient([])
    rule = _FixedDecisionRule("blocker", frozenset({"llm_before"}), _stop)
    planner = ReactPlanner(
        llm_client=client,
        catalog=_demo_catalog(),
        max_iters=2,
        guardrail_gateway=_gateway(rule),
    )

    result = await planner.run("hi")

    assert isinstance(result, PlannerFinish)
    assert result.reason == "no_path"
    assert result.metadata["guardrail"]["rule_id"] == "blocker"
    assert result.payload["raw_answer"] == "Blocked by policy."
    assert client.calls == []


@pytest.mark.asyncio
async def test_shadow_mode_logs_every_event_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Shadow mode logs the decisions of tool events too, without acting."""
    rule = _FixedDecisionRule(
        "blocker",
        frozenset({"llm_before", "tool_call_start", "tool_call_result"}),
        _stop,
    )
    planner = ReactPlanner(
        llm_client=_StubClient(
            [
                {
                    "thought": "call demo",
                    "next_node": "demo",
                    "args": {"value": "hi"},
                },
                _answer("done"),
            ]
        ),
        catalog=_demo_catalog(),
        max_iters=3,
        guardrail_gateway=_gateway(rule, mode="shadow"),
    )

    with caplog.at_level("INFO", logger="penguiflow.planner"):
        result = await planner.run("hi")

    assert isinstance(result, PlannerFinish)
    assert result.reason == "answer_complete"
    shadowed = [
        getattr(record, "event_type", None)
        for record in caplog.records
        if record.getMessage() == "guardrail_decision_shadow"
    ]
    assert shadowed == [
        "llm_before",
        "tool_call_start",
        "tool_call_result",
        "llm_before",
    ]


def _break_json(event: GuardrailEvent) -> GuardrailDecision | None:
    text = event.text_content or ""
    start = text.find("sk-")
    if start < 0:
        return None
    # Redact the secret and its closing quote, leaving invalid JSON.
    end = text.index('"', start) + 1
    return GuardrailDecision(
        action=GuardrailAction.REDACT,
        rule_id="breaker",
        reason="secret",
        redactions=(
            RedactionSpec(path="result", start_offset=start, end_offset=end),
        ),
    )


@pytest.mark.asyncio
async def test_invalid_json_redaction_never_reaches_the_llm() -> None:
    """A REDACT that breaks the JSON still hides the original result."""
    registry = ModelRegistry()
    registry.register("secret", ToolArgs, ToolOut)
    catalog = build_catalog([Node(secret_tool, name="secret")], registry)
    client = _RecordingStubClient(
        [
            {
                "thought": "call secret",
                "next_node": "secret",
                "args": {"value": "hi"},
            },
            _answer("done"),
        ]
    )
    rule = _FixedDecisionRule(
        "breaker", frozenset({"tool_call_result"}), _break_json
    )
    planner = ReactPlanner(
        llm_client=client,
        catalog=catalog,
        max_iters=3,
        guardrail_gateway=_gateway(rule),
    )

    result = await planner.run("hi")

    assert isinstance(result, PlannerFinish)
    observation = result.metadata["steps"][0]["observation"]
    assert "sk-abc" not in json.dumps(observation)
    assert "[REDACTED]" in json.dumps(observation)
    assert "sk-abc" not in json.dumps(client.calls[-1])
