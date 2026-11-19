# Configuration and Policies

Background behaviour is configured in three places:
- `BackgroundTasksConfig`: prompt guidance, tool background, group defaults;
- `SessionLimits` on `SessionManager(limits=...)`: caps and timeouts;
- per call: `tasks.spawn` args, or a tool's `extra["background"]`.

## `BackgroundTasksConfig`

From `penguiflow.planner.models` (also exported by `penguiflow.planner`). `ReactPlanner(background_tasks=...)` reads the first three fields. Sessions read the rest, from the config set with `StreamingSession.configure_background_tasks(config)` or passed as `InProcessTaskService(background_config=...)`. The service applies it to a session each time it spawns there.

| Field | Default | Purpose |
|---|---|---|
| `enabled` | `False` | Adds background-task guidance to the system prompt. It does **not** turn the `tasks.*` tools on or off. |
| `include_prompt_guidance` | `True` | Whether `enabled` adds the guidance. |
| `allow_tool_background` | `False` | Lets tools with `extra["background"]["enabled"]` run as tasks. |
| `default_group_merge_strategy` | `"APPEND"` | Merge strategy of a new group that doesn't set one. `APPEND`, `REPLACE` or `HUMAN_GATED`, in any case. |
| `default_group_report` | `"all"` | Report strategy of a new group: `"all"`, `"any"` or `"none"`. |
| `auto_seal_groups_on_foreground_yield` | `True` | Seal the turn's open groups when a session foreground task ends. |
| `retain_turn_timeout_s` | `60.0` | Max wait for a retained group before the foreground continues. |
| `background_continuation_max_hops` | `2` | Continuation cycles after a retained wait times out. |
| `background_continuation_cooldown_s` | `0.0` | Delay between continuation cycles. |

The model uses `extra="forbid"`, so unknown fields raise `ValidationError`.

To turn background work off, leave `tasks.*` out of the catalog (or hide them with a tool policy) and keep `allow_tool_background=False`.

### Where the removed fields went

7.0.0 removed 18 fields that were never read:

| Removed field | Use instead |
|---|---|
| `max_concurrent_tasks`, `max_tasks_per_session`, `task_timeout_s` | `SessionLimits.max_concurrent_tasks`, `max_tasks_per_session` / `max_background_tasks`, `max_task_runtime_s` |
| `max_pending_steering` | `SessionLimits.steering_queue_size` (each task's inbox size) |
| `proactive_report_enabled`, `_strategies`, `_max_queued`, `_timeout_s`, `_max_hops`, `_fallback_notification` | the matching args (`enabled`, `strategies`, `max_queued`, `timeout_s`, `max_hops`, `fallback_notification`) of `setup_proactive_reporting()` or `StreamingSession.configure_proactive_reporting()` |
| `default_mode`, `default_merge_strategy`, `context_depth`, `propagate_on_cancel` | `tasks.spawn` args (`mode`, `merge_strategy`, `context_depth`, `propagate_on_cancel`), or the tool's `extra["background"]` (`mode`, `default_merge_strategy`) |
| `spawn_requires_confirmation` | No replacement. Veto spawns with a `SpawnGuard` (`InProcessTaskService(spawn_guard=...)`). |
| `group_timeout_s`, `group_partial_on_failure`, `max_tasks_per_group` | No replacement. A group with a failed task ends `failed`, and session limits cap the task count. |

## `SessionLimits`

From `penguiflow.sessions`. Every session the manager creates enforces these limits:

| Field | Default | Effect |
|---|---|---|
| `max_tasks_per_session` | `8` | Non-terminal tasks per session, foreground included. Spawning past it raises `task_limit_exceeded`. |
| `max_background_tasks` | `4` | Non-terminal background tasks. Spawning past it raises `background_task_limit_exceeded`. |
| `max_concurrent_tasks` | `3` | Tasks running at once. The rest wait (status update `queued`); `0` disables the cap. |
| `max_task_runtime_s` | `900` | Per-task wall clock. On expiry the task is FAILED with error `timeout`; `None` disables it. |
| `max_pending_patches` | `32` | Human-gated patches awaiting approval. Past it: `pending_patch_limit_exceeded`. |
| `update_queue_size` | `500` | Per-subscriber update queue. |
| `steering_queue_size` | `200` | Per-task steering inbox. |
| `max_steering_payload_bytes`, `max_steering_events_per_task` | | Steering payload size cap and duplicate-detection window. |

## Proactive reporting

Proactive reports tell the user about auto-merged results without waiting for their next message. They are off until you set them up on the session:

```python
from penguiflow.sessions import setup_proactive_reporting

session = await sessions.get_or_create("s1")
setup_proactive_reporting(
    session,
    make_foreground,                 # builds the planner that writes the report
    enabled=True,                    # False makes the call a no-op
    strategies=["APPEND", "REPLACE"],  # default; uppercase names
    max_queued=5,
    timeout_s=60.0,
    max_hops=2,
    fallback_notification=True,
)
```

What happens next:
- An `append` or `replace` task completes, and a report request is queued.
- The reporter waits while a session foreground task is running, then calls `make_foreground().run(...)` with the results in `llm_context["proactive_report"]`.
- It publishes the answer as a `RESULT` update with `proactive: True`, and removes the reported results from `background_results`.
- Grouped tasks with report strategy `"all"` produce one report for the whole group.
- Human-gated tasks get no report. A human-gated group publishes a `CHECKPOINT` (`kind: "group_approval"`) instead.
- A report run may spawn more tasks. `max_hops` bounds that chain: at 0 hops, `tasks.spawn` raises `proactive_hops_exhausted` and tool background is off.
- If the report times out or fails and `fallback_notification` is set, a `NOTIFICATION` update is published instead.

`StreamingSession.configure_proactive_reporting(generator=..., enabled=...)` takes the same options with your own async `generator(request: ProactiveReportRequest)`; its `timeout_s` default is `30.0`.

## Tool-initiated background spawns

```python
from pydantic import BaseModel

from penguiflow.catalog import tool
from penguiflow.planner import ToolContext


class ScrapeArgs(BaseModel):
    url: str


class ScrapeOut(BaseModel):
    text: str


@tool(
    desc="Scrape a site (slow)",
    side_effects="read",
    extra={
        "background": {
            "enabled": True,                    # required, must be True
            "mode": "job",                      # default "job"; or "subagent"
            "default_merge_strategy": "append",  # any case; default human_gated
            "notify_on_complete": True,         # default True
        }
    },
)
async def scrape_site(args: ScrapeArgs, ctx: ToolContext) -> ScrapeOut:
    ...
```

The call is spawned instead of run when the planner has `allow_tool_background=True` and `tool_context` has `task_service` and a string `session_id`. Otherwise it runs inline. The observation is a `BackgroundTaskHandle`: `{"task_id": ..., "status": "PENDING", "message": "spawned:job"}`.
- `mode="job"` calls `spawn_tool_job`, which needs a `tool_job_factory` on the service.
- `mode="subagent"` spawns a subagent asked to run the tool with the same args.

Group args are not available on this path.

## Prompt guidance

With `enabled=True` and `include_prompt_guidance=True`, the system prompt gets a `<background_tasks>` section: when to spawn, the `tasks.*` tools, merge rules, steering, context divergence. Set `include_prompt_guidance=False` if your own prompt already covers it.

## Telemetry

`TaskTelemetryEvent` (`penguiflow.sessions.telemetry`) goes to the `telemetry_sink` of `SessionManager` or `StreamingSession`:
- `event_type`: `task_spawned`, `task_completed`, `task_failed`, `task_cancelled`, `task_group_completed`, `task_group_failed`;
- `outcome`, `session_id`, `task_id` (the group id for group events), `parent_task_id`, `trace_id`, `task_type`, `status`, `mode`, `spawn_reason`, `duration_ms`, `extra` (`error`, `reason`, group counts).

Use `LoggingTaskTelemetrySink()` or implement `async def emit(self, event)`. Track spawn rate, the duration of each mode, failure and timeout rates, and the human-gated backlog (`len(session.pending_patches)`).

## Common mistakes

| Mistake | Consequence | Fix |
|---|---|---|
| Limits set on `BackgroundTasksConfig` | `ValidationError` | Use `SessionLimits` |
| `enabled=False` taken as "off" | Tools still spawn if they're in the catalog | Remove `tasks.*` from the catalog |
| Uppercase `merge_strategy` in `tasks.spawn` args | Args fail validation | Use `human_gated` / `append` / `replace` |
| Lowercase `strategies` for proactive reports | Reports never fire | Use `"APPEND"`, `"REPLACE"` |
| `append` from day one | Unreviewed results reach the user | Start `human_gated`; promote trusted tasks |
| No `tool_job_factory` | Job spawns raise `tool_background_unavailable` | Pass one (see `task-service-protocol.md`) |
