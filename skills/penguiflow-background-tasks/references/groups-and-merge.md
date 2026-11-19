# Task Groups and Merge Strategies

## Why groups

One foreground turn often spawns related tasks: "research these 3 segments", "refresh these 5 reports". Without a group, each task reports on its own, and the user sees a stream of partial updates. A group reports once, and its human-gated results can be approved together.

## Group lifecycle

```
open ──seal──▶ sealed ──all tasks terminal──▶ complete   (no task failed)
                                          └─▶ failed     (any task failed or was cancelled)
```

- **Create or join.** `tasks.spawn(group="market_q3", ...)` joins an **open** group with that name created in the same turn, or creates one. The turn comes from `tool_context["turn_id"]`, so pass a fresh `turn_id` on each user turn. `group_id=...` joins that exact group, which must be open (otherwise `group_not_joinable`).
- **Strategies are fixed at creation.** `group_merge_strategy` and `group_report` on the creating spawn. Otherwise `BackgroundTasksConfig.default_group_merge_strategy` (default `APPEND`) and `default_group_report` (default `"all"`), from the session's config.
- **Seal** in any of these ways. A sealed group accepts no more tasks.
  - `group_sealed=True` on a spawn;
  - `tasks.seal_group(group_id=...)` or `tasks.seal_group(group=...)`;
  - `retain_turn=True`;
  - automatically when a session foreground task ends (`auto_seal_groups_on_foreground_yield`). A direct `planner.run(...)` does not trigger this, so seal explicitly or call `await session.on_foreground_yield()` afterwards.
- **Complete.** When the group is sealed and every task is terminal, it becomes `complete` or `failed`. A `task_group_completed` / `task_group_failed` telemetry event follows, and with report strategy `"all"` one group report is queued (when proactive reporting is set up).

Report strategies: `"all"` (one report for the group), `"any"` (a report per task), `"none"` (no report; the agent polls).

## What the group's merge strategy controls

Each task's result still merges by that task's own `merge_strategy` (default `human_gated` in `tasks.spawn`). The group's strategy decides:
- **The report.** A `human_gated` group publishes a `CHECKPOINT` (`kind: "group_approval"`) instead of a synthesized report.
- **`retain_turn`.** Waiting happens only for `append`/`replace` groups. The spawn args reject `retain_turn=True` when the strategy is `human_gated`.

Human-gated patches from a group's tasks are collected in `TaskGroup.patches`. `tasks.apply_group(group_id=..., action="apply" | "reject", strategy=None)` handles them all at once, and only once the group is complete (otherwise `group_not_complete`).

## Merge strategies

Values are lowercase (`MergeStrategy.HUMAN_GATED == "human_gated"`).

### `human_gated` (default)
The patch waits in `session.pending_patches` as a `PendingContextPatch`: `patch_id`, `task_id`, `patch`, `strategy`, `created_at`. The session publishes a `CHECKPOINT` update with this content: `kind: "context_patch"`, `patch_id`, `task_id`, `digest`, `diverged`, `prompt`, `options: ["approve", "reject"]`.

To approve or reject, use one of:
- the tool `tasks.apply_patch(patch_id=..., action="apply" | "reject")`;
- `task_service.apply_patch(session_id=..., patch_id=..., action=...)`;
- `session.steer(SteeringEvent(..., event_type=SteeringEventType.APPROVE, payload={"patch_id": ...}))`, or `REJECT`.

An approved patch is merged as `append` unless you pass `strategy`. Use this strategy for anything whose result changes what the user sees or what the agent does next.

### `append` / `replace`
The result is stored at once as a `BackgroundTaskResult` in the session's `background_results[task_id]`, with `summary` (from the digest), `payload`, `facts` and `artifacts`. The in-process session handles both strategies the same way. Use them for trusted, idempotent enrichment.

### Context divergence
A patch remembers the context version and hash its task started from. If the session context changed since then, the patch gets `context_diverged=True`, and a warning `NOTIFICATION` ("Context changed") is published.

## `ContextPatch`

`penguiflow.sessions.models.ContextPatch` has these fields:
- `task_id`, `spawned_from_event_id`;
- `source_context_version`, `source_context_hash`, `context_diverged`, `completed_at`;
- `digest: list[str]`, `facts: dict`, `artifacts: list[dict]`, `sources: list[dict]`;
- `recommended_next_steps: list[str]`, `assumptions: list[str]`.

A subagent's patch carries its answer as `digest` (up to 5000 chars), plus the run's artifacts and sources. A custom `TaskPipeline` returns `TaskResult(payload=..., context_patch=ContextPatch(task_id=..., digest=[...], facts={...}))`.

## Patterns

The model drives these through `tasks.*` tool calls with the same argument names. The service calls below are what those tools run.

### Parallel research, one report
```python
from penguiflow.sessions import MergeStrategy

for segment in ("A", "B", "C"):
    await task_service.spawn(
        session_id="s1",
        query=f"Research market segment {segment}",
        merge_strategy=MergeStrategy.APPEND,
        group="market_q3",
        turn_id="turn-7",
        group_sealed=segment == "C",   # seal on the last spawn
    )
```
With proactive reporting set up and the default report strategy `"all"`, the user gets one report when all three finish.

### Human-gated bundle
```python
from penguiflow.sessions import MergeStrategy

first = await task_service.spawn(
    session_id="s1",
    query="Draft email v1 (formal)",
    group="email",
    group_merge_strategy=MergeStrategy.HUMAN_GATED,
)
await task_service.spawn(session_id="s1", query="Draft email v2 (friendly)", group="email")
await task_service.seal_group(session_id="s1", group_id=first.group_id)

# after the group_approval checkpoint, on the user's decision:
outcome = await task_service.apply_group(session_id="s1", group_id=first.group_id, action="apply")
```
`apply_group` applies or rejects every patch. To keep only one draft, apply its patch with `apply_patch` and reject the others. `session.pending_patches[patch_id].task_id` tells you which task produced each patch.

### Wait for the group in the same turn
`tasks.spawn` args: `{"query": "...", "group": "etl", "merge_strategy": "append", "group_merge_strategy": "append", "retain_turn": true}`. The spawn seals the group and waits up to `retain_turn_timeout_s`. It then returns `retained=True` and a `group_completion` (status, completed and failed task ids, results). On timeout, `group_completion.timed_out` is true and a background continuation is scheduled (`background_continuation_max_hops`).

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `apply_group` → `group_not_complete` | Not sealed, or tasks still running | Seal it; wait for completion |
| `group_not_joinable` | `group_id` names a sealed or finished group | Start a new group |
| Group never completes | Never sealed (direct `planner.run` doesn't auto-seal) | `group_sealed=True`, `tasks.seal_group`, or `session.on_foreground_yield()` |
| Spawns join an old group | No `turn_id`, so every spawn is in the same turn | Pass `tool_context["turn_id"]` per user turn |
| No group report | Proactive reporting not set up, report `"none"`, or a human-gated group | `setup_proactive_reporting(...)`; handle the `group_approval` checkpoint |
| `retain_turn` rejected | Human-gated strategy | Use `append`/`replace` for retained groups |
| Stale results | `context_diverged=True` | Show the flag; re-run if needed |

## Observability

Track groups per session, time from creation to seal and from seal to completion, tasks per group, the human-gated approval rate, and the divergence rate. The group telemetry events carry `group_name`, `task_count`, `completed_count` and `failed_count` in `extra`.
