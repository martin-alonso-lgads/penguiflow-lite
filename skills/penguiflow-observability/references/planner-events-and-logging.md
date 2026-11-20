# `PlannerEvent` Catalog and Structured Logging

## `configure_logging(...)`

```python
from penguiflow import configure_logging

configure_logging(
    level="INFO",  # standard logging level
    structured=True,  # JSON lines; False = human-readable
    include_extras=True,  # when structured=False, append extra fields
    logger_name="penguiflow",  # default
)
```

Configures one logger (default `penguiflow`) and its children: it replaces that logger's handlers with one stream handler and sets `propagate=False`, so root handlers no longer receive those records. Useful sub-loggers:
- `penguiflow.planner` — every `PlannerEvent` (`llm_stream_chunk` at DEBUG, others at INFO), plus planner warnings (`deadline_exhausted`, `event_callback_error`, ...).
- `penguiflow.llm.telemetry`, `penguiflow.llm.tracing` — LLM call telemetry and spans.
- `penguiflow.tools.node` — ToolNode / MCP / UTCP connections and calls.
- `penguiflow.sessions.session`, `penguiflow.sessions.telemetry` — sessions and `LoggingTaskTelemetrySink`.

The A2A server logs on `penguiflow_a2a` (failed planner runs are logged with `task_id`, push delivery failures as `a2a_push_delivery_failed`). That is a separate root logger, not a child of `penguiflow`, so configure it with its own call:

```python
from penguiflow import configure_logging

configure_logging(level="INFO", structured=True, logger_name="penguiflow_a2a")
```

Call once per logger at process startup. Re-calling for the same logger resets its handlers.

## `PlannerEvent`

```python
from penguiflow.planner import PlannerEvent

event = PlannerEvent(
    event_type="step_complete",
    ts=0.0,
    trajectory_step=0,
    thought=None,
    node_name="search",
    latency_ms=12.5,
    token_estimate=None,
    error=None,
    extra={},
)
```

`PlannerEvent` is a frozen dataclass with exactly these fields; `extra` defaults to an empty mapping.

`event.to_payload()` returns `{"event": event_type, "ts", "step", ...optional fields, **extra}` (reserved `logging` keys are filtered out of `extra`). That is what the planner logs and what you should ship to log pipelines.

Receive events with `ReactPlanner(..., event_callback=fn)`, where `fn(event)` is synchronous. Exceptions in the callback are logged (`event_callback_error`) and swallowed.

## Event type catalog

### Loop lifecycle
| `event_type` | When | Useful fields |
|---|---|---|
| `step_start` | a planning step begins | `extra["action_seq"]` |
| `step_complete` | a tool step finished | `node_name` (tool), `latency_ms`, `thought` |
| `finish` | the run finished | `extra["reason"]`, `extra["cost"]`, `extra["answer_action_seq"]`, `extra["error"]` (when set), `thought` |
| `resume` | a paused run resumed | `extra["user_input"]` |
| `steering_received` | a steering event was consumed | `extra["event_id"]`, `extra["event_type"]`, `extra["source"]` |

Pauses don't emit an event — `planner.run()` returns a `PlannerPause`.

### Tool calls
| `event_type` | Useful fields |
|---|---|
| `tool_call_start` | `tool_name`, `tool_call_id`, `args_json`, `action_seq` |
| `tool_call_end` | `tool_name`, `tool_call_id`, `action_seq` — emitted right after `tool_call_start`, **before** the tool runs |
| `tool_call_result` | `tool_name`, `tool_call_id`, `result_json`, `action_seq` — after the tool returns, fails (`{"error": ...}`), pauses or is stopped by a guardrail; use this for tool latency and outcome |
| `tool_activated`, `tool_activation_denied`, `tool_search_query`, `tool_get` | tool discovery |
| `observation_clamped` | an observation exceeded its size guardrail |
| `artifact_stored`, `resource_updated` | an artifact was stored; an MCP resource changed |

### Quality and repair
`planner_args_invalid` / `planner_args_suspect` (`extra["tool"]`, `extra["error_summary"]`, `extra["placeholders"]`, `extra["source"]`), `planner_repair_attempt`, `arg_fill_failure`, `finish_repair_success` / `finish_repair_failure`, `graceful_failure_attempt` / `graceful_failure_success`, `multi_action_fallback_used`, `reflection_critique`, `guardrail_retry`.

### Streaming
`llm_stream_chunk`, `stream_chunk`, `artifact_chunk` — see [[penguiflow-streaming]].

### Context and skills
`llm_before`, `llm_context_hook_applied` / `llm_context_hook_failed`, `skills_retrieved`, `skill_pack_loaded`, `skill_directory_rendered`, `skill_search_query` / `skill_get` / `skill_list` / `skill_propose`, `tool_directory_rendered`, `tool_hints_generated`.

Guardrail decisions at every event type (`llm_before`, `tool_call_start`, `tool_call_result`, `llm_stream_chunk`) are INFO log records on `penguiflow.planner` (`guardrail_decision`, with `event_type`, `run_id`, `action`, `rule_id`, `reason`, `enforced`; plus `guardrail_decision_shadow` for non-ALLOW decisions in shadow mode), not planner events; `guardrail_retry` is the planner event. A run ended by a guardrail STOP finishes `no_path` with `metadata["guardrail"]`. See [[penguiflow-guardrails]].

Grep the planner for `event_type="` when you need the exact payload of a rarer event.

## Reducing volume
- Keep `penguiflow.planner` at INFO in staging; in production raise it to WARNING and rely on `event_callback` metrics plus sampled logs.
- `llm_stream_chunk` is already DEBUG.
- Drop `tool_call_start.args_json` / `tool_call_result.result_json` from shipped logs if they can be large or sensitive.

## Run-level record

The returned `PlannerFinish.metadata` is the best single record of a run: `reason`, `thought`, `steps` (trajectory history with actions, observations, errors, streams), `step_count`, `cost` (`total_cost_usd`, `main_llm_calls`, `reflection_llm_calls`, `summarizer_llm_calls`), `sources`, `artifacts`, `llm_context`, `tool_context`, `constraints`, `trajectory_metadata`, `answer_action_seq`, and repair counters (`validation_failures_count`, `repair_attempts`, `args_invalid_count`, `finish_repair_success_count`, ...). Log a summary of it (not the whole thing) per run.

With a `state_store` and a `trace_id` (plus `session_id`) in `tool_context`, the planner also saves the trajectory (`save_trajectory`) and its events (`save_planner_event`) in the background, if the store has those methods. `penguiflow-admin` doesn't read these; it reads `StoredEvent`s via `load_history`, which only `StateStoreSessionAdapter` writes, under trace id `session:<session_id>`.

## Trace correlation
- Put a request/trace id in `tool_context` (e.g. `trace_id`, `session_id`) — sessions do this for you (`tool_context["trace_id"]`, `["task_id"]`).
- Add it to your log records with a `logging.Filter` or `LoggerAdapter` in the code that calls `planner.run()`.
- Never use it as a metric label.

## Redaction

PenguiFlow does **not** redact secrets. Common leak vectors:
- `tool_call_start.extra["args_json"]` and `tool_call_result.extra["result_json"]`.
- Exception messages from tools (surface in step errors).
- `llm_context` (sent to the LLM *and* returned in `PlannerFinish.metadata["llm_context"]`).
- `tool_context` (never sent to the LLM, but returned in `PlannerFinish.metadata["tool_context"]` and persisted with trajectories when it is JSON-serializable).

```python
import logging

from penguiflow.planner import PlannerEvent

log = logging.getLogger("my_app.planner_events")
SECRET_KEYS = {"authorization", "api_key", "token", "cookie", "password"}


def redact(value: object) -> object:
    if isinstance(value, dict):
        return {
            k: "[redacted]" if k.lower() in SECRET_KEYS else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def on_event(event: PlannerEvent) -> None:
    payload = redact(event.to_payload())
    if isinstance(payload, dict):
        log.info(event.event_type, extra=payload)
```

`args_json` / `result_json` are JSON strings, so key-based redaction doesn't reach inside them; parse them first or drop them.

At the boundary: keep secrets in `tool_context` (never shown to the LLM), and sanitize exception messages raised by tools.

## Operational defaults
- `configure_logging(structured=True)` in production.
- One metrics `event_callback` per planner (or per planner factory).
- Tag logs with `service`, `env`, `worker_id`.
- Index logs by your trace/session id.

## Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| No planner events | logging not configured or level too high | `configure_logging(...)`; check `penguiflow.planner` level |
| penguiflow logs missing from root handlers | `configure_logging` sets `propagate=False` | use its handler, or configure the logger yourself |
| No A2A logs | `penguiflow_a2a` is outside the `penguiflow` tree | `configure_logging(logger_name="penguiflow_a2a")` |
| Callback metrics missing | callback replaced per request on a shared planner | process-wide callback or planner factory |
| Event loop stalls | blocking I/O in `event_callback` | enqueue and export asynchronously |
| Secrets in logs | args/results logged verbatim | redact in the callback / logging filter |
