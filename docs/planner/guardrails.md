# Guardrails (ReactPlanner safety layer)

## What it is / when to use it

PenguiFlow guardrails are a **policy enforcement layer** that can inspect and constrain:

- user input before the LLM plans (`llm_before`)
- tool execution (`tool_call_start` / `tool_call_result`)
- streamed text output (`llm_stream_chunk`)

Use guardrails when you need:

- tool allowlists/denylists enforced outside the prompt,
- secret redaction in streamed output,
- “fail closed” behavior for high-risk actions (STOP / PAUSE),
- a policy pack you can audit and version.

This page focuses on guardrails **as used by `ReactPlanner`**. ToolNode also has its own hardening controls; see **[Tooling](tooling.md)**.

## Non-goals / boundaries

- Guardrails are not a replacement for `ToolPolicy` / tool visibility. Use those for primary access control.
- Guardrails do not make unsafe tools “safe”. They are a *control plane*, not a sandbox.
- Guardrails are not a full content moderation product; policy packs are intentionally small and composable.

## Contract surface

### Wiring guardrails into ReactPlanner

Guardrails are **off by default**. Enable them by passing a gateway at construction:

- `ReactPlanner(..., guardrail_gateway=gateway)`
- optionally: `guardrail_conversation_history_turns=N` (adds memory turns to the guardrail context payload)

When enabled, the planner can enforce these actions:

- `STOP`: end the run with a user-safe message. Every STOP (before the LLM call, on a stream chunk, on a tool, or on a branch or the join of a parallel plan) finishes with `reason="no_path"`, the message as `raw_answer` and the decision in `metadata["guardrail"]`
- `PAUSE`: trigger HITL approval at `llm_before` (before the LLM call), on a stream chunk (the chunk is dropped and the run pauses after the LLM call), or at `tool_call_start` or `tool_call_result` (`run()` returns `PlannerPause(reason="approval_required")`). On a planner built with `pause_enabled=False` it becomes a STOP: `metadata["guardrail"]` has `action="STOP"`, `requested_action="PAUSE"`, `pause_disabled=True` and `stop.error_code="GUARDRAIL_PAUSE_DISABLED"`, and `raw_answer` is the `PauseSpec` prompt (or a default message)
- `RETRY`: re-run the LLM call with corrective instructions
- `REDACT`: redact text from streamed output and tool results. If a tool result is no longer JSON after redaction, the LLM sees `{"result": "<redacted text>"}`, never the original
- `ALLOW`: do nothing

Guardrails apply the same way in native tool-calling mode (`tool_call_mode="native"`).

### Guardrail events and decisions

The gateway evaluates a `GuardrailEvent` and returns a `GuardrailDecision`.

Key fields:

- `GuardrailEvent.event_type`: e.g. `llm_before`, `tool_call_start`, `tool_call_result`, `llm_stream_chunk`
- `GuardrailEvent.text_content`: user input or streamed text (when applicable)
- `GuardrailEvent.tool_name` / `tool_args`: tool call metadata (when applicable)
- `GuardrailEvent.payload`: extra structured context (tool_call_id, action_seq, conversation history, etc.)

Decisions include:

- `action`: `ALLOW | REDACT | RETRY | PAUSE | STOP`
- `rule_id`: stable identifier for policy auditing
- `severity` / `confidence`
- optional action-specific payloads:
  - `redactions: tuple[RedactionSpec, ...]`
  - `retry: RetrySpec(max_attempts, corrective_message)`
  - `pause: PauseSpec(scope, approver_roles, prompt, timeout_s)`, copied into `PlannerPause.payload["guardrail"]["pause"]` (`timeout_s` is informational; the planner does not enforce it)
  - `stop: StopSpec(error_code, user_message, internal_reason)`

### Modes: `shadow` vs `enforce`

Guardrail gateway config controls enforcement:

- `GatewayConfig(mode="shadow")`: evaluate every event (streamed chunks included) and log decisions, but never change a chunk or the run
- `GatewayConfig(mode="enforce")`: apply STOP/PAUSE/RETRY/REDACT behavior

In both modes the planner logs every rule decision as `guardrail_decision` on the `penguiflow.planner` logger, with `event_type`, `run_id`, `action`, `rule_id`, `reason` and `enforced`. In shadow mode, a decision other than `ALLOW` is also logged as `guardrail_decision_shadow`.

> [!TIP]
> Start in `shadow` mode in production to measure rule hit rates, then roll to `enforce`.

### Sync vs async rules (latency control)

Rules are classified by cost:

- **FAST** rules are evaluated synchronously (on the request path)
- **DEEP** rules are evaluated asynchronously via a `SteeringGuardInbox` (for example `InMemoryGuardInbox(AsyncRuleEvaluator(registry))`)

Every DEEP rule registered for the event type runs. The gateway waits for them only as long as the risk router says (`async_wait_ms`; `DefaultRiskRouter` derives it from the tool risk tier and context signals). `DefaultRiskRouter(llm_before_wait_ms=...)` (default `0`) sets the least wait before each LLM call; with the default, a DEEP decision on the user input arrives late. A DEEP `STOP` that arrives after the wait is applied at the same run's next guardrail check; late `PAUSE`, `REDACT` and `RETRY` decisions are dropped. Each `run()` has its own guardrail run id (kept across pause and resume), so neither a late decision nor a strike (the context snapshot's `previous_violations`) reaches another run.

Gateway options you will tune:

- `sync_timeout_ms` (default 15ms)
- `sync_parallel` (run sync rules concurrently)
- `sync_fail_open` (a sync rule that errors or times out: skip it, or STOP)
- `async_fail_open` (the guard inbox fails — `submit`/`await_response` raises, or a response reports an `error`, as it does when a DEEP rule raises: log it and keep the other decisions, or STOP with `GUARDRAIL_ASYNC_ERROR`). Async timeouts are decided by the risk router's `on_timeout`, not by this flag.

### Observation size “guardrail” (reliability safety net)

Separately from the guardrail gateway, `ReactPlanner` applies an **observation clamp** to prevent context overflow:

- `ReactPlanner(..., observation_guardrail=ObservationGuardrailConfig(...))`
- default: enabled with `ObservationGuardrailConfig()`

If a tool produces an overly large JSON observation, the planner will:

1) try to store it as an artifact (if an `ArtifactStore` is available) and return an artifact reference, otherwise
2) truncate it (optionally preserving JSON structure)

This is a reliability guardrail, not a policy decision system.

## Operational defaults (recommended)

- Use `ToolPolicy` / tool visibility as your primary access control.
- Run guardrails in `shadow` mode first, then enforce:
  - enforce **tool allowlists** (STOP on unauthorized tools)
  - enforce **secret redaction** (REDACT on streamed output/tool results)
- Keep sync rules small and deterministic; push high-latency checks to async rules.

Tradeoff guidance:

- `sync_fail_open=False` is safer (a guardrail timeout blocks), but can impact availability.
- `sync_fail_open=True` improves availability, but can allow policy bypass on timeouts.

## Failure modes & recovery

### Guardrails “stopping everything”

**Symptoms**

- planner finishes early (`reason="no_path"`) with a generic safe message, or tool calls return `guardrail_stop:*`

**Fix**

- check which rule triggered (`metadata["guardrail"]["rule_id"]`) and whether you are in `shadow` or `enforce`
- `stop.error_code="GUARDRAIL_PAUSE_DISABLED"` means a PAUSE fired on a planner with `pause_enabled=False`
- verify your allowlist/denylist config (especially when catalogs are tenant-scoped)

### Redaction doesn’t apply to streamed output

**Likely causes**

- `stream_final_response=False` (no LLM streaming path), or no `event_callback`
- guardrails aren’t enabled (no `guardrail_gateway`)
- gateway is in `shadow` mode (decision logged but not applied)

### Observation clamp triggers unexpectedly

**Symptoms**

- tool observations become `{artifact: ..., preview: ...}` or truncated stubs

**Fix**

- ensure ToolNode artifact extraction is configured correctly (large outputs should be artifacts)
- increase `ObservationGuardrailConfig.max_observation_chars` only if you have token budget headroom

## Observability

Recommended signals:

- count the planner's `guardrail_decision` log records (INFO; `event_type`, `run_id`, `action`, `rule_id`, `reason`, `enforced`, no raw content) by rule and action; no record is written when no rule decided
- watch the WARNING records `guardrail_async_error`, `guardrail_async_submit_failed` and `guardrail_required_rule_missing` (DEEP rule or inbox failures, and router-requested rules that aren't registered)
- record `PlannerEvent(event_type="guardrail_retry")` counts (retries can hide a failing model/prompt)
- alert on repeated `guardrail_stop:*` outcomes for a tenant (misconfig or abuse)

Useful places to look:

- `PlannerFinish.metadata["guardrail"]` holds the decision of any STOP that ended the run.
- `PlannerEvent(event_type="tool_call_result").extra["result_json"]` includes a `failure.guardrail` payload on a tool STOP. A guardrail PAUSE emits no tool result; its decision is in `PlannerPause.payload["guardrail"]`.
- streamed output seen in `PlannerEvent(event_type="llm_stream_chunk")` is already post-redaction (when enforced).

See **[Planner observability](observability.md)**.

## Security / multi-tenancy notes

- Guardrails should not rely on “prompt compliance”. Enforce the policy in code.
- Prefer per-tenant configuration (tool visibility + allowlists) instead of global rules.
- Never log raw prompts/tool results when debugging guardrails; log decision metadata plus stable correlation ids.

## Runnable examples

Guardrails examples exist under `examples/guardrails/`:

- `uv run python examples/guardrails/huggingface/flow.py`
- `uv run python examples/guardrails/scope_classifier/flow.py`

## Troubleshooting checklist

- Is `ReactPlanner(..., guardrail_gateway=...)` set (guardrails are disabled otherwise)?
- Is `GatewayConfig.mode` set to `enforce` when you expect blocking/redaction?
- Do your rules list the correct `supports_event_types` for the events you care about?
- Are timeouts too aggressive (`sync_timeout_ms`) causing unexpected STOP (fail-closed) behavior?
- Does a DEEP rule on `llm_before` get a wait (`DefaultRiskRouter(llm_before_wait_ms=...)`)? Without one, only its STOP applies, at the run's next check.
- Are you confusing policy guardrails with the observation clamp (`ObservationGuardrailConfig`)?

