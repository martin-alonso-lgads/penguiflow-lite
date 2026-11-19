---
name: penguiflow-observability
description: Add structured observability to a ReactPlanner deployment — turn on JSON logging with `configure_logging(structured=True)` (plus a separate call for the `penguiflow_a2a` logger), consume `PlannerEvent`s (`step_start`/`step_complete`/`tool_call_*`/`finish`/`planner_args_invalid`/`guardrail_*`/...) through `event_callback` or the `penguiflow.planner` logger, derive counters/histograms with safe cardinality (never tag by `trace_id`/`session_id`), trace native-adapter LLM calls with `PENGUIFLOW_LLM_TRACING`/`LLMTraceSink` (requires `use_native_llm=True`), record task lifecycle with a `TaskTelemetrySink`, read run cost/steps from `PlannerFinish.metadata`, and inspect session history persisted through `StateStoreSessionAdapter` with `penguiflow-admin`. Use when a user says "add observability", "logging", "metrics", "alerts", "trace this agent", "LLM tracing", "cost tracking", or asks how to monitor a production planner fleet.
---

# PenguiFlow Observability

## When to use
- Wiring structured logging into a production planner deployment.
- Defining the metric/alert baseline for planner workers.
- Tracing LLM calls (latency, tokens, cost) to logs or MLflow.
- Investigating a run after the fact (steps, errors, persisted history).

## When NOT to use
- Shipping events to frontend clients → use [[penguiflow-streaming]].
- Planner configuration (which features emit which events) → use [[penguiflow-reactplanner-config]].
- StateStore backends for durable history → use [[penguiflow-statestore]].
- Guardrail decision auditing in depth → use [[penguiflow-guardrails]].

## Hard boundaries
This skill is the **operator observability surface**: logs, planner events as metrics, LLM tracing, task telemetry, cost, and offline inspection. Client-facing event delivery lives in [[penguiflow-streaming]].

## Workflow

### 1) Turn on structured logging
```python
from penguiflow import configure_logging

configure_logging(level="INFO", structured=True)
configure_logging(level="INFO", structured=True, logger_name="penguiflow_a2a")
```
The first call configures the `penguiflow` logger tree to emit JSON lines (`structured=False, include_extras=True` for readable dev output). Useful sub-loggers: `penguiflow.planner` (every planner event), `penguiflow.llm.telemetry` / `penguiflow.llm.tracing` (LLM calls), `penguiflow.tools.node` (ToolNode/MCP), `penguiflow.sessions.*` (sessions and task telemetry). The A2A server logs on its own `penguiflow_a2a` logger, outside that tree, so it needs the second call (or your own handler). `configure_logging` sets `propagate=False`, so your root handlers stop seeing those records.

### 2) Know what the planner already logs
Every `PlannerEvent` is logged on `penguiflow.planner` with `event.to_payload()` as `extra` — `llm_stream_chunk` at DEBUG, everything else at INFO. Lower that logger to WARNING in production if volume is a problem, and use step 3 for metrics.

### 3) Hook `event_callback` for metrics
```python
from prometheus_client import Counter, Histogram

from penguiflow.planner import PlannerEvent, ReactPlanner

STEP_LATENCY = Histogram("pf_step_latency_s", "Tool step latency", ["tool"])
FINISH = Counter("pf_finish_total", "Finished runs", ["reason"])


def on_event(event: PlannerEvent) -> None:  # synchronous, keep it cheap
    if event.event_type == "step_complete":
        tool = event.node_name or "unknown"
        STEP_LATENCY.labels(tool=tool).observe((event.latency_ms or 0) / 1000)
    elif event.event_type == "finish":
        FINISH.labels(reason=event.extra.get("reason", "unknown")).inc()


planner = ReactPlanner(llm="gpt-4o-mini", catalog=..., event_callback=on_event)
```
Event catalog and fields in `references/planner-events-and-logging.md`.

### 4) Derive metrics with the right cardinality
**Never tag metrics by `trace_id`, `session_id`, `task_id` or `user_id`.** Safe tags: `event_type`, tool name (bounded by your catalog), finish `reason`, `env`, `service`. A tenant tag only if the tenant count is bounded.

Recommended baseline:
- **Counters**: runs by finish reason (`answer_complete` / `no_path` / `budget_exhausted`), tool calls and tool errors by tool, `planner_args_invalid`, `finish_repair_*`, `guardrail_retry`, pauses.
- **Histograms**: `step_complete.latency_ms` by tool; run duration; steps per run (`PlannerFinish.metadata["step_count"]`).
- **Cost**: `PlannerFinish.metadata["cost"]["total_cost_usd"]` (also `finish` event `extra["cost"]`) per run.

Recipes in `references/metrics-and-alerts.md`.

### 5) Trace LLM calls
Tracing covers the **native LLM adapter only**: `ReactPlanner(use_native_llm=True, ...)` (the default is `False`, the LiteLLM client) or an adapter you build with `create_native_adapter(...)` and pass as `llm_client=`.
- Zero-code: `export PENGUIFLOW_LLM_TRACING=mlflow` (or `log`) — every native-adapter completion becomes a span with provider, model, tokens, cost and latency (never message contents). Without `use_native_llm=True` the variable does nothing.
- Explicit: pass `trace_sink=MlflowLLMTraceSink()` / `LoggingLLMTraceSink()` (from `penguiflow.llm.tracing`) to `create_native_adapter(...)`.
- Raw LLM events (`request_start`/`request_end`/`retry`/`error`): `get_telemetry_hooks().register(callback)` from `penguiflow.llm.telemetry`, with ready-made `create_mlflow_callback()` / `create_prometheus_callback()`. These fire only for the standalone `penguiflow.llm.LLMClient`, not for planner LLM calls.

Worked example: `examples/mlflow_llm_tracing/`.

### 6) Task lifecycle with sessions
`StreamingSession` / `SessionManager` accept `telemetry_sink=` — a `TaskTelemetrySink` receiving `TaskTelemetryEvent`s (`task_spawned`/`task_completed`/`task_failed`/`task_cancelled`, `task_group_completed`/`task_group_failed`, with `duration_ms`, task type and mode). `LoggingTaskTelemetrySink` (in `penguiflow.sessions.telemetry`) logs them. See [[penguiflow-background-tasks]].

### 7) Inspect runs after the fact
- `PlannerFinish.metadata` carries `steps` (the full trajectory with observations/errors), `step_count`, `cost`, `sources`, `artifacts`, `llm_context`, `tool_context`, and repair counters.
- Persisted session history: `penguiflow-admin history session:<session_id> --state-store module:factory` (or `replay ... --delay 0.2`) prints the `StoredEvent`s that `StateStoreSessionAdapter` saved for that session (tasks, updates, steering). A bare `planner.run()` doesn't write `StoredEvent`s; with a `state_store` and `trace_id` in `tool_context` it saves through `save_trajectory`/`save_planner_event`, which the CLI doesn't read.
- Export the tool catalog for reviews: `[spec.to_tool_record() for spec in catalog]` — see `references/tool-catalog-export.md`.

### 8) Don't leak secrets into logs/metrics
- Planner event payloads can include tool args (`tool_call_start.extra["args_json"]`) and observations — redact before shipping logs off-host.
- Keep secrets in `tool_context` (never sent to the LLM), not `llm_context`. Both are still returned in `PlannerFinish.metadata` and persisted with trajectories, so don't log those wholesale.
- Sanitize exception messages raised by tools; they surface in `tool_call_result` / step errors.

## Troubleshooting (fast checks)
- **No planner events in logs** — `configure_logging` not called, or the `penguiflow.planner` logger level is above INFO.
- **No A2A server logs** — `penguiflow_a2a` isn't under `penguiflow`; configure it separately.
- **penguiflow logs missing from your root handlers** — `configure_logging` sets `propagate=False`; either use its handler or skip it and configure the `penguiflow` logger yourself.
- **`event_callback` metrics missing under load** — one shared planner instance with a per-request callback overwritten by another request; set a process-wide metrics callback, or build planners from a factory.
- **Metric backend OOMs** — tagged by `trace_id`/`session_id`; demote to a log field.
- **No LLM spans** — `use_native_llm` is False (the default), `PENGUIFLOW_LLM_TRACING` unset or misspelled (a warning is logged), or MLflow not installed.
- **`penguiflow-admin` prints nothing** — the trace id isn't `session:<session_id>`, the store isn't the one behind `StateStoreSessionAdapter`, or the history came from a bare `planner.run()` (not readable by the CLI).
- **Sensitive data in logs** — tool args or observations logged verbatim; redact via a logging filter.

## Worked examples
- `examples/mlflow_llm_tracing/` — LLM tracing to MLflow.
- `examples/planner_enterprise_agent/telemetry.py` — planner-event telemetry collector.
- `examples/streaming_llm/stream_answer.py` — `event_callback` wiring.

## References (load only as needed)
- `references/planner-events-and-logging.md` — `configure_logging` options, `PlannerEvent` catalog and fields, logging volume, redaction.
- `references/metrics-and-alerts.md` — counters/histograms, cardinality rules, Prometheus/OpenTelemetry recipes, dashboards, alerts.
- `references/tool-catalog-export.md` — exporting the tool catalog (`to_tool_record`) for docs, reviews and CI diffs.
