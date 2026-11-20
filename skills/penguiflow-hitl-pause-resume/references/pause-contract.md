# The Pause Contract

## How pauses work mechanically

```
planner.run(user_message, tool_context=...)
  -> LLM step
  -> tool invocation
  -> tool calls `await ctx.pause(reason, payload)`
  -> pause raises an internal signal that exits the tool body
  -> planner catches the signal
  -> planner records the pause:
       a trajectory step whose observation is {"pause": reason, "payload": payload}
       a pause record in memory (on that session's planner)
       the same record via state_store.save_planner_state, if available
  -> planner returns PlannerPause(reason, payload, resume_token)
```

The tool's local variables are **gone**. The function did not pause mid-execution; it raised. On resume, the run continues from the saved trajectory, and the model sees the pause observation and your `user_input`. Usually it calls the same tool again, with updated `tool_context` (the approval flag, the OAuth token, the missing field).

So design tools to **re-check their preconditions at the top** and pause only when one is missing. Don't pause in the middle of a multi-step tool: split it into two tools, or move the pause to the start.

## `ctx.pause(reason, payload=None)`

Parameters:
- `reason`: one of `approval_required`, `await_input`, `external_event` or `constraints_conflict`. Any other value fails validation, and the tool fails with that error.
- `payload`: a JSON-friendly, application-defined mapping (optional).

In a tool, `await ctx.pause(...)` never returns. If the planner has `pause_enabled=False`, it raises `RuntimeError("Pause/resume is disabled for this planner")`, and the tool call fails with that error.

Guardrail PAUSE decisions also produce `approval_required` pauses, with the decision under `payload["guardrail"]` (see `SKILL.md`). Your tools never call those. With `pause_enabled=False` they don't raise: the run finishes `no_path` instead.

### Reason taxonomy

#### `approval_required`
The tool wants to do something the user must explicitly OK: writing data, sending an email, calling an external API with side effects.

Typical payload:
```python
{
    "title": "Approve write",
    "preview": "First 200 chars of what will be written",
    "approval_key": "write_line",   # convention: tool name or operation id
    "tags": ["destructive"],
}
```

#### `await_input`
The tool needs information the user has but hasn't provided: a missing form field, an ambiguous reference, a clarification.

Typical payload:
```python
{
    "title": "Which database?",
    "options": ["prod", "staging", "dev"],
    "input_key": "target_db",
}
```

#### `external_event`
The tool depends on an out-of-band event. OAuth is the canonical case, but any "go do X, then come back" handoff fits.

The OAuth payload sent by `ToolNode`:
```python
{
    "pause_type": "oauth",
    "provider": "github",
    "display_name": "GitHub",
    "auth_url": "https://...",
    "scopes": ["repo"],
    "state": "<random>",
}
```

A typical custom payload:
```python
{
    "pause_type": "webhook",
    "callback_url": "https://app.example.com/callback/abc123",
    "expected_event": "payment.succeeded",
}
```

#### `constraints_conflict`
A policy says no, and the user must decide whether to override. It differs from `approval_required` in that the default is to deny.

Typical payload:
```python
{
    "title": "Policy violation",
    "rule": "no_writes_to_prod",
    "violation_summary": "Tool foo would write to prod tables",
    "override_key": "force_prod_write",
    "requires_role": "admin",
}
```

## Payload rules

1. **JSON-friendly only.** Strings, numbers, booleans, null, lists and dicts. No datetimes, Pydantic models or callables.
2. **Small.** Payloads travel through the trajectory, possibly the `StateStore`, and your UI. Aim for under 4 KB.
3. **No secrets.** Payloads are visible to the application, and likely logged.
4. **Stable schema per tool.** UIs render the payload, so don't change its shape between releases without a UI migration.
5. **No prompt content.** Don't echo the user's full message; reference it by `trace_id` if needed.

### When a payload exceeds these limits

- Large content: upload it to the artifact store and reference it by id.
- Sensitive content: keep it in a side store keyed by `session_id`, and put the side-store id in the payload.
- Many options: show the first screen with a "see more" control in the UI, and truncate the payload list.

## Tool design patterns

The fences below share these imports:
```python
from uuid import uuid4

from pydantic import BaseModel

from penguiflow.catalog import tool
from penguiflow.planner import ToolContext
```

### Pattern 1: check and pause at entry

The default. Re-check on each call, and pause only if the requirement isn't met.

```python
class SendArgs(BaseModel):
    to: str
    body: str


class SendOut(BaseModel):
    sent: bool


@tool(desc="Send an email", side_effects="external")
async def send_email(args: SendArgs, ctx: ToolContext) -> SendOut:
    if not (ctx.tool_context.get("approvals") or {}).get("send_email"):
        await ctx.pause("approval_required", {"approval_key": "send_email", "preview": args.body[:200]})
    ...  # send it
    return SendOut(sent=True)
```

### Pattern 2: plan, then commit

Split a destructive tool in two:
- `plan_write` is pure: it builds the plan and returns a `plan_id`;
- `commit_write` takes the `plan_id`, pauses for approval, then executes.

State must not be stashed in `ctx.tool_context` between the calls. Each tool call gets a fresh copy of the run's `tool_context`, so a key set in one call is gone by the next. There are four ways to carry it:
- in `ctx.run_state`, a mapping that every tool call of the run shares and that survives pause and resume (values must be JSON-serializable; it lives in the trajectory metadata, not across runs);
- through the tool result, as below: the model passes `plan_id` to the next call;
- through the pause payload, which the host can pass back in the resume `tool_context`;
- in a store keyed by session.

Objects already in `tool_context` are shared between calls, so an app-owned store passed in it works:

```python
class PlanStore:
    """App-owned plan store. Pass the same instance in tool_context on run() and resume()."""

    def __init__(self) -> None:
        self._plans: dict[tuple[str, str], list[str]] = {}

    def put(self, session_id: str, plan_id: str, rows: list[str]) -> None:
        self._plans[(session_id, plan_id)] = rows

    def get(self, session_id: str, plan_id: str) -> list[str] | None:
        return self._plans.get((session_id, plan_id))


class PlanArgs(BaseModel):
    rows: list[str]


class PlanOut(BaseModel):
    plan_id: str
    preview: str


class CommitArgs(BaseModel):
    plan_id: str


class CommitOut(BaseModel):
    written: int


@tool(desc="Preview a write; returns a plan_id to commit", side_effects="pure")
async def plan_write(args: PlanArgs, ctx: ToolContext) -> PlanOut:
    plan_id = f"plan-{uuid4().hex[:8]}"
    ctx.tool_context["plans"].put(ctx.tool_context["session_id"], plan_id, args.rows)
    return PlanOut(plan_id=plan_id, preview=f"{len(args.rows)} rows")


@tool(desc="Commit a previewed write", side_effects="write")
async def commit_write(args: CommitArgs, ctx: ToolContext) -> CommitOut:
    rows = ctx.tool_context["plans"].get(ctx.tool_context["session_id"], args.plan_id)
    if rows is None:
        raise ValueError(f"unknown plan {args.plan_id}")
    if not (ctx.tool_context.get("approvals") or {}).get(args.plan_id):
        await ctx.pause("approval_required", {"approval_key": args.plan_id, "preview": f"{len(rows)} rows"})
    ...  # write the rows
    return CommitOut(written=len(rows))
```

The host passes `tool_context={"session_id": ..., "plans": plan_store}` on `run()`. It resumes with the same keys, plus `"approvals": {plan_id: True}`.

An in-process store works in one process only. With several workers, back `PlanStore` with Redis or a database. If the plan is small, return it whole in `PlanOut` and take it as a `CommitArgs` field instead.

`ctx.kv` is a built-in durable alternative. It needs a state store with `save_memory_state`/`load_memory_state`, and `tenant_id`, `user_id`, `session_id`, `task_id` and `trace_id` in `tool_context`. Its default namespace is per tool, so pass the same `namespace=` from both tools.

### Pattern 3: multi-stage await-input

Each missing field gets its own pause:

```python
class EmailArgs(BaseModel):
    body: str
    recipient: str | None = None
    subject: str | None = None


@tool(desc="Send an email, asking for missing fields", side_effects="external")
async def send_email_interactive(args: EmailArgs, ctx: ToolContext) -> SendOut:
    if not args.recipient:
        await ctx.pause("await_input", {"input_key": "recipient", "prompt": "To whom?"})
    if not args.subject:
        await ctx.pause("await_input", {"input_key": "subject", "prompt": "What subject?"})
    ...  # send it
    return SendOut(sent=True)
```

This works only because pauses raise and the tool runs again after resume. The user's answer arrives as `user_input`, and the model supplies the missing field as an argument on its next call.

## Operational rules

- **Check at the top.** Don't pause halfway through a computation.
- **Make pre-pause work idempotent.** Anything before `ctx.pause(...)` runs again on every retry.
- **Make re-entry cheap.** Cache slow precondition checks in a store keyed by session, not in `ctx.tool_context` (each call gets a fresh copy).
- **One reason per pause site.** Don't multiplex reasons.
- **`pause_enabled=False`** turns every pause into a tool error that the model sees. Use it where HITL must never fire.

## When pauses don't fire

| Symptom | Cause | Fix |
|---|---|---|
| Tool runs without prompting | `tool_context` already has the approval or input | Verify the check |
| Tool fails with "Pause/resume is disabled for this planner" | Planner has `pause_enabled=False` | Enable it, or remove HITL from that deployment |
| Tool fails with a validation error at the pause | `reason` isn't one of the four | Use exactly the four reasons |
| Next tool can't find the planned state | State was stashed in `ctx.tool_context` | Pattern 2: pass it through the result or a store |
| Pause resumes but the gated tool never runs | The model chose another tool | Check the trajectory; tighten the prompt so it re-calls the gated tool |
