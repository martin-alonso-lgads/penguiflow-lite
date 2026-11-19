# Worker Patterns

## Pattern: planner per job (best isolation)

One planner instance per job, built from a factory. The worker loop:

```python
from __future__ import annotations

import asyncio

from penguiflow.planner import PlannerPause
from penguiflow.steering import SteeringCancelled, SteeringInbox

running: dict[str, SteeringInbox] = {}   # job id -> inbox, so shutdown and cancel can reach the run


async def worker(queue: Queue, shutting_down: asyncio.Event) -> None:
    while not shutting_down.is_set():
        job = await queue.fetch(timeout_s=1.0)
        if job is None:
            continue
        planner = make_planner()
        inbox = SteeringInbox()
        running[job.id] = inbox
        try:
            result = await asyncio.wait_for(
                planner.run(
                    job.query,
                    tool_context={"session_id": job.session_id, "tenant_id": job.tenant_id, "trace_id": f"job-{job.id}"},
                    steering=inbox,
                ),
                timeout=job.deadline_s,
            )
        except SteeringCancelled as exc:          # a CANCEL was pushed into the inbox
            if exc.reason == "shutdown":
                await queue.retry_job(job.id, delay_s=0, reason="shutdown")
            else:
                await queue.mark_failed(job.id, reason="cancelled", details=exc.reason)
        except TimeoutError:                       # the outer hard stop fired
            await queue.retry_job(job.id, delay_s=30, reason="deadline")
        else:
            if isinstance(result, PlannerPause):
                await queue.park(job.id, resume_token=result.resume_token, prompt=result.payload)
            elif result.reason == "answer_complete":
                await queue.mark_complete(job.id, result=result.payload)
            else:
                await queue.mark_failed(job.id, reason=result.reason, details=result.metadata.get("error"))
        finally:
            running.pop(job.id, None)
```

A `CANCEL` steering event makes `planner.run` raise `SteeringCancelled` (from `penguiflow.steering`). A running tool is cancelled at once; otherwise the run stops at the start of its next step, so an in-flight LLM call finishes first. Catch the exception explicitly: without the `except SteeringCancelled` branch, the exception escapes, kills the worker loop, and the job is never requeued.

Prefer this pattern when:
- jobs are independent;
- a crash should affect one job only;
- per-run callbacks (metrics, streaming) must not mix.

Building a planner is cheap compared with an LLM call. Reuse the LLM client, the catalog and the `StateStore` across planners.

## Pattern: sessions-backed service (shared runtime)

A `SessionManager` owns one `StreamingSession` per conversation. Each request becomes a session task, run by `PlannerTaskPipeline(planner_factory=make_planner)`:

```python
from penguiflow.sessions import PlannerTaskPipeline, SessionLimits, SessionManager, TaskResult

manager = SessionManager(state_store=store, limits=SessionLimits(max_concurrent_tasks=4))


async def handle(session_id: str, query: str) -> TaskResult:
    session = await manager.get_or_create(session_id)
    return await session.run_task(PlannerTaskPipeline(planner_factory=make_planner), query=query)
```

Notes:
- Put identity in the session context once: `session.update_context(tool_context={"tenant_id": ..., "user_id": ...})`. The pipeline adds `session_id`, `task_id` and `trace_id`.
- `session.cancel_task(task_id, reason=...)` makes `run_task` raise `SteeringCancelled`.
- A pause inside a session task doesn't return to you. The pipeline publishes a `CHECKPOINT` (`kind: "approval_required"`, with the `resume_token`) and waits in that process for an `APPROVE` or `REJECT` steering event. Session-task pauses therefore don't move between workers.

Prefer this pattern when:
- clients need live updates, reconnect and replay, or steering (`session.steer(...)`, `session.cancel_task(...)`);
- planners spawn background tasks;
- you want per-session concurrency limits and task telemetry out of the box.

## Resuming paused jobs

A `PlannerPause` is a checkpoint, not a failure. Store the `resume_token` with the job. When the human (or external system) answers, any worker can continue:

```python
planner = make_planner()   # with the same state_store as the run that paused
result = await planner.resume(
    token,
    user_input=answer,
    tool_context={"session_id": job.session_id, "tenant_id": job.tenant_id, "trace_id": f"job-{job.id}"},
)
```

This needs a `StateStore` with `save_planner_state` / `load_planner_state`, and the same `session_id` as the run. See [[penguiflow-hitl-pause-resume]]. The resumed segment continues the run's deadline budget; it does not get a fresh `deadline_s`.

## Queue contracts

Whatever queue you use (Redis, SQS, Kafka, DB-backed), the worker integration is easiest when the queue exposes:

```python
from typing import Any, Protocol


class Queue(Protocol):
    async def fetch(self, *, timeout_s: float | None) -> Job | None: ...
    async def mark_complete(self, job_id: str, *, result: Any) -> None: ...
    async def retry_job(self, job_id: str, *, delay_s: float, reason: str) -> None: ...
    async def mark_failed(self, job_id: str, *, reason: str, details: Any | None) -> None: ...
    async def park(self, job_id: str, *, resume_token: str, prompt: Any) -> None: ...   # HITL pauses
```

Required invariants:
- **At-least-once delivery.** Build for idempotent tools.
- **A visibility timeout** larger than your worst-case run (`deadline_s` plus headroom), so the queue doesn't redeliver mid-run.
- **A dead-letter queue** for terminally failed jobs (`no_path`, repeated errors).

## Per-job correlation

Put a job-derived id in `tool_context`, for example `trace_id=f"job-{job.id}"`:
- the planner persists trajectories and planner events under it, when the store supports them (`save_trajectory`, `save_planner_event`);
- tools can use it as an idempotency key, and A2A bindings and artifacts record it;
- your logs can be joined on it.

Don't reuse ids across retries. Generate a fresh one, and keep a parent reference if you need the link.

## Idempotency

At-least-once delivery plus retries means side effects can repeat. Strategies:

### Idempotency keys
Derive a key from the job id and the tool call (`f"{trace_id}:{tool}:{hash(args)}"`) and pass it to external services. Most APIs (Stripe, SQS, AWS) support this natively.

### Plan/commit split
- A **plan tool** is pure and produces an action description.
- A **commit tool** is gated by an idempotency check, a guardrail, or HITL approval.

### Atomic write-and-mark
```python
from penguiflow.catalog import tool
from penguiflow.planner import ToolContext


@tool(desc="Commit the order", side_effects="write")
async def commit_order(args: CommitArgs, ctx: ToolContext) -> CommitResult:
    key = f"{ctx.tool_context['trace_id']}:{args.order_id}"
    async with db.transaction():
        if existing := await find_commit(key):
            return existing
        result = await do_write(args)
        await mark_committed(key, result)
        return result
```

## Time budgets

Three layers:
1. **Per tool**: `asyncio.timeout(...)` inside local tools; `ExternalToolConfig.timeout_s` for ToolNode tools. Tight (seconds).
2. **Per LLM call**: `llm_timeout_s` / `llm_max_retries` on the planner.
3. **End to end**: the planner's `deadline_s`, plus an outer `asyncio.wait_for` as a hard stop. Loose (tens of seconds to minutes).
   - When `deadline_s` runs out, the planner finishes with `reason="budget_exhausted"` and the last observation as payload.
   - The budget covers all of a run's segments: a resume continues with the time that was left at the pause, and the clock doesn't run while paused.

On an end-to-end timeout:
1. Push a steering `CANCEL` (cooperative), or let `wait_for` cancel the run (hard).
2. Call `retry_job` (if transient) or `mark_failed`.

## Graceful shutdown

On `SIGTERM`:
1. Set `shutting_down`, so workers stop fetching new jobs; the health check returns 503.
2. Wait for in-flight runs, up to the drain deadline.
3. Push `SteeringEventType.CANCEL` with reason `"shutdown"` into the inboxes of runs still going. Their workers catch `SteeringCancelled` and requeue the jobs.
4. After a short grace period, cancel whatever is left.
5. `await a2a_service.stop()`, then close sessions and LLM clients.

```python
import asyncio

from penguiflow.steering import SteeringEvent, SteeringEventType


async def graceful_shutdown(workers: set[asyncio.Task[None]], *, drain_s: float = 30, grace_s: float = 5) -> None:
    if not workers:                                   # asyncio.wait() rejects an empty set
        return
    _done, pending = await asyncio.wait(workers, timeout=drain_s)        # 2. drain
    for job_id, inbox in list(running.items()):                          # 3. cooperative cancel
        await inbox.push(
            SteeringEvent(session_id=job_id, task_id=job_id, event_type=SteeringEventType.CANCEL, payload={"reason": "shutdown"})
        )
    if pending:
        _done, pending = await asyncio.wait(pending, timeout=grace_s)    # workers requeue and exit
    for task in pending:                                                 # 4. hard stop
        task.cancel()
```

A hard-cancelled job is not requeued by the worker; the queue's visibility timeout redelivers it. Paused runs need nothing: their state is already in the `StateStore`.

## Tenant isolation

For multi-tenant workers:
- Put `tenant_id` and `user_id` in `tool_context` on every run. Memory isolation and scoped artifacts read these keys; a `tenant` key alone leaves tenants that share a `session_id` sharing memory. Tools must enforce them too.
- Rate-limit per tenant at the queue level, so one tenant doesn't starve the others.
- Use tenant-scoped credentials; don't share API tokens across tenants.
- Use tenant-scoped `session_id`s and state-store keys (`tenant:<id>:...`).
- Use tenant-scoped artifact retrieval, with authz on the retrieval endpoint.

A single worker can serve many tenants safely if isolation is enforced at every layer.

## Health checks

Expose `/health`, returning:
- 200 while the worker is processing;
- 503 once `shutting_down` is set, so the load balancer drains it;
- 503 if a critical dependency (queue, state store, LLM gateway) is unreachable.

Keep probes cheap; don't call the LLM from a health check.

## Anti-patterns

- **One planner instance shared across concurrent jobs with per-job callbacks**: events and metrics mix.
- **No steering inbox**: you can't cancel a run cooperatively.
- **Not catching `SteeringCancelled`**: one cancel kills the worker loop and loses the job.
- **Synchronous I/O inside a tool**: it blocks the event loop for every run on the worker.
- **A DB/HTTP client per job**: connection churn; use pools.
- **Huge tool results**: push them to the artifact store and return a ref.
- **`asyncio.create_task` without tracking**: fire-and-forget work you can't wait for or cancel.
