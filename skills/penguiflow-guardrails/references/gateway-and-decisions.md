# `GuardrailGateway` and Decisions

Source: `penguiflow/planner/guardrails/` (`gateway.py`, `models.py`, `context.py`, `protocols.py`, `rules.py`) and the planner hooks in `penguiflow/planner/react_step.py`, `react_step_native.py` and `tool_calls.py`.

## Wiring

```python
from penguiflow.planner import ReactPlanner
from penguiflow.planner.guardrails import (
    AsyncRuleEvaluator,
    GatewayConfig,
    GuardrailGateway,
    InjectionPatternRule,
    RuleRegistry,
    SecretRedactionRule,
    ToolAllowlistRule,
)
from penguiflow.steering import InMemoryGuardInbox

registry = RuleRegistry()
registry.register(InjectionPatternRule())
registry.register(ToolAllowlistRule(denied_tools=frozenset({"shell"})))
registry.register(SecretRedactionRule())

gateway = GuardrailGateway(
    registry=registry,
    guard_inbox=InMemoryGuardInbox(AsyncRuleEvaluator(registry)),
    config=GatewayConfig(mode="shadow"),
)
planner = ReactPlanner(..., guardrail_gateway=gateway)
```

- `RuleRegistry.register(rule)` files the rule by `rule.cost`: FAST rules run in the gateway, DEEP rules in the inbox. `register_sync(rule)` forces a rule onto the sync path.
- `SteeringGuardInbox` is an ABC. `InMemoryGuardInbox(AsyncRuleEvaluator(registry))` is the only shipped implementation; pass it the same registry.
- Other `GuardrailGateway` fields: `decision_policy` (default `DefaultDecisionPolicy()`), `risk_router` (default `DefaultRiskRouter()`, see `rules-fast-vs-deep.md`), and `context_builder` (default `ContextSnapshotBuilder()`, which holds the tool risk tiers).
- `guardrail_conversation_history_turns=N` on `ReactPlanner` (default `1`) copies the last N short-term-memory turns into `event.payload["conversation_history"]` as `[{"user": ..., "assistant": ...}]`. It is empty without `short_term_memory`.
- Each `run()` gets its own guardrail run id, stored in `trajectory.metadata["guardrail_run_id"]`. `resume()` keeps it. When a run finishes (not when it pauses), the planner calls `gateway.end_run(run_id, ctx)` with its `GuardrailContext`, which discards that run's late async responses and its strikes.

## `GatewayConfig`

A dataclass; the settings apply to the whole gateway, not per rule.

| Field | Default | Meaning |
|---|---|---|
| `mode` | `"enforce"` | `"enforce"` applies decisions. `"shadow"` evaluates and applies nothing; see "Observability" for what gets logged. |
| `sync_timeout_ms` | `15.0` | Time limit for each FAST rule. |
| `sync_parallel` | `True` | Run the FAST rules concurrently (`asyncio.gather`). |
| `async_enabled` | `True` | Submit each evaluated event to the inbox for DEEP rules. |
| `sync_fail_open` | `False` | A FAST rule that raises or times out: `True` skips it, `False` returns STOP (`GUARDRAIL_SYNC_ERROR` or `GUARDRAIL_SYNC_TIMEOUT`). |
| `async_fail_open` | `True` | An inbox failure (`submit` or `await_response` raises, or a response has `error` set, as `InMemoryGuardInbox` does when a DEEP rule raises): `True` logs `guardrail_async_submit_failed` or `guardrail_async_error` and keeps the response's other decisions; `False` adds a STOP with `rule_id="__async_error__"` and `error_code="GUARDRAIL_ASYNC_ERROR"`. |

An async **timeout** is not an inbox failure: the risk router's `on_timeout` decides it.

## `GuardrailEvent`

```python
from penguiflow.planner.guardrails import GuardrailEvent

GuardrailEvent(
    event_type="tool_call_start",
    run_id="...",        # the run's guardrail run id
    text_content=None,
    tool_name="db_write",
    tool_args={"target_db": "prod"},
    payload={},          # see below
)
```

| `event_type` | When | `text_content` | Extra `payload` keys |
|---|---|---|---|
| `llm_before` | Before each LLM call of a step | The prompt's last user-role message: `{"query": ...}` on the first step, then the latest observation, steering input or `Resume input: ...` | — |
| `tool_call_start` | Before a tool runs | `None` | `tool_call_id`, `action_seq` |
| `tool_call_result` | After a tool succeeds | The result JSON the LLM will see, after the observation clamp | `tool_call_id`, `action_seq` |
| `llm_stream_chunk` | Each streamed LLM chunk | The chunk text | `phase`, `channel` |

Every event also carries `conversation_history`, plus `last_assistant` and `task_scope` when available. `llm_stream_chunk` is evaluated in both modes, with an `event_callback` set and streaming on (`stream_final_response=True` and a streaming-capable client). In `shadow` mode the chunk is emitted unchanged.

In `tool_call_mode="native"` the events are the same: `llm_before` runs before each LLM call, and streamed chunks are checked before the step returns, so a chunk STOP ends the run.

## Context snapshot and `GuardrailContext`

Rules receive a `ContextSnapshotV1` built by the gateway's `ContextSnapshotBuilder`: `user_text`, `primary_source` (`TOOL_OUTPUT` for `tool_call_result`, else `USER`), `available_tools`, `current_tool` (with its `ToolRiskTier`), `max_tool_risk`, `requests_system_info`, `requests_capability_change` and `previous_violations`.

Tools default to `ToolRiskTier.MEDIUM`. Set tiers with `ContextSnapshotBuilder(tool_risk_registry={...})` or `gateway.context_builder.set_tool_risks({...})`.

The planner keeps one `GuardrailContext` per planner instance and counts strikes per run in `strikes_by_run` (`{run_id: {category: count}}`), through `add_strike(run_id, category)`, `strike_total(run_id)` and `clear_run(run_id)`. `previous_violations` is `strike_total(event.run_id)`: only the current run's strikes count. They survive pause and resume, and `end_run` clears them when the run finishes.

## `GuardrailDecision`

```python
from penguiflow.planner.guardrails import (
    GuardrailAction,
    GuardrailDecision,
    GuardrailSeverity,
    StopSpec,
)

decision = GuardrailDecision(
    action=GuardrailAction.STOP,   # ALLOW | REDACT | RETRY | PAUSE | STOP
    rule_id="tool-denylist",       # required, stable id for audit
    reason="Tool 'shell' is denied",  # required
    severity=GuardrailSeverity.CRITICAL,
    confidence=1.0,
    effects=("increment_strike",),
    stop=StopSpec(error_code="TOOL_DENIED", user_message="I can't run that."),
)
```

Other fields: `decision_id` (auto), `correlation_id`, `was_sync` (the gateway sets `False` on DEEP results), `redactions`, `retry`, `pause` and `classifier_result`. There is no `metadata` field.

| Spec | Fields (defaults) | Notes |
|---|---|---|
| `StopSpec` | `error_code="GUARDRAIL_STOP"`, `user_message="I'm unable to complete that request."`, `internal_reason=""` | `user_message` becomes the answer. |
| `PauseSpec` | `scope="tool_call"` (`run`/`step`/`tool_call`), `approver_roles=("admin",)`, `prompt=""`, `timeout_s=300.0` | Copied into the pause payload. The planner does not enforce `scope` or `timeout_s`. |
| `RetrySpec` | `max_attempts=2`, `corrective_message=""` | Only honoured at `llm_before`. |
| `RedactionSpec` | `path` (required), `replacement="[REDACTED]"`, `entity_type`, `start_offset`, `end_offset` | Applied by offsets into `text_content`. `path` is a label; a spec without both offsets is ignored. |

Effects: only `increment_strike` does something (it adds a strike for the run under `rule_id.split("-")[0]`). `flag_trajectory` and `emit_alert` are accepted but are no-ops.

## What each action does

In `enforce` mode:

| Event | STOP | PAUSE | RETRY | REDACT |
|---|---|---|---|---|
| `llm_before` | Run finishes `reason="no_path"`; the answer is `stop.user_message` | Ignored | Inserts `corrective_message` as a system message into this LLM call and emits `guardrail_retry`, up to `max_attempts` times per run | Ignored |
| `tool_call_start` | Tool skipped; run finishes `reason="no_path"` | Run pauses, tool not run | Ignored | Ignored |
| `tool_call_result` | Run finishes `no_path` (the tool already ran) | Run pauses | Ignored | Result JSON redacted before the LLM sees it |
| `llm_stream_chunk` | Chunk dropped; after the LLM call the run finishes `no_path` with `stop.user_message` | Chunk dropped | Ignored | Chunk text redacted |

- **STOP:** every STOP, LLM-side (`llm_before`, a stream chunk, a late DEEP STOP landing there) or tool-side, finishes the same way: `reason="no_path"`, `result.payload["raw_answer"]` is `stop.user_message`, `result.metadata["guardrail"]` holds the decision and `result.metadata["error"]` is `guardrail_stop:<rule_id>`. A tool STOP's `tool_call_result` event `result_json` carries `{"error": ..., "failure": {"guardrail": ...}}`. In a parallel plan, a tool STOP only fails that branch.
- **PAUSE:** `run()` returns `PlannerPause(reason="approval_required")` with `payload["guardrail"] = {"action": "PAUSE", "rule_id", "reason", "decision_id", "severity", "effects", "stop": None, "pause": {"scope", "approver_roles", "prompt", "timeout_s"}}`. No `tool_call_result` event is emitted.
- **PAUSE without pause support:** on a planner built with `pause_enabled=False`, a PAUSE at `tool_call_start` or `tool_call_result` (including the router's `on_timeout="pause"` PAUSE) becomes a STOP. The run finishes `no_path`, `raw_answer` is `pause.prompt` (or a default message), and `metadata["guardrail"]` holds the decision with `action="STOP"`, `requested_action="PAUSE"`, `pause_disabled=True` and `stop.error_code="GUARDRAIL_PAUSE_DISABLED"`.
- **REDACT on `tool_call_result`:** if the redacted text is no longer valid JSON, the observation the LLM sees (and the step's observation) becomes `{"result": "<redacted text>"}`; the original is never passed on. Keep replacements inside JSON string values, and free of quotes and backslashes, to keep the structure. Trajectory `artifacts` and `sources` are collected after the guardrail, from the redacted observation.

## Decision resolution

`GuardrailGateway.evaluate` works in this order:
1. A late DEEP STOP of this run, if any, wins outright (see `rules-fast-vs-deep.md`).
2. If the router sets `skip_evaluation`, the result is ALLOW (`rule_id="__skip__"`).
3. FAST rules run. Any FAST STOP returns at once, and the event is not sent to the inbox.
4. The event goes to the inbox, and DEEP decisions are collected within the router's wait.
5. If no rule returned a decision, the result is ALLOW (`rule_id="__default__"`); otherwise `decision_policy.resolve(...)` picks one.

`DefaultDecisionPolicy` sorts by priority `STOP=5, PAUSE=4, RETRY=3, REDACT=2, ALLOW=1`, then by `confidence`. If the winner is REDACT, it merges every decision's `redactions`; it also unions all `effects`. `severity` does not affect ordering.

## Built-in rules

All are FAST dataclasses with public fields:

| Rule | Events | Action |
|---|---|---|
| `InjectionPatternRule(patterns=...)` | `llm_before` | STOP `JAILBREAK_<INTENT>` on a regex match; effects `flag_trajectory`, `increment_strike` |
| `ToolAllowlistRule(denied_tools=..., allowed_tools=...)` | `tool_call_start` | STOP `TOOL_DENIED` or `TOOL_NOT_ALLOWED` |
| `SecretRedactionRule(patterns=...)` | `tool_call_result`, `llm_stream_chunk` | REDACT secret-shaped tokens by offset |

`load_policy_pack(path, env=...)` and `apply_policy_config(registry, gateway, gateway.context_builder, gateway.risk_router, pack)` set gateway, rule, tool-risk and router settings from YAML; see `docs/architecture/planning_orchestration/guardrails_policy_packs.md`.

## Authoring a rule

A rule satisfies the `GuardrailRule` protocol (`penguiflow/planner/guardrails/protocols.py`). It has the attributes `rule_id`, `version`, `supports_event_types`, `cost`, `enabled` and `severity`, plus `async evaluate(event, context_snapshot)`. Return `None` when the rule does not apply. The registry only offers a rule events listed in `supports_event_types`, and only while `enabled` is true.

A rule with a wrong `evaluate` signature raises on every call. With `sync_fail_open=False`, that becomes a STOP on every event.

### Tool denylist (STOP)

```python
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
class ToolDenylistRule:
    rule_id: str = "tool-denylist"
    version: str = "1.0.0"
    supports_event_types: frozenset[str] = frozenset({"tool_call_start"})
    cost: RuleCost = RuleCost.FAST
    enabled: bool = True
    severity: GuardrailSeverity = GuardrailSeverity.CRITICAL
    denied: frozenset[str] = frozenset({"shell", "delete_all"})

    async def evaluate(
        self, event: GuardrailEvent, context_snapshot: ContextSnapshotV1
    ) -> GuardrailDecision | None:
        if event.tool_name not in self.denied:
            return None
        return GuardrailDecision(
            action=GuardrailAction.STOP,
            rule_id=self.rule_id,
            reason=f"Tool {event.tool_name} is denied",
            severity=self.severity,
            stop=StopSpec(
                error_code="TOOL_DENIED",
                user_message="I can't run that operation.",
            ),
        )
```

### SSN redaction (REDACT by offset)

```python
import re
from dataclasses import dataclass

from penguiflow.planner.guardrails import (
    ContextSnapshotV1,
    GuardrailAction,
    GuardrailDecision,
    GuardrailEvent,
    GuardrailSeverity,
    RedactionSpec,
    RuleCost,
)

SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


@dataclass
class SsnRedactionRule:
    rule_id: str = "ssn-redaction"
    version: str = "1.0.0"
    supports_event_types: frozenset[str] = frozenset(
        {"tool_call_result", "llm_stream_chunk"}
    )
    cost: RuleCost = RuleCost.FAST
    enabled: bool = True
    severity: GuardrailSeverity = GuardrailSeverity.HIGH

    async def evaluate(
        self, event: GuardrailEvent, context_snapshot: ContextSnapshotV1
    ) -> GuardrailDecision | None:
        text = event.text_content or ""
        redactions = tuple(
            RedactionSpec(
                path="text",
                replacement="[SSN]",
                entity_type="ssn",
                start_offset=match.start(),
                end_offset=match.end(),
            )
            for match in SSN.finditer(text)
        )
        if not redactions:
            return None
        return GuardrailDecision(
            action=GuardrailAction.REDACT,
            rule_id=self.rule_id,
            reason=f"{len(redactions)} SSN(s)",
            severity=self.severity,
            redactions=redactions,
        )
```

### Approval gate (PAUSE)

After `resume()`, the LLM plans again and usually repeats the call, so a rule that always pauses would pause forever. This one pauses the first prod write of a run and lets later ones in the same run through. The run id survives the pause, but the `approved_runs` set lives only in this process.

```python
from dataclasses import dataclass, field

from penguiflow.planner.guardrails import (
    ContextSnapshotV1,
    GuardrailAction,
    GuardrailDecision,
    GuardrailEvent,
    GuardrailSeverity,
    PauseSpec,
    RuleCost,
)


@dataclass
class ProdWriteApprovalRule:
    rule_id: str = "prod-write-approval"
    version: str = "1.0.0"
    supports_event_types: frozenset[str] = frozenset({"tool_call_start"})
    cost: RuleCost = RuleCost.FAST
    enabled: bool = True
    severity: GuardrailSeverity = GuardrailSeverity.HIGH
    approved_runs: set[str] = field(default_factory=set)

    async def evaluate(
        self, event: GuardrailEvent, context_snapshot: ContextSnapshotV1
    ) -> GuardrailDecision | None:
        args = event.tool_args or {}
        if event.tool_name != "db_write" or args.get("target_db") != "prod":
            return None
        if event.run_id in self.approved_runs:
            return None
        self.approved_runs.add(event.run_id)
        return GuardrailDecision(
            action=GuardrailAction.PAUSE,
            rule_id=self.rule_id,
            reason="Production write needs approval",
            pause=PauseSpec(approver_roles=("dba",), prompt="Approve the prod write?"),
        )
```

The host side:

```python
from penguiflow.planner import PlannerPause

result = await planner.run("Write the totals to prod")
if isinstance(result, PlannerPause) and result.reason == "approval_required":
    pause = result.payload["guardrail"]["pause"]   # scope, approver_roles, prompt, timeout_s
    # Show pause["prompt"] to someone with one of pause["approver_roles"].
    result = await planner.resume(result.resume_token, user_input="approved")
```

To reject, don't resume the token: this rule treats any resumed run as approved. Durable pause records are covered in [[penguiflow-hitl-pause-resume]].

## Observability

- Every rule decision, at every event type (`llm_before`, `tool_call_start`, `tool_call_result`, `llm_stream_chunk`), is logged as `guardrail_decision` on the `penguiflow.planner` logger (INFO), with `extra` keys `event_type`, `run_id`, `action`, `rule_id`, `reason` and `enforced`. Gateway defaults (`rule_id` `__default__` or `__skip__`: no rule decided) are not logged. In shadow mode a decision other than ALLOW is also logged as `guardrail_decision_shadow`.
- The gateway and evaluator log `guardrail_async_submit_failed`, `guardrail_async_error` (fail-open inbox or DEEP rule failures) and `guardrail_required_rule_missing` at WARNING on their module loggers under `penguiflow.planner.guardrails`.
- Look for the `guardrail_retry` planner event, `result.metadata["guardrail"]` (any STOP) and `PlannerPause.payload["guardrail"]`.
- To log other fields (`tool_name`, `severity`), log from the decision policy. `resolve` runs whenever at least one rule returned a decision:

```python
import logging

from penguiflow.planner.guardrails import (
    ContextSnapshotV1,
    DefaultDecisionPolicy,
    GuardrailDecision,
    GuardrailEvent,
)

logger = logging.getLogger("myapp.guardrails")


class LoggingDecisionPolicy(DefaultDecisionPolicy):
    def resolve(
        self,
        decisions: list[GuardrailDecision],
        event: GuardrailEvent,
        context_snapshot: ContextSnapshotV1,
    ) -> GuardrailDecision:
        final = super().resolve(decisions, event, context_snapshot)
        logger.info(
            "guardrail_hit",
            extra={
                "event_type": event.event_type,
                "run_id": event.run_id,
                "tool_name": event.tool_name,
                "action": final.action.value,
                "rule_id": final.rule_id,
                "severity": final.severity.value,
            },
        )
        return final
```

Pass it as `GuardrailGateway(..., decision_policy=LoggingDecisionPolicy())`. Don't log `text_content` or `tool_args`, because they may hold user data.
