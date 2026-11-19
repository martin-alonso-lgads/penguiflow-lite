# The `TaskService` Protocol

The `tasks.*` tools call whatever object is in `tool_context["task_service"]`. The contract is `penguiflow.sessions.task_service.TaskService`, a structural `Protocol`. `InProcessTaskService` is the shipped implementation.

## Methods

All methods are `async` and keyword-only. Every one takes `session_id`.

| Method | Other args | Returns |
|---|---|---|
| `spawn` | `query`, `parent_task_id`, `priority`, `merge_strategy`, `propagate_on_cancel`, `notify_on_complete`, `context_depth`, `task_id`, `idempotency_key`, `context_tool_context`, group args | `TaskSpawnResult` |
| `spawn_tool_job` | `tool_name`, `tool_args`, the same args minus `query` / `context_depth` / `idempotency_key` | `TaskSpawnResult` |
| `list` | `status: TaskStatus \| None` | `list[TaskSummary]` |
| `get` | `task_id`, `include_result` | `TaskDetails \| None` |
| `cancel` | `task_id`, `reason` | `bool` |
| `prioritize` | `task_id`, `priority` | `bool` |
| `apply_patch` | `patch_id`, `action` (`"apply"`/`"reject"`), `strategy` | `bool` |
| `acknowledge_background` | `task_ids` | `int` (results removed) |
| `seal_group` | `group_id` or `group_name`, `turn_id` | `dict` (`ok`, `group_id`, `status`, `sealed_task_count`, or `error`) |
| `cancel_group` | `group_id`, `reason`, `propagate_on_cancel` | `dict` (`ok`, `cancelled_task_count`, `total_tasks`) |
| `apply_group` | `group_id`, `action`, `strategy` | `dict` (`ok`, `applied_patch_count`, `rejected_patch_count`, `total_patches`, or `error`) |
| `list_groups` | `status` (`open`/`sealed`/`complete`/`failed`) | `list[TaskGroup]` |
| `get_group` | `group_id` or `group_name`, `turn_id` | `TaskGroup \| None` |

The group args of `spawn` and `spawn_tool_job` are `group`, `group_id`, `group_sealed`, `retain_turn`, `group_merge_strategy`, `group_report` and `turn_id`. `merge_strategy` is a `MergeStrategy` (`penguiflow.sessions.models`); its values are lowercase.

Result models (`penguiflow.sessions.task_service`):
- `TaskSpawnResult`: `task_id`, `session_id`, `status`, `group_id`, `group`, `retained`, `group_completion`.
- `TaskSummary`: `task_id`, `session_id`, `status`, `task_type`, `priority`, `description`, `progress`, `error`, `has_result`.
- `TaskDetails`: a `TaskSummary` plus `result_digest` (filled when `include_result=True`), `spawned_from_task_id` and `spawned_from_event_id`.

`TaskStatus` is `PENDING`, `RUNNING`, `PAUSED`, `COMPLETE`, `FAILED` or `CANCELLED`.

A custom service only needs the methods your agents call. The tool-initiated path (`extra["background"]`) calls only `spawn` and `spawn_tool_job`.

## `InProcessTaskService`

```python
from typing import Any

from penguiflow.catalog import NodeSpec
from penguiflow.planner import ReactPlanner
from penguiflow.planner.models import BackgroundTasksConfig
from penguiflow.sessions import (
    InProcessTaskService,
    SessionLimits,
    SessionManager,
    SpawnDecision,
    SpawnRequest,
    TaskPipeline,
)
from penguiflow.sessions.telemetry import LoggingTaskTelemetrySink
from penguiflow.sessions.tool_jobs import build_tool_job_pipeline

worker_tools: list[NodeSpec] = ...  # e.g. build_catalog(nodes, registry)
specs = {spec.name: spec for spec in worker_tools}


def make_subagent() -> ReactPlanner:
    return ReactPlanner(llm="gpt-4o-mini", catalog=worker_tools)


def tool_job_factory(tool_name: str, tool_args: Any) -> TaskPipeline:
    return build_tool_job_pipeline(spec=specs[tool_name], args_payload=dict(tool_args))


class TenantQuota:
    async def decide(self, request: SpawnRequest) -> SpawnDecision:
        if request.mode == "job" and request.tool_name not in specs:
            return SpawnDecision(allowed=False, reason="unknown_tool")
        return SpawnDecision(allowed=True)


task_service = InProcessTaskService(
    sessions=SessionManager(
        limits=SessionLimits(max_background_tasks=4, max_task_runtime_s=300),
        state_store=None,                         # or a StateStore / SessionStateStore
        telemetry_sink=LoggingTaskTelemetrySink(),
    ),
    planner_factory=make_subagent,                # required (may be None)
    subagent_planner_factory=None,                # overrides planner_factory for subagents
    tool_job_factory=tool_job_factory,            # needed for mode="job"
    spawn_guard=TenantQuota(),                    # default allows everything
    background_config=BackgroundTasksConfig(default_group_merge_strategy="human_gated"),
)
```

What a spawn does:
1. Asks `spawn_guard.decide(SpawnRequest(...))`. A denial raises `RuntimeError(reason or "spawn_blocked")`.
2. Gets or creates the `StreamingSession`, and applies `background_config` to it.
3. Returns the existing task when an `idempotency_key` or `task_id` repeats.
4. Snapshots the session context. `tool_context` keeps JSON values only, and `context_depth` trims `llm_context`.
5. Resolves or creates the group, then calls `session.spawn_task(...)`. That checks `SessionLimits`, records the task as `PENDING` and starts an `asyncio` task. The task waits for a concurrency slot, runs, and ends `COMPLETE`, `FAILED` or `CANCELLED`.
6. Joins the group, seals it if asked, and waits for it when `retain_turn=True`.

Without a subagent factory, `spawn` raises `background_tasks_unavailable`. Without a `tool_job_factory`, `spawn_tool_job` raises `tool_background_unavailable`.

### Subagent runs

Each subagent is a `PlannerTaskPipeline` run. Its `tool_context` gets `session_id="<session>:<task_id>"`, `parent_session_id`, `is_subagent=True`, `trace_id` and `task_id`, so its memory is separate from the foreground's.

If a subagent pauses (HITL), the task becomes `PAUSED` and the session publishes a `CHECKPOINT` with `kind: "approval_required"` and the `resume_token`. Approve with an `APPROVE` steering event whose payload carries `{"resume_token": ..., "decision": ...}`. `REJECT` cancels the task.

### Tool jobs

`build_tool_job_pipeline(spec=..., args_payload=...)` validates the args, calls the tool once with a `ToolJobContext`, and returns the output as the task result. It also extracts artifacts. Jobs cannot pause.

## Cancellation

`tasks.cancel` / `service.cancel` send a `CANCEL` steering event. The session marks the task `CANCELLED`, cancels its `asyncio` task and cascades to its children. A child spawned with `propagate_on_cancel="isolate"` keeps running. `tasks.cancel_group` cancels the group's pending tasks when `propagate_on_cancel="cascade"`.

## Persistence and recovery

With `SessionManager(state_store=...)`, the session saves task records, updates and steering events. It accepts a `SessionStateStore` or a core `StateStore`, which is wrapped in `StateStoreSessionAdapter`. Store errors are not swallowed: a failing task save fails the spawn or status change that triggered it.

On `get_or_create`, a session reloads its task records. Nothing else survives a restart: running tasks, pending patches, groups, `background_results` and the idempotency map are in memory. After a restart, list the tasks that were `PENDING`/`RUNNING`, then re-spawn them (if idempotent) or mark them failed.

## Custom services

Implement the protocol over your own queue or database when tasks must outlive a process or spread across workers:
- `spawn` / `spawn_tool_job` enqueue a job and return a `TaskSpawnResult` with status `PENDING`.
- Workers run the subagent or the tool, and write the status and the result digest back.
- `list` / `get` / the group methods read your store. `cancel` signals the worker.

Keep the return types: the `tasks.*` tools validate them (`TaskSummary`, `TaskDetails`, `TaskGroup`).

## Anti-patterns

- CPU-bound work in in-process tasks, which blocks the event loop. Use a custom service with a worker pool.
- Relying on in-process groups or pending patches across restarts.
- Non-idempotent jobs re-spawned after recovery without an idempotency key.
- Passing clients through the session `tool_context` to subagents: they are dropped. Close over them in the factory.
