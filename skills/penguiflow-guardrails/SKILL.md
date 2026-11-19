---
name: penguiflow-guardrails
description: Add a policy-enforcement layer to a PenguiFlow ReactPlanner — build a `GuardrailGateway(registry=RuleRegistry(), guard_inbox=InMemoryGuardInbox(AsyncRuleEvaluator(registry)), config=GatewayConfig(...))`, write rules with `rule_id`/`version`/`supports_event_types`/`cost`/`enabled`/`severity` and `async evaluate(event, context_snapshot)`, register them with `registry.register(rule)`, and return `GuardrailDecision`s (`STOP`/`PAUSE`/`RETRY`/`REDACT`/`ALLOW`) on `llm_before`/`tool_call_start`/`tool_call_result`/`llm_stream_chunk`; roll out in `shadow` then `enforce`; tune FAST rules (`sync_timeout_ms`, `sync_fail_open`) and DEEP rules (risk-router waits, late STOPs that apply within the run, `async_fail_open` for inbox errors); handle a guardrail PAUSE as `PlannerPause(reason="approval_required")`; and configure the separate `ObservationGuardrailConfig` clamp for huge tool outputs. Use when a user says "add guardrails", "PII redaction", "tool denylist", "policy enforcement", "stop unsafe tool", "fail-closed", or names `GuardrailEvent`, `GuardrailDecision`, `GatewayConfig`.
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
- `mode="shadow"` evaluates rules but applies nothing. Only `llm_before` decisions are logged (`guardrail_decision_shadow`), and stream chunks are not evaluated at all. To measure tool-event hit rates, log from a custom decision policy (see `references/gateway-and-decisions.md`).
- `mode="enforce"` applies STOP/PAUSE/RETRY/REDACT.

Ship in shadow, measure hit rates and false positives, fix the rules, then switch the gateway to enforce.

### 3) Write rules and classify them
A rule has `rule_id`, `version`, `supports_event_types`, `cost`, `enabled` and `severity`, plus `async evaluate(event, context_snapshot) -> GuardrailDecision | None`. Return `None` when the rule doesn't apply. See `references/gateway-and-decisions.md` for working examples.
- **`RuleCost.FAST`:** runs inline, and each rule is bounded by `sync_timeout_ms`. Use it for set lookups, regexes and deterministic checks.
- **`RuleCost.DEEP`:** runs in the guard inbox. The gateway waits only as long as the risk router says. `DefaultRiskRouter` waits 100 ms for MEDIUM-risk tools (the default tier), 200 ms for HIGH and CRITICAL ones, and 0 for `llm_before` and stream chunks unless risk signals apply. A DEEP **STOP** that arrives after that wait is applied at the same run's next guardrail check after it arrives. Late PAUSE, RETRY and REDACT decisions are dropped. Use DEEP for LLM judges and external classifiers. See `references/rules-fast-vs-deep.md`.

### 4) Pick decision actions
`GuardrailDecision(action=..., rule_id=..., reason=..., severity=..., ...)`; `rule_id` and `reason` are required. Which actions apply depends on the event:

| Event | STOP | PAUSE | RETRY | REDACT |
|---|---|---|---|---|
| `llm_before` | Ends the run; answer = `StopSpec.user_message` | — | Adds `RetrySpec.corrective_message` as a system message, up to `max_attempts` per run | — |
| `tool_call_start` | Skips the tool; run finishes `no_path` | `PlannerPause(reason="approval_required")` | — | — |
| `tool_call_result` | Run finishes `no_path` (tool already ran) | `PlannerPause(reason="approval_required")` | — | Redacts the result JSON by offsets |
| `llm_stream_chunk` | Drops the chunk, then ends the run after the LLM call | Drops the chunk | — | Redacts the chunk by offsets |

- A PAUSE payload is `payload["guardrail"] = {"action": "PAUSE", "rule_id", "reason", ..., "pause": {"scope", "approver_roles", "prompt", "timeout_s"}}`. `timeout_s` is informational only. PAUSE needs `pause_enabled=True` (the default).
- Redaction is offset-based (`RedactionSpec.start_offset`/`end_offset` into `event.text_content`); `path` is only a label.
- In `tool_call_mode="native"`, `llm_before` is not evaluated, and a stream STOP only drops chunks.

### 5) Decide fail-open vs fail-closed
| Knob | Covers | `True` | `False` |
|---|---|---|---|
| `sync_fail_open` (default `False`) | A FAST rule raising or timing out | Skip the rule | STOP (`GUARDRAIL_SYNC_ERROR` / `GUARDRAIL_SYNC_TIMEOUT`) |
| `async_fail_open` (default `True`) | Inbox `submit`/`await_response` raising, or `response.error` set | Log `guardrail_async_submit_failed` / `guardrail_async_error` and skip | STOP `rule_id="__async_error__"`, `error_code="GUARDRAIL_ASYNC_ERROR"` |

Async **timeouts** are decided by the router's `on_timeout` (`allow`/`pause`/`stop`), not by `async_fail_open`. Exceptions raised inside a DEEP rule are swallowed by `AsyncRuleEvaluator`, so they never reach either flag.

### 6) Know what each event carries
| `event_type` | `text_content` | Other fields |
|---|---|---|
| `llm_before` | The prompt's last user-role message (`{"query": ...}` first, then the latest observation or resume input) | — |
| `tool_call_start` | `None` | `tool_name`, `tool_args`, `payload.tool_call_id`, `payload.action_seq` |
| `tool_call_result` | Result JSON after the observation clamp | same as above |
| `llm_stream_chunk` | Chunk text (enforce mode, with an `event_callback`) | `payload.phase`, `payload.channel` |

Every event's `payload` also has `conversation_history` (the last `guardrail_conversation_history_turns` memory turns), plus `last_assistant` and `task_scope` when known. `event.run_id` is per run and survives pause and resume.

### 7) Wire the reliability observation clamp
This is separate from policy guardrails: `ReactPlanner(..., observation_guardrail=ObservationGuardrailConfig(...))`. It is always on, and `None` means the default config. Defaults: `max_observation_chars=50_000`, `max_field_chars=10_000`, `truncation_suffix`, `preserve_structure=True`, `auto_artifact_threshold=20_000` (`0` disables the artifact path) and `preview_length=500`. Results over `max_observation_chars` become an artifact ref plus preview, or are truncated. Without a real `ArtifactStore`, the ref points at nothing. To effectively disable the clamp, raise `max_observation_chars`. See `references/observation-clamp.md`.

### 8) Audit and tune
Every decision carries a stable `rule_id`. Run in shadow mode while you measure hit rates and timeouts, move slow rules to DEEP (or speed them up), and drop rules that never fire.

## Troubleshooting (fast checks)
- **Guardrails stop everything** — overly broad rules in `enforce`. Also check for a rule that raises: with `sync_fail_open=False`, an error, including a wrong `evaluate` signature, is a STOP.
- **Random `GUARDRAIL_SYNC_TIMEOUT` stops** — a FAST rule exceeds `sync_timeout_ms`; speed it up or make it DEEP.
- **Secrets leak through the stream** — no REDACT rule supports `llm_stream_chunk`, the gateway is in shadow mode, or no `event_callback` is set.
- **Tool allowlist not enforced** — mode is `shadow`, or the rule doesn't list `tool_call_start` in `supports_event_types`.
- **Approval PAUSE repeats after resume** — the LLM repeats the call and the rule pauses again; let the rule allow the run it already paused (example in `references/gateway-and-decisions.md`).
- **`RuntimeError: Pause/resume is disabled`** — a PAUSE rule fired with `pause_enabled=False`.
- **DEEP rule never blocks** — the router gave it no wait, so non-STOP decisions are dropped and STOPs land one check later. Raise the tool's risk tier or use a custom `RiskRouter`.
- **REDACT on a tool result didn't reach the LLM** — the redacted text was no longer valid JSON; keep replacements inside string values.
- **Context overflow despite the clamp** — `max_observation_chars` is too high for the model, or a nested structure survived truncation; lower the cap or keep the artifact path on.

## Worked example
- `examples/guardrails/scope_classifier/flow.py` and `examples/guardrails/huggingface/flow.py` wire DEEP rules (a scope classifier and a prompt-injection classifier) through `InMemoryGuardInbox`. Both rules are on `llm_before`, which the default router doesn't wait for unless risk signals apply. The injection classifier's STOP then lands at the run's next check, and the scope classifier's RETRY is dropped (see step 3).
- `tests/test_guardrails_integration.py` (tool STOP, redaction, PAUSE payload, late DEEP STOP per run) and `tests/test_guardrails_gateway.py` (fail-open/closed, timeouts, late decisions).

## References (load only as needed)
- `references/gateway-and-decisions.md` — wiring, `GatewayConfig`, `GuardrailEvent`, `GuardrailDecision` and its specs, the per-event action table, decision resolution, built-in rules, rule examples, observability.
- `references/rules-fast-vs-deep.md` — FAST timeouts, DEEP waits and late STOPs, `DefaultRiskRouter`, tool risk tiers, `async_fail_open`, the `SteeringGuardInbox` contract.
- `references/observation-clamp.md` — `ObservationGuardrailConfig`, the artifact and truncation paths, the no-store caveat, tuning.
