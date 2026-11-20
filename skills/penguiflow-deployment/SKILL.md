---
name: penguiflow-deployment
description: Take a ReactPlanner agent from local dev to a production service or worker fleet — pick a deployment shape (embedded HTTP/WebSocket service, sessions-backed service, queue-backed worker pool, A2A specialist), build planners from a factory, set run limits (`max_iters`, `deadline_s`, `hop_budget`, `token_budget`, `llm_timeout_s`, `llm_max_retries`, `SessionLimits`, ToolNode `timeout_s`/`max_concurrency`), wire durability via `StateStore` capabilities (planner pause state, memory, tasks, bindings), expose or call agents over A2A, and shut down gracefully with steering cancellation (catching `SteeringCancelled` and requeueing). Use when a user says "deploy to production", "worker integration", "multi-worker", "queue-backed", "scale the agent", "A2A deployment", or asks how to size budgets/timeouts/concurrency.
---

# PenguiFlow Deployment

## When to use
- Moving a planner agent from a script to a long-lived production service.
- Building a queue-backed job worker that runs planner tasks.
- Sizing budgets, timeouts and concurrency.
- Scaling to multiple workers / machines (durable pause/resume, memory, bindings).

## When NOT to use
- Configuring planner features (tools, skills, guardrails, memory) → use [[penguiflow-reactplanner-config]].
- Persistence semantics in depth → use [[penguiflow-statestore]].
- A2A wire contract → use [[penguiflow-a2a-integration]].
- Observability dashboards/alerts → use [[penguiflow-observability]].
- Client streaming (SSE/WebSocket) → use [[penguiflow-streaming]].

## Hard boundaries
This skill is the **production runbook**: shapes, sizing, lifecycle, durability, distributed contracts. Persistence implementation lives in statestore. Observability lives in observability. A2A bindings live in a2a-integration. Don't duplicate.

## Workflow

### 1) Pick a deployment shape
| Shape | When | Trade-offs |
|---|---|---|
| Embedded in an HTTP/WS service (FastAPI) | Synchronous user-facing chat | Simplest; tied to service scaling |
| Sessions-backed service (`SessionManager` + `StreamingSession`) | Chat with background tasks, steering, reconnect/replay | More moving parts; needs a session state store for multi-worker |
| Queue-backed worker pool | Async jobs, batch | Scales horizontally; needs a queue and durable state |
| A2A specialist (`A2AService(planner, ...)`) | Other agents call this one | Standard protocol; one service per specialist |

Start embedded or sessions-backed. Add workers when a single process can't keep up.

### 2) Build planners from a factory
```python
from penguiflow.planner import PlannerEventCallback, ReactPlanner


def make_planner(*, event_callback: PlannerEventCallback | None = None) -> ReactPlanner:
    return ReactPlanner(
        llm_client=shared_llm_client,     # reuse connections across planners
        catalog=catalog,
        state_store=state_store,          # durable pause/resume + memory
        max_iters=8,
        deadline_s=120,
        event_callback=event_callback,
    )
```
A planner instance holds per-run callbacks and per-session runtimes. A factory gives each request (or each session task, via `PlannerTaskPipeline(planner_factory=make_planner)`) a clean instance. Pass `tool_context={"session_id": ..., "tenant_id": ..., "user_id": ...}` on every `run()` and `resume()`:
- `session_id` serializes calls per conversation and keys short-term memory;
- `tenant_id` and `user_id` are the keys memory isolation and scoped artifacts read. A bare `tenant` key doesn't isolate memory.

### 3) Set run limits deliberately
| Knob | Where | Default | Production start |
|---|---|---|---|
| `max_iters` | `ReactPlanner` | 8 | 6-12; raise only with evidence |
| `deadline_s` | `ReactPlanner` | None | always set for user-facing work; one budget shared by all resume segments |
| `hop_budget` | `ReactPlanner` | None | set for cost control; ends the run with `budget_exhausted` |
| `token_budget` | `ReactPlanner` | None | compresses the trajectory when the prompt grows past it; never ends a run |
| `llm_timeout_s` / `llm_max_retries` | `ReactPlanner` | 360 / 3 | 60-120 / 2-3 for chat |
| `absolute_max_parallel` | `ReactPlanner` | 50 | lower to protect downstreams |
| `SessionLimits(max_concurrent_tasks, max_task_runtime_s, update_queue_size, ...)` | `SessionManager` | 3 / 900 / 500 | per workload |
| ToolNode `timeout_s` / `max_concurrency` | `ExternalToolConfig` | 30 / 10 | tune per remote tool |

Local `@tool` functions have no runtime-enforced timeout — wrap slow I/O in `asyncio.timeout(...)` inside the tool. Sizing recipes in `references/sizing-and-limits.md`.

### 4) Manage lifecycle in your worker
```python
from penguiflow.planner import PlannerPause
from penguiflow.steering import SteeringCancelled, SteeringInbox


async def handle(job: Job) -> None:
    planner = make_planner()
    inbox = SteeringInbox()
    running[job.id] = inbox                        # so shutdown/cancel can reach it
    try:
        result = await planner.run(
            job.query,
            tool_context={"session_id": job.session_id, "tenant_id": job.tenant_id},
            steering=inbox,
        )
    except SteeringCancelled:                      # a CANCEL was pushed into the inbox
        await queue.retry_job(job.id, delay_s=0, reason="cancelled")
    else:
        if isinstance(result, PlannerPause):
            await queue.park(job.id, resume_token=result.resume_token, prompt=result.payload)
        else:
            await queue.mark_complete(job.id, result=result.payload)
    finally:
        running.pop(job.id, None)
```
A `CANCEL` steering event makes `planner.run` raise `SteeringCancelled`. Catch it, or it escapes the worker and the job is lost. Patterns (per-job planner vs shared planner with sessions, at-least-once delivery, dead letters) are in `references/worker-patterns.md`.

### 5) Wire durability (when you need it)
Pass one `StateStore` to the planner (and to `SessionManager(state_store=...)`). Capabilities are duck-typed:
- `save_planner_state` / `load_planner_state` — resume a `PlannerPause` on any worker ([[penguiflow-hitl-pause-resume]]).
- `save_memory_state` / `load_memory_state` — short-term memory across workers ([[penguiflow-memory]]).
- `save_task` / `save_update` / `list_updates` — session tasks and replayable updates ([[penguiflow-background-tasks]]).
- `save_remote_binding` / `find_binding` — A2A conversation continuity.

Full contract in [[penguiflow-statestore]].

### 6) Distribute across agents (when you need it)
- **Expose** a specialist: `A2AService(planner, agent_card=..., config=A2AConfig(agent_url=...))` + `install_a2a_http(app, service)` or the gRPC binding.
- **Call** specialists: `A2AAgentToolset` / `A2ARouterToolset` tools in the manager's catalog.
- **Custom transports**: implement `penguiflow.remote.RemoteTransport` (`send` / `stream` / `cancel`, optionally the task operations) and hand it to `A2AAgentToolset(transport=...)`.
- Extras: `penguiflow-lite[a2a-server]`, `penguiflow-lite[a2a-client]`, `penguiflow-lite[a2a-grpc]`.
- A2A tasks that pause (`input-required`) can only be resumed in the process that paused them. `A2AService` keeps the task-to-token map in memory, so route follow-ups for a paused task to the same replica.

See `references/distributed-primitives.md` and [[penguiflow-a2a-integration]].

### 7) Harden tool execution (planner + ToolNode)
Tools are an untrusted boundary:
- Set `max_concurrency` and `timeout_s` per `ExternalToolConfig`; use `tool_filter` allowlists.
- Restrict tools per request with `ToolPolicy` / `ToolVisibilityPolicy`.
- Gate write/external tools behind HITL ([[penguiflow-hitl-pause-resume]]) or guardrails ([[penguiflow-guardrails]]).
- Secrets in env/secret managers or `tool_context`, never in `llm_context`.

See [[penguiflow-mcp-integration]].

### 8) Observe + alert
Use `configure_logging(structured=True)`, a metrics `event_callback`, and LLM tracing. `PENGUIFLOW_LLM_TRACING=mlflow|log` takes effect only on the native LLM layer, so it needs `ReactPlanner(llm=..., use_native_llm=True)` (the default is `False`). Alert on failure-reason share, tool error spikes, latency SLOs and cost. See [[penguiflow-observability]].

### 9) Graceful shutdown
On `SIGTERM`:
1. Stop accepting new work.
2. Wait for in-flight runs, up to the drain deadline.
3. Push `SteeringEventType.CANCEL` (reason `"shutdown"`) into the inboxes of runs still going, or call `session.cancel_task(...)`. Their workers catch `SteeringCancelled` and requeue the jobs.
4. After a short grace period, cancel what's left.
5. `await a2a_service.stop()`, close clients, and exit.

Paused runs need nothing: their state is already in the `StateStore`. Most platforms give 30-60 s before SIGKILL. Code in `references/worker-patterns.md`.

## Troubleshooting (fast checks)
- **Runs never end / cost spikes** — no `deadline_s` / `hop_budget`; set them. `token_budget` only compresses the prompt and won't stop a run.
- **Worker dies after a cancel** — the loop doesn't catch `SteeringCancelled` from `planner.run`.
- **Resumed run hits its deadline at once** — `deadline_s` is one budget across pause/resume segments; size it for the whole run.
- **Random latency spikes** — remote tools without `timeout_s`, or `llm_timeout_s` left at 360 s.
- **Tokens/events leak across users** — one planner instance shared with a per-request `event_callback`; use a factory or sessions.
- **Resume fails on another worker** — the `StateStore` lacks `save_planner_state` / `load_planner_state`, the planner wasn't given the store, or the resume passed a different `session_id`. A2A pauses never move between replicas.
- **Memory resets between requests** — no `session_id` in `tool_context`, or memory isn't persisted to the store.
- **Retry storms across workers** — at-least-once delivery + non-idempotent write tools; add idempotency keys or gate writes behind HITL.
- **Worker hangs on shutdown** — runs not cancelled through steering; keep a registry of inboxes.
- **One user DoSes the worker** — no `SessionLimits.max_concurrent_tasks` / per-tenant quotas.

## Worked examples
- `examples/planner_enterprise_agent/` — production-shaped planner agent (config, telemetry, streaming).
- `examples/a2a_grpc_server/main.py` — a planner served as an A2A specialist.
- `examples/react_pause_resume/main.py` — pause/resume flow to adapt for durable workers.

## References (load only as needed)
- `references/worker-patterns.md` — per-job vs session-backed workers, queue contracts (at-least-once, retry, dead-letter), resume tokens, graceful shutdown.
- `references/sizing-and-limits.md` — budgets, timeouts, retries, concurrency sizing by workload type; sample SLOs.
- `references/distributed-primitives.md` — `StateStore` capabilities, `RemoteTransport`, A2A handoff, recovery semantics, idempotency rules.
