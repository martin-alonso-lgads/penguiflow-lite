---
name: penguiflow-background-tasks
description: Run background work from a PenguiFlow ReactPlanner — add the `tasks.*` tools with `build_task_tool_specs()`, back them with `InProcessTaskService(sessions=SessionManager(limits=SessionLimits(...)), planner_factory=..., tool_job_factory=...)` (or your own `TaskService`), pass `tool_context["task_service"]` and `session_id`, spawn subagents (`query`) or tool jobs (`tool_name` + `tool_args`), pick a merge strategy (`human_gated`/`append`/`replace`, any case), group tasks, let tools opt in with `extra["background"]`, and configure `BackgroundTasksConfig` (prompt guidance, tool background, group defaults). Use when a user says "background task", "spawn a subagent", "async tool", "task groups", "proactive report", or names `BackgroundTasksConfig`, `tasks.spawn`, `task.subagent`, `TaskService`.
---

# PenguiFlow Background Tasks

## When to use
- The foreground agent should stay responsive while a long task runs.
- You need parallel work beyond a single `parallel` planner step.
- A slow tool (scrape, ETL) should run as a job and report back later.
- Several related tasks should report back together (task groups).

## When NOT to use
- The work is fast (<2 s): call the tool in the foreground.
- You need a durable, cross-process queue: `InProcessTaskService` runs tasks as `asyncio` tasks in one process; implement your own `TaskService` for anything else.
- You want to stream interim output → [[penguiflow-streaming]].
- One planner step can fan out → planner `parallel` (see [[penguiflow-reactplanner-config]]).

## Hard boundaries
This skill covers planner-driven background tasks: spawn, observe, merge. The session runtime (`SessionManager`, `StreamingSession`) executes them. Pausing a run for a human belongs to [[penguiflow-hitl-pause-resume]].

## Workflow

### 1) Build the task service
```python
from penguiflow.planner import ReactPlanner
from penguiflow.planner.models import BackgroundTasksConfig
from penguiflow.sessions import InProcessTaskService, SessionLimits, SessionManager

worker_tools = ...  # your NodeSpecs, e.g. build_catalog(nodes, registry)
background = BackgroundTasksConfig(enabled=True)  # enabled adds prompt guidance only


def make_subagent() -> ReactPlanner:
    return ReactPlanner(llm="gpt-4o-mini", catalog=worker_tools)  # no tasks.* tools


sessions = SessionManager(limits=SessionLimits(max_background_tasks=4, max_task_runtime_s=300))
task_service = InProcessTaskService(
    sessions=sessions,
    planner_factory=make_subagent,    # builds each subagent
    tool_job_factory=None,            # set it to allow mode="job"
    background_config=background,     # group defaults for new groups
)
```
Limits live in `SessionLimits`, not in `BackgroundTasksConfig`. `BackgroundTasksConfig` rejects unknown fields, so a config that still sets a removed field (`max_concurrent_tasks`, `task_timeout_s`, `proactive_report_*`, …) raises `ValidationError`. See `references/config-and-policies.md` for where each removed field went.

### 2) Give the foreground planner the `tasks.*` tools
```python
from penguiflow.sessions.task_tools import build_task_tool_specs

foreground = ReactPlanner(
    llm="gpt-4o-mini",
    catalog=[*worker_tools, *build_task_tool_specs()],  # both are NodeSpec lists
    background_tasks=background,
)
result = await foreground.run(
    "Research segments A and B in the background",
    tool_context={"session_id": "s1", "task_service": task_service},
)
```
Tools: `tasks.spawn`, `tasks.list`, `tasks.get`, `tasks.cancel`, `tasks.prioritize`, `tasks.apply_patch`, `tasks.acknowledge_background`, and for groups `tasks.seal_group`, `tasks.cancel_group`, `tasks.apply_group`, `tasks.list_groups`, `tasks.get_group`.

Every call needs `tool_context["task_service"]` and `tool_context["session_id"]`. Subagent runs get `is_subagent=True` in their tool context, and every `tasks.*` tool then fails with `subagent_task_management_disabled`. Still leave `tasks.*` out of the subagent catalog so the model doesn't waste steps on them.

A subagent starts from the **session's** context (`session.update_context(...)`), not from the foreground run's arguments. It gets a JSON-only copy of that `tool_context`, so clients and the task service are dropped. Subagent tools get their clients from the planner factory.

### 3) Spawn: subagent or job
| Mode | `tasks.spawn` args | What runs |
|---|---|---|
| `subagent` (default) | `query` | A planner from `planner_factory`. |
| `job` | `tool_name` + `tool_args` (both required) | One tool call from `tool_job_factory(tool_name, tool_args)`. No pause/resume inside a job. |

Other args: `priority`, `merge_strategy`, `propagate_on_cancel` (`cascade`/`isolate`), `notify_on_complete`, `context_depth` (`full`/`summary`/`none`, subagents only), `task_id`/`idempotency_key`, and the group args. The `task.subagent` / `task.tool` action opcodes are rewritten into `tasks.spawn` calls.

### 4) Pick a merge strategy (per task)
| `merge_strategy` | What happens when the task completes |
|---|---|
| `human_gated` (default) | The result is held as a pending patch and a `CHECKPOINT` update (`kind: "context_patch"`, `patch_id`) is published. Approve with `tasks.apply_patch(patch_id=..., action="apply")`, `task_service.apply_patch(...)`, or an `APPROVE` steering event whose payload carries the `patch_id`. |
| `append` / `replace` | The result goes straight into the session's `background_results`. Both behave the same: the latest result per task wins. |

Merged results are in `session.get_background_results()`. Strategy names are accepted in any case (`"APPEND"`, `"append"`); the enum values are lowercase. The planner shows merged results to the foreground LLM as a read-only `<read_only_background_results>` system message (summary, facts and artifact references, newest 20; the raw payload stays out, and `tasks.get(task_id=..., include_result=True)` returns it). Proactive reporting tells the user about them (`references/config-and-policies.md`). Call `tasks.acknowledge_background` to drop consumed results, which also removes them from the prompt.

### 5) Group related tasks
Spawn with the same `group="market_q3"` in one turn, seal with `group_sealed=True` on the last spawn or `tasks.seal_group(group=...)`, and approve a human-gated group with `tasks.apply_group(group_id=..., action="apply")`. A new group takes its merge and report strategies from `background_config` unless the spawn sets `group_merge_strategy` / `group_report`. Details: `references/groups-and-merge.md`.

### 6) Let a tool run in the background
A tool call becomes a background task, instead of running inline, when all of these hold:
- the planner has `background_tasks.allow_tool_background=True`;
- the tool has `extra={"background": {"enabled": True, ...}}`;
- `tool_context` has `task_service` and a string `session_id`.

The call then returns `{"task_id": ..., "status": "PENDING", "message": "spawned:job"}`. Keys and defaults are in `references/config-and-policies.md`.

### 7) Limits and telemetry
`SessionLimits` (on `SessionManager(limits=...)`) defaults: 8 active tasks per session, 4 background, 3 running at once (the rest wait), 900 s per task, 32 pending patches. Lifecycle events are `TaskTelemetryEvent`s, sent to `SessionManager(telemetry_sink=...)`, for example `LoggingTaskTelemetrySink()`. See [[penguiflow-observability]].

## Troubleshooting (fast checks)
- **`task_service_unavailable`**: no `tool_context["task_service"]` on this run.
- **`session_id_missing`**: no `tool_context["session_id"]`.
- **`background_tasks_unavailable` / `tool_background_unavailable`**: the service has no subagent planner factory, or no `tool_job_factory`.
- **`task_limit_exceeded` / `background_task_limit_exceeded` / `pending_patch_limit_exceeded`**: a `SessionLimits` cap was hit.
- **Task FAILED with error `timeout`**: it ran longer than `SessionLimits.max_task_runtime_s`.
- **Spawn fails validation**: a job without `tool_args`, an unknown `merge_strategy`, or `retain_turn=True` with a human-gated strategy.
- **`ValidationError` building `BackgroundTasksConfig`**: a removed field; move it (see `references/config-and-policies.md`).
- **Results pile up**: tasks are `human_gated` and nobody approves them. Build the approval UI, or use `append` for trusted tasks.
- **`tasks.apply_group` returns `group_not_complete`**: the group isn't sealed, or some tasks are still running.
- **Tool background opt-in ignored**: check `allow_tool_background`, `extra["background"]["enabled"] is True`, and both `tool_context` keys.

## Worked examples (tests)
- `tests/test_task_service.py`: `InProcessTaskService` spawn, idempotency, patches, groups, `retain_turn`.
- `tests/test_task_tools.py`: the `tasks.*` tool surface.
- `tests/test_tool_background_mode.py`: tools that opt in with `extra["background"]`.
- `tests/test_proactive_reports.py`: proactive reports and task groups.
- `tests/test_task_lifecycle.py`, `tests/test_task_registry.py`, `tests/test_task_registry_and_policy.py`: task states, cancellation and control policy.
- `tests/sessions/`: the session pipeline, projections and proactive reporting.

## References (load only as needed)
- `references/config-and-policies.md`: `BackgroundTasksConfig` fields, where the removed fields went, `SessionLimits`, proactive reporting, tool-initiated spawns.
- `references/task-service-protocol.md`: the `TaskService` protocol, `InProcessTaskService`, spawn guards, tool jobs, persistence and recovery.
- `references/groups-and-merge.md`: task groups, merge strategies, `ContextPatch`, approval workflows.
