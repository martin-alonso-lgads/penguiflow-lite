# Resume Mechanics

## `planner.resume(token, user_input=None, *, tool_context=None, memory_key=None, steering=None, tool_visibility=None)`

```python
result = await planner.resume(
    pause.resume_token,
    user_input="approved",
    tool_context={"session_id": "s1", "tenant_id": "acme", "user_id": "u1", "approvals": {"write_line": True}},
)
```

What it does:
1. **Finds the pause record.** It looks first in memory, on the internal planner of this call's `session_id`, removing the record when found, and then in `state_store.load_planner_state(token)`. If both miss, it raises `KeyError`.
2. **Restores the run.** It restores the trajectory (including the pause step), the constraint tracker (deadline remaining, hops used) and the artifact registry, then emits a `resume` planner event.
3. **Sets `tool_context`.** The one you pass replaces the stored one. If you pass none, the stored one is used, when it was saved.
4. **Continues the loop.** It returns a `PlannerFinish`, or another `PlannerPause` if the run pauses again.

The result has the same shape as `planner.run(...)` returns. Loop until it isn't a `PlannerPause`.

## Parameter semantics

### `token: str`
The `resume_token` from the prior `PlannerPause`. Treat it as a **secret**: anyone who has it can resume the run.

### `user_input: str | None`
Text added to the next prompt as `Resume input: <text>`. Use cases:
- an approval decision: `"approved"` / `"rejected"`;
- a pasted code: `"123456"` for 2FA;
- a selected option: `"prod"` from an `await_input` list.

The model reads it and decides. For structured decisions, prefer a `tool_context` flag that the tool checks: it is deterministic, while `user_input` relies on the model.

### `tool_context: dict | None` (pass it every time)
It replaces the stored `tool_context`; the two are not merged:

```python
# at pause time, tool_context was:
# {"session_id": "s1", "approvals": {}}

# at resume, supply the approval:
result = await planner.resume(
    token,
    tool_context={"session_id": "s1", "approvals": {"write_line": True}},
)
```

Re-pass the full dict. The stored copy is saved only when every value is JSON-serializable, and is dropped as a whole otherwise, so clients never survive persistence.

**Always include:**
- the **same `session_id` as the run**. It routes the call to the session that holds the in-memory record, and it also keys memory;
- `tenant_id` and `user_id` if you use them (memory isolation, artifacts, OAuth);
- the decision (`approvals`, the provided input, …);
- anything the next tool needs (clients, stores).

### `memory_key: MemoryKey | None`
Short-term memory derives its key from `tool_context` (`tenant_id`, `user_id`, `session_id`). Only `session_id` is required; tenant and user default to `default` and `anonymous`. Pass an explicit `MemoryKey` (from `penguiflow.planner.memory`) only if the identity isn't in `tool_context`.

## What survives pause/resume

Survives:
- the trajectory, including the pause step `{"pause": reason, "payload": payload}`;
- the remaining deadline and the hops used. They are not reset, so `deadline_s` and `hop_budget` cover all segments together, and the clock doesn't run while paused;
- the pause `reason` and `payload`;
- the run's `tool_context`. In memory it is kept as-is. In a state store it is kept only if it is entirely JSON-serializable.

Does **not** survive:
- local variables inside the paused tool (it raised);
- values a tool wrote into `ctx.tool_context` (each call gets a copy);
- clients and other non-JSON `tool_context` values, once the record goes through a state store.

## The continue model

When the planner resumes, it doesn't continue inside the tool function:
1. It restores the trajectory that ends with the pause step.
2. The model is prompted with that trajectory and `Resume input: <user_input>`.
3. The model picks the next action, usually the same tool, with the same or updated args.
4. The tool runs again from the top, re-checks `tool_context`, and this time the precondition holds.

That is why **tools must re-check at the top**, rather than expect to "wake up" after the pause.

## OAuth resume

OAuth resumes work the same way:

```python
# pause.payload == {"pause_type": "oauth", "provider": "github", "auth_url": "https://...", "state": "...", ...}

# 1. the UI opens auth_url and the user completes OAuth
# 2. your callback route exchanges the code; the token is stored under (user_id, provider)
user_id, trace_id = await oauth_manager.handle_callback(code, state)

# 3. resume with the same user_id
result = await planner.resume(
    pause.resume_token,
    user_input="oauth_completed",
    tool_context={**original_ctx, "user_id": user_id},
)
```

On the next call, `ToolNode` finds the token with `OAuthManager.get_token(user_id, provider)` and sends `Authorization: Bearer <token>`. The real signal is the token in the store; you set no flag.

## Multiple sequential pauses

A single run can pause many times:

```python
result = await planner.run(query, tool_context=ctx)
while isinstance(result, PlannerPause):
    ...  # render result.payload, wait for the user
    result = await planner.resume(result.resume_token, tool_context=ctx)
```

Each pause has its own token.

## Concurrent sessions

One planner instance serves many sessions at once. Calls with the same `session_id` are serialized on that session's internal planner; different sessions run in parallel. Pause records are keyed by token, and in memory they live on the session that paused.

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `KeyError` on resume | Token already used, restart without a store, or a different/missing `session_id` | Resume with the run's `session_id`; configure a durable `StateStore` |
| Resume calls a different tool | The model didn't pick the gated tool | Tighten the prompt; check the gated tool is in the catalog and visible |
| The tool re-checks but the flag is missing | The resume `tool_context` didn't include it | Re-pass the full `tool_context` |
| Approval set but the tool still pauses | The tool reads a different key than the host sets | Share one constant for the key |
| `user_input` ignored | The model doesn't treat it as decisive | Encode the decision in `tool_context` |
| OAuth token stored, tool re-pauses | `user_id` differs between callback and resume | Normalize `user_id` |
| Deadline hit right after resume | The deadline budget is shared across segments | Size `deadline_s` for the whole run's active time |

## Hard rules

- `resume_token` is a secret. Don't log it or expose it to other users. The planner logs only its first 8 characters.
- In memory, a token works once (the record is removed). The planner never deletes from the state store, so delete on load there to make tokens one-time across workers.
- A failing `save_planner_state` doesn't block the pause; it is logged as `state_store_save_failed`. A failing `load_planner_state` surfaces as `KeyError`.
- `tool_context` on resume is a full replacement; don't omit keys you still need.
