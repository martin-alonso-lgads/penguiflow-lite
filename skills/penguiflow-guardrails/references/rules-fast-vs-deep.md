# FAST vs DEEP Rules

A rule's `cost` decides where it runs. `RuleRegistry.register(rule)` puts `RuleCost.FAST` rules on the gateway's sync path and `RuleCost.DEEP` rules in the guard inbox. Source: `penguiflow/planner/guardrails/gateway.py`, `routing.py`, `async_eval.py` and `penguiflow/steering/guard_inbox.py`.

## FAST rules

FAST rules run inline on every event they support, before the planner continues.
- Each rule gets its own `asyncio.wait_for(..., sync_timeout_ms / 1000)` (default 15 ms).
- With `sync_parallel=True` (the default) the rules run concurrently, so the cost is roughly the slowest rule. With `False` they run one after another.
- A rule that raises or times out follows `sync_fail_open`. `False` (the default) returns STOP with `GUARDRAIL_SYNC_ERROR` or `GUARDRAIL_SYNC_TIMEOUT`; `True` skips the rule.
- A FAST STOP returns at once: the event is not submitted to the inbox.

Use FAST for set lookups, compiled regexes over short text, and deterministic checks on `tool_args`. Don't use it for network calls, LLM judges, or regexes over very large text.

## DEEP rules

When `async_enabled=True`, every event that gets past the FAST rules is submitted to the inbox, even when no DEEP rule is registered. With `InMemoryGuardInbox(AsyncRuleEvaluator(registry))`, the evaluator runs every DEEP rule registered for that event type concurrently.

The gateway then waits `async_wait_ms`, as chosen by the risk router:
- **The response arrives in time:** its decisions join the FAST ones and are resolved together.
- **The wait times out:** the router's `on_timeout` decides. `"allow"` adds nothing; `"pause"` adds PAUSE (`rule_id="__timeout__"`); `"stop"` adds STOP (`GUARDRAIL_TIMEOUT`). The evaluation keeps running.
- **`async_wait_ms` is 0:** the gateway does not wait at all.

**Late decisions.** A response that completes after the gateway stopped waiting is only kept if it holds a STOP. That STOP is applied at the **same run's** first guardrail check after it arrives, whatever that event's type, and wins over everything else at that check. Late PAUSE, RETRY and REDACT decisions are dropped, because the step they reviewed has already happened. When the run finishes, `end_run()` discards whatever is still pending, so a late STOP never reaches another run.

So a DEEP rule can only block in time when the router waits for it. The default router gives `llm_before` no wait (`llm_before_wait_ms=0.0`) unless high-risk tools or risky text are involved, so a DEEP rule there has its RETRY dropped and its STOP applied at the run's next check. Set `llm_before_wait_ms` to wait for it.

## `DefaultRiskRouter`

`DefaultRiskRouter(high_risk_wait_ms=200.0, medium_risk_wait_ms=100.0, critical_fail_closed=True, llm_before_wait_ms=0.0)` routes by the snapshot:

| Condition | `async_wait_ms` | `on_timeout` |
|---|---|---|
| Event's tool is `CRITICAL` | high (200) | `"stop"` (`"pause"` if `critical_fail_closed=False`) |
| Event's tool is `HIGH` | high (200) | `"pause"` |
| Event's tool is `MEDIUM` (the default tier) | medium (100) | `"allow"` |
| Event's tool is `LOW`, or no tool (`llm_before`, `llm_stream_chunk`) | 0 | `"allow"` |
| Every `llm_before` event | at least `llm_before_wait_ms` (0) | unchanged |
| `llm_before` while any available tool is `HIGH`/`CRITICAL` | at least medium | `"pause"` if it was `"allow"` |
| Text asks for the system prompt or instructions | at least medium | unchanged |
| Text asks to bypass restrictions | at least high | `"pause"` if it was `"allow"` |

`router.configure(...)` overrides any of the four settings and keeps the others; a policy pack sets them under `risk_router` (`high_risk_wait_ms`, `medium_risk_wait_ms`, `critical_fail_closed`, `llm_before_wait_ms`).

For the last two rows the router also sets `required_async_rules={"hf-jailbreak"}`. Required rules don't narrow evaluation: every DEEP rule registered for the event type still runs. A required rule that isn't registered is logged as `guardrail_required_rule_missing` (WARNING, with `event_type` and `rule_ids`).

Tool tiers come from the context builder; unknown tools are `MEDIUM`:

```python
from penguiflow.planner.guardrails import (
    AsyncRuleEvaluator,
    ContextSnapshotBuilder,
    DefaultRiskRouter,
    GuardrailGateway,
    RuleRegistry,
    ToolRiskTier,
)
from penguiflow.steering import InMemoryGuardInbox

registry = RuleRegistry()
gateway = GuardrailGateway(
    registry=registry,
    guard_inbox=InMemoryGuardInbox(AsyncRuleEvaluator(registry)),
    context_builder=ContextSnapshotBuilder(
        tool_risk_registry={"db_write": ToolRiskTier.CRITICAL, "search": ToolRiskTier.LOW}
    ),
    risk_router=DefaultRiskRouter(high_risk_wait_ms=500.0),
)
```

### Wait for a DEEP judge on `llm_before`

`DefaultRiskRouter(llm_before_wait_ms=300.0)` waits at least 300 ms on every `llm_before` event and keeps the rest of the default routing. A custom router covers what the default can't, for example a fixed 300 ms wait that keeps `on_timeout="allow"` even when high-risk tools are available:

```python
from penguiflow.planner.guardrails import (
    ContextSnapshotV1,
    DefaultRiskRouter,
    GuardrailEvent,
    RiskRoutingDecision,
)


class WaitBeforeLlm:
    """Default routing, but a fixed 300 ms wait before each LLM call."""

    def __init__(self) -> None:
        self._default = DefaultRiskRouter()

    def route(
        self, event: GuardrailEvent, context_snapshot: ContextSnapshotV1
    ) -> RiskRoutingDecision:
        decision = self._default.route(event, context_snapshot)
        if event.event_type != "llm_before":
            return decision
        return RiskRoutingDecision(
            async_wait_ms=300.0,
            on_timeout="allow",
            required_async_rules=decision.required_async_rules,
        )
```

Pass it as `GuardrailGateway(..., risk_router=WaitBeforeLlm())`. `RiskRoutingDecision(skip_evaluation=True)` skips every rule for that event.

## A DEEP rule

```python
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from penguiflow.planner.guardrails import (
    ContextSnapshotV1,
    GuardrailAction,
    GuardrailDecision,
    GuardrailEvent,
    GuardrailSeverity,
    RuleCost,
    StopSpec,
)


@dataclass
class LlmJudgeRule:
    judge: Callable[[str], Awaitable[float]]  # returns P(unsafe)
    threshold: float = 0.9
    rule_id: str = "llm-judge"
    version: str = "1.0.0"
    supports_event_types: frozenset[str] = frozenset({"llm_before"})
    cost: RuleCost = RuleCost.DEEP
    enabled: bool = True
    severity: GuardrailSeverity = GuardrailSeverity.HIGH

    async def evaluate(
        self, event: GuardrailEvent, context_snapshot: ContextSnapshotV1
    ) -> GuardrailDecision | None:
        text = event.text_content or ""
        if len(text) < 8:  # cheap pre-filter before the slow call
            return None
        score = await asyncio.wait_for(self.judge(text), timeout=5.0)
        if score < self.threshold:
            return None
        return GuardrailDecision(
            action=GuardrailAction.STOP,
            rule_id=self.rule_id,
            reason=f"Judge score {score:.2f}",
            severity=self.severity,
            confidence=score,
            stop=StopSpec(error_code="UNSAFE_REQUEST"),
        )
```

If a DEEP rule raises (here, a judge timeout), `AsyncRuleEvaluator` raises `AsyncRuleEvaluationError`, exported from `penguiflow.planner.guardrails`. It carries the decisions of the rules that didn't fail in `.decisions` and the exceptions by rule id in `.errors`. `InMemoryGuardInbox` turns it into a response with those decisions plus an `error`, so `async_fail_open` applies (see below). Catch errors inside the rule if a failure needs its own decision.

There is no two-stage rule type. For "cheap check first, deep check when unsure", register a FAST rule and a DEEP rule. A FAST STOP already prevents the DEEP submission, and the DEEP rule can pre-filter cheaply itself, as above.

## `async_fail_open`

This setting covers inbox failures: `submit` or `await_response` raising, or a `SteeringGuardResponse` with `error` set (which `InMemoryGuardInbox` sets when a DEEP rule raises).
- `True` (the default) logs `guardrail_async_submit_failed` or `guardrail_async_error` and keeps the response's other decisions.
- `False` adds a STOP with `rule_id="__async_error__"`, `severity=CRITICAL` and `error_code="GUARDRAIL_ASYNC_ERROR"`.

Timeouts are decided by the router's `on_timeout`, not by this flag.

## `SteeringGuardInbox` contract

`InMemoryGuardInbox` is the only shipped implementation. To evaluate DEEP rules out of process, subclass the ABC and keep this contract:

| Method | Contract |
|---|---|
| `async submit(event: SteeringGuardEvent) -> str` | Start the evaluation and return its `correlation_id` (`event.correlation_id`). Remember `event.run_id`. |
| `async await_response(correlation_id, timeout_s) -> SteeringGuardResponse` | Return the response, or raise `TimeoutError`. A timeout must not cancel or forget the evaluation. |
| `drain_responses(run_id) -> list[SteeringGuardResponse]` | Sync. Return the completed responses of events submitted with that `run_id`, and forget them. Responses that `await_response` already returned may be included; the gateway ignores them. |

`SteeringGuardEvent` carries `event_type`, `run_id`, `payload` (the event payload plus `text_content`, `tool_name` and `tool_args`), `context_snapshot` (as a dict) and `required_rules` (rules the router asks for; not a filter: run every registered DEEP rule for the event type). `SteeringGuardResponse(correlation_id, decisions=[...], error=None)` carries the result; when a rule fails, keep the other decisions and set `error`, so `async_fail_open` applies.

```python
from penguiflow.steering.guard_inbox import (
    SteeringGuardEvent,
    SteeringGuardInbox,
    SteeringGuardResponse,
)


class RemoteGuardInbox(SteeringGuardInbox):
    """Sketch: send events to a moderation service, poll for verdicts."""

    async def submit(self, event: SteeringGuardEvent) -> str:
        ...  # enqueue event; record event.run_id for event.correlation_id
        return event.correlation_id

    async def await_response(
        self, correlation_id: str, timeout_s: float
    ) -> SteeringGuardResponse:
        ...  # wait up to timeout_s; raise TimeoutError but keep the job
        raise TimeoutError

    def drain_responses(self, run_id: str) -> list[SteeringGuardResponse]:
        ...  # pop finished, unreturned responses of run_id
        return []
```

## Operational defaults

- Keep most rules FAST, and keep each one well under `sync_timeout_ms`. Compile regexes at module level.
- Give a DEEP rule that must block a router wait: set `llm_before_wait_ms`, set a tool risk tier, or use a custom router. Otherwise treat it as a late STOP or as monitoring.
- Raise `sync_timeout_ms` only as far as your latency budget allows; every FAST rule pays it on every event.

| Mistake | Symptom | Fix |
|---|---|---|
| Slow FAST rule | Random `GUARDRAIL_SYNC_TIMEOUT` STOPs (fail-closed) | Speed it up or make it DEEP |
| DEEP rule expected to block `llm_before` | RETRY never applies; STOP arrives one step late | `DefaultRiskRouter(llm_before_wait_ms=...)`, or a custom router with `async_wait_ms > 0` for that event |
| DEEP rule raises | `guardrail_async_error` logged (fail-open) or STOP `GUARDRAIL_ASYNC_ERROR` (fail-closed) | Catch errors in the rule and return a decision |
| `async_fail_open=False` with a flaky inbox or rule | Runs stop with `GUARDRAIL_ASYNC_ERROR` | Fix the inbox or rule, or fail open and alert on the log events |
| Risky user text with no `hf-jailbreak` rule | `guardrail_required_rule_missing` warnings | Register a DEEP rule with that id, or ignore them: the other DEEP rules still run |
