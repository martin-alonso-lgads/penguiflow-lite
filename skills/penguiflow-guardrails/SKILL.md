---
name: penguiflow-guardrails
description: Add a policy-enforcement layer to a PenguiFlow ReactPlanner — build a `GuardrailGateway(registry=RuleRegistry(), guard_inbox=InMemoryGuardInbox(AsyncRuleEvaluator(registry)), config=GatewayConfig(...))`, write rules with `rule_id`/`version`/`supports_event_types`/`cost`/`enabled`/`severity` and `async evaluate(event, context_snapshot)`, register them with `registry.register(rule)`, and return `GuardrailDecision`s (`STOP`/`PAUSE`/`RETRY`/`REDACT`/`ALLOW`) on `llm_before`/`tool_call_start`/`tool_call_result`/`llm_stream_chunk`; roll out in `shadow` then `enforce`; tune FAST rules (`sync_timeout_ms`, `sync_fail_open`) and DEEP rules (risk-router waits including `llm_before_wait_ms`, late STOPs that apply within the run, `async_fail_open` for inbox and DEEP rule errors); handle a guardrail PAUSE as `PlannerPause(reason="approval_required")`; and configure the separate `ObservationGuardrailConfig` clamp for huge tool outputs. Use when a user says "add guardrails", "PII redaction", "tool denylist", "policy enforcement", "stop unsafe tool", "fail-closed", or names `GuardrailEvent`, `GuardrailDecision`, `GatewayConfig`.
---

# PenguiFlow Guardrails

## When to use
- Enforce tool allowlists/denylists at runtime, outside the prompt.
- Redact secrets/PII from tool results and streamed output.
- Fail-closed gating ("STOP if the tool would touch prod").
- HITL approval triggered by policy (`PAUSE`).
- Audit-friendly policy with stable `rule_id`s.
- Reliability safety net for oversized tool observations.

## When NOT to use
- Primary access control → use `ToolPolicy` / `ToolVisibilityPolicy` (see [[penguiflow-reactplanner-config]]). Guardrails are policy *enforcement*, not the access boundary.
- Sandboxing untrusted code → use a real sandbox; guardrails are a control plane.
- Content moderation product — guardrails are intentionally small and composable.
- HITL UX patterns → use [[penguiflow-hitl-pause-resume]] (guardrails can *trigger* a pause; the resume contract lives there).

## Hard boundaries
Guardrails are the **policy enforcement layer**. They don't replace tool visibility, ToolNode hardening, or sandboxing. The reliability `ObservationGuardrailConfig` is bundled here but is conceptually separate — it's a context-overflow safety net, not a policy decision.

## Workflow

### 1) Wire a gateway
Guardrails are off until you pass a `guardrail_gateway`:
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
planner = ReactPlanner(..., guardrail_gateway=gateway, guardrail_conversation_history_turns=1)
```
- `registry.register(rule)` files a rule by its `cost`; `register_sync(rule)` forces the sync path. There is no `register_async`.
- `SteeringGuardInbox` is an abstract base class; `InMemoryGuardInbox` is the shipped implementation.
- `GatewayConfig` defaults: `mode="enforce"`, `sync_timeout_ms=15.0`, `sync_parallel=True`, `async_enabled=True`, `sync_fail_open=False`, `async_fail_open=True`. They apply to the whole gateway, not per rule.

### 2) Roll out via `shadow` then `enforce`
- `mode="shadow"` evaluates every event, streamed chunks included, but never changes a chunk or the run.
- `mode="enforce"` applies STOP/PAUSE/RETRY/REDACT.

In both modes every rule decision, at every event type, is logged as `guardrail_decision` on the `penguiflow.planner` logger (INFO), with `extra` keys `event_type`, `run_id`, `action`, `rule_id`, `reason` and `enforced`. Gateway defaults (`rule_id` `__default__` or `__skip__`: no rule decided) are not logged. In shadow mode a decision other than ALLOW is also logged as `guardrail_decision_shadow`.

Ship in shadow, measure hit rates and false positives, fix the rules, then switch the gateway to enforce.

### 3) Write rules and classify them
A rule has `rule_id`, `version`, `supports_event_types`, `cost`, `enabled` and `severity`, plus `async evaluate(event, context_snapshot) -> GuardrailDecision | None`. Return `None` when the rule doesn't apply. See `references/gateway-and-decisions.md` for working examples.
- **`RuleCost.FAST`:** runs inline, and each rule is bounded by `sync_timeout_ms`. Use it for set lookups, regexes and deterministic checks.
- **`RuleCost.DEEP`:** runs in the guard inbox. Every DEEP rule registered for the event type runs. The gateway waits only as long as the risk router says. `DefaultRiskRouter` waits 100 ms for MEDIUM-risk tools (the default tier), 200 ms for HIGH and CRITICAL ones, and 0 for `llm_before` and stream chunks unless risk signals apply. `DefaultRiskRouter(llm_before_wait_ms=...)` (default `0.0`) sets the least wait on every `llm_before` event; set it when a DEEP rule must act on the user input. A DEEP **STOP** that arrives after that wait is applied at the same run's next guardrail check after it arrives. Late PAUSE, RETRY and REDACT decisions are dropped. Use DEEP for LLM judges and external classifiers. See `references/rules-fast-vs-deep.md`.

### 4) Pick decision actions
`GuardrailDecision(action=..., rule_id=..., reason=..., severity=..., ...)`; `rule_id` and `reason` are required. Which actions apply depends on the event:

| Event | STOP | PAUSE | RETRY | REDACT |
|---|---|---|---|---|
| `llm_before` | Run finishes `no_path`; answer = `StopSpec.user_message` | `PlannerPause(reason="approval_required")` before the LLM call | Adds `RetrySpec.corrective_message` as a system message, up to `max_attempts` per run | — |
| `tool_call_start` | Skips the tool; run finishes `no_path` | `PlannerPause(reason="approval_required")` | — | — |
| `tool_call_result` | Run finishes `no_path` (tool already ran) | `PlannerPause(reason="approval_required")` | — | Redacts the result JSON by offsets |
| `llm_stream_chunk` | Drops the chunk, then the run finishes `no_path` after the LLM call | Drops the chunk, then the run pauses after the LLM call (a STOP on any chunk wins) | — | Redacts the chunk by offsets |

- Every STOP, LLM-side or tool-side, finishes with `reason="no_path"`, `payload["raw_answer"]` = `stop.user_message` and the decision in `result.metadata["guardrail"]` (in a parallel plan, a STOP on any branch or on the join ends the run once the branches finish, and a branch STOP skips the join).
- A PAUSE payload is `payload["guardrail"] = {"action": "PAUSE", "rule_id", "reason", ..., "pause": {"scope", "approver_roles", "prompt", "timeout_s"}}`. `timeout_s` is informational only.
- On a planner built with `pause_enabled=False`, a PAUSE on any event (including the router's `on_timeout="pause"`) becomes a STOP: the run finishes `no_path`, `raw_answer` is `pause.prompt` (or a default message), and `metadata["guardrail"]` has `action="STOP"`, `requested_action="PAUSE"`, `pause_disabled=True` and `stop.error_code="GUARDRAIL_PAUSE_DISABLED"`.
- Redaction is offset-based (`RedactionSpec.start_offset`/`end_offset` into `event.text_content`); `path` is only a label.
- `tool_call_mode="native"` follows the same table: `llm_before` runs before each LLM call, and streamed chunks are checked before the step returns.

### 5) Decide fail-open vs fail-closed
| Knob | Covers | `True` | `False` |
|---|---|---|---|
| `sync_fail_open` (default `False`) | A FAST rule raising or timing out | Skip the rule | STOP (`GUARDRAIL_SYNC_ERROR` / `GUARDRAIL_SYNC_TIMEOUT`) |
| `async_fail_open` (default `True`) | Inbox `submit`/`await_response` raising, a DEEP rule raising, or `response.error` set | Log `guardrail_async_submit_failed` / `guardrail_async_error` and keep the other decisions | STOP `rule_id="__async_error__"`, `error_code="GUARDRAIL_ASYNC_ERROR"` |

Async **timeouts** are decided by the router's `on_timeout` (`allow`/`pause`/`stop`), not by `async_fail_open`. A DEEP rule that raises makes `AsyncRuleEvaluator` raise `AsyncRuleEvaluationError` (`.decisions` of the rules that didn't fail, `.errors` by rule id); `InMemoryGuardInbox` turns it into a response with those decisions plus an `error`.

### 6) Know what each event carries
| `event_type` | `text_content` | Other fields |
|---|---|---|
| `llm_before` | The prompt's last user-role message (`{"query": ...}` first, then the latest observation or resume input) | — |
| `tool_call_start` | `None` | `tool_name`, `tool_args`, `payload.tool_call_id`, `payload.action_seq` |
| `tool_call_result` | Result JSON after the observation clamp | same as above |
| `llm_stream_chunk` | Chunk text (needs an `event_callback` and streaming on) | `payload.phase`, `payload.channel` |

Every event's `payload` also has `conversation_history` (the last `guardrail_conversation_history_turns` memory turns), plus `last_assistant` and `task_scope` when known. `event.run_id` is per run and survives pause and resume. `context_snapshot.previous_violations` counts only this run's strikes (`increment_strike` effects); they survive pause and resume and are cleared when the run finishes.

### 7) Wire the reliability observation clamp
This is separate from policy guardrails: `ReactPlanner(..., observation_guardrail=ObservationGuardrailConfig(...))`. It is always on, and `None` means the default config. Defaults: `max_observation_chars=50_000`, `max_field_chars=10_000`, `truncation_suffix`, `preserve_structure=True`, `auto_artifact_threshold=20_000` (`0` disables the artifact path) and `preview_length=500`. Results over `max_observation_chars` become an artifact ref plus preview, or are truncated. Without a real `ArtifactStore`, the ref points at nothing. To effectively disable the clamp, raise `max_observation_chars`. See `references/observation-clamp.md`.

### 8) Audit and tune
Every decision carries a stable `rule_id`; count the `guardrail_decision` logs by `event_type`, `rule_id` and `action`. Run in shadow mode while you measure hit rates and timeouts, move slow rules to DEEP (or speed them up), and drop rules that never fire.

## Troubleshooting (fast checks)
- **Guardrails stop everything** — overly broad rules in `enforce`. Also check for a rule that raises: with `sync_fail_open=False`, an error, including a wrong `evaluate` signature, is a STOP.
- **Random `GUARDRAIL_SYNC_TIMEOUT` stops** — a FAST rule exceeds `sync_timeout_ms`; speed it up or make it DEEP.
- **Secrets leak through the stream** — no REDACT rule supports `llm_stream_chunk`, the gateway is in shadow mode, or no `event_callback` is set.
- **Tool allowlist not enforced** — mode is `shadow`, or the rule doesn't list `tool_call_start` in `supports_event_types`.
- **Approval PAUSE repeats after resume** — the LLM repeats the call and the rule pauses again; let the rule allow the run it already paused (example in `references/gateway-and-decisions.md`).
- **Run finishes `no_path` with `GUARDRAIL_PAUSE_DISABLED`** — a rule's PAUSE, or the router's timeout PAUSE, fired on a planner with `pause_enabled=False`. Enable pauses, or make the rule STOP.
- **DEEP rule never blocks** — the router gave it no wait, so non-STOP decisions are dropped and STOPs land one check later. Set `DefaultRiskRouter(llm_before_wait_ms=...)` for `llm_before` rules, raise the tool's risk tier, or use a custom `RiskRouter`.
- **`guardrail_required_rule_missing` warnings** — `DefaultRiskRouter` asks for `hf-jailbreak` on system-info and capability-change requests, and no DEEP rule with that id is registered. The other DEEP rules still run.
- **Tool observation became `{"result": "..."}`** — a REDACT left text that is no longer valid JSON, so the LLM gets the redacted text wrapped. Keep replacements inside string values.
- **Context overflow despite the clamp** — `max_observation_chars` is too high for the model, or a nested structure survived truncation; lower the cap or keep the artifact path on.

## Worked example
- `examples/guardrails/scope_classifier/flow.py` and `examples/guardrails/huggingface/flow.py` wire DEEP rules (a scope classifier and a prompt-injection classifier) through `InMemoryGuardInbox`. Both rules are on `llm_before`, so both flows build the gateway with `DefaultRiskRouter(llm_before_wait_ms=...)` (500 ms for the scope classifier, 5000 ms for the injection classifier): the injection classifier's STOP ends the run before the LLM call, and the scope classifier's RETRY applies to that call. With the default wait of 0, the STOP would land at the run's next check and the RETRY would be dropped (see step 3).
- `tests/test_guardrails_integration.py` (tool and `llm_before` STOP, `llm_before` and prompted-mode stream PAUSE and STOP, redaction, PAUSE payload, PAUSE without pause support, shadow logging, late DEEP STOP per run) and `tests/test_guardrails_gateway.py` (fail-open/closed, DEEP rule errors, timeouts, late decisions, strikes per run).

## References (load only as needed)
- `references/gateway-and-decisions.md` — wiring, `GatewayConfig`, `GuardrailEvent`, `GuardrailDecision` and its specs, the per-event action table, decision resolution, built-in rules, rule examples, observability.
- `references/rules-fast-vs-deep.md` — FAST timeouts, DEEP waits and late STOPs, `DefaultRiskRouter`, tool risk tiers, `async_fail_open`, the `SteeringGuardInbox` contract.
- `references/observation-clamp.md` — `ObservationGuardrailConfig`, the artifact and truncation paths, the no-store caveat, tuning.
