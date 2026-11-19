---
name: penguiflow-hitl-pause-resume
description: Add human-in-the-loop pause/resume to a PenguiFlow ReactPlanner — let tools call `ctx.pause(reason, payload)` to gate approvals, OAuth handoffs, await-input prompts and policy decisions; handle guardrail PAUSE decisions that arrive as `approval_required` pauses with a `payload["guardrail"]` block; consume the resulting `PlannerPause(reason, payload, resume_token)` in your UI; call `planner.resume(resume_token, user_input=..., tool_context=...)` with the same `session_id` to continue; and persist pause state across workers via a `StateStore` implementing `save_planner_state`/`load_planner_state`. Use when a user says "pause for HITL", "human in the loop", "approval gate", "wait for user input", "OAuth handoff", "resume after restart", or names `PlannerPause`, `ctx.pause`, `resume_token`.
---

# PenguiFlow HITL Pause and Resume

## When to use
- A tool must wait for a human decision (approval, edit, OAuth completion).
- A run must survive a process restart between pause and resume.
- You need a structured pause payload your UI renders, not just a yield.

## When NOT to use
- Async background work the user doesn't directly approve → [[penguiflow-background-tasks]].
- Streaming partial responses to the user → [[penguiflow-streaming]].
- The durable backend that persists pauses → [[penguiflow-statestore]].
- The OAuth bookkeeping inside `ToolNode` → [[penguiflow-mcp-integration]]. This skill covers the planner side of the pause.
- Writing the guardrail rules that decide to pause → [[penguiflow-guardrails]].

## Hard boundaries
This skill is the pause/resume contract between tools, the planner and your host app. It does not define your UI, your auth model or the durable backend (that is the statestore skill). It shows what OAuth and guardrail pauses look like to the host; the MCP and guardrails skills own their wiring.

## Workflow

### 1) Decide what to pause for
| `reason` | Use case |
|---|---|
| `approval_required` | "Is it OK to send this email / write this row?" Guardrail PAUSE decisions also use this reason. |
| `await_input` | "Please provide the missing field." |
| `external_event` | OAuth handoff, third-party webhook, async callback. |
| `constraints_conflict` | A policy conflict that needs an operator override. |

These four are the only accepted values (`PlannerPauseReason`). The reason and payload are recorded in the trajectory, and the model sees them after resume.

### 2) Pause from a tool
```python
from pydantic import BaseModel

from penguiflow.catalog import tool
from penguiflow.planner import ToolContext


class WriteArgs(BaseModel):
    text: str


class WriteOut(BaseModel):
    ok: bool


@tool(desc="Write a line to a system (approval required)", side_effects="write")
async def write_line(args: WriteArgs, ctx: ToolContext) -> WriteOut:
    approvals = ctx.tool_context.get("approvals") or {}
    if not approvals.get("write_line"):
        await ctx.pause(
            "approval_required",
            {"title": "Approve write", "preview": args.text, "approval_key": "write_line"},
        )
    return WriteOut(ok=True)  # runs only on a later call, after approval
```

Mental model: **`ctx.pause(...)` raises an internal signal that exits the tool body.** The tool does not continue mid-function. After `planner.resume(...)`, the run continues from its saved trajectory, and the model decides the next step, usually calling the same tool again with the approval now in `tool_context`.

### 3) Consume the pause in the host app
```python
from penguiflow.planner import PlannerPause

tool_ctx = {"session_id": "s1", "tenant_id": "acme", "user_id": "u1"}
result = await planner.run(user_message, tool_context=tool_ctx)

while isinstance(result, PlannerPause):
    ...  # render result.payload, wait for the user's decision
    tool_ctx = {**tool_ctx, "approvals": {"write_line": True}}
    result = await planner.resume(result.resume_token, user_input="approved", tool_context=tool_ctx)
```

`PlannerPause` fields:
- `reason`: one of the four above;
- `payload: dict`: JSON-friendly and application-defined;
- `resume_token: str`: a random 32-character hex string. **Treat it as a secret.**

### 4) Handle guardrail pauses
A guardrail rule that returns `PAUSE` on `tool_call_start` (before the tool runs) or `tool_call_result` (after it ran, with the result withheld) ends `run()` with `PlannerPause(reason="approval_required")`. Its payload looks like this:
```python
{
    "guardrail": {
        "action": "PAUSE",
        "rule_id": "...",
        "reason": "...",
        "decision_id": "...",
        "severity": "...",
        "effects": [],
        "stop": None,
        "pause": {"scope": "tool_call", "approver_roles": ["admin"], "prompt": "...", "timeout_s": 300.0},
    }
}
```
Show `pause["prompt"]` and check `pause["approver_roles"]` in your UI. `timeout_s` is informational: nothing expires the pause, so enforce it yourself if you need it. Resume as usual. The tool call that triggered the pause has no result in the trajectory, so the model may call it again, and the rule is evaluated again. Guardrail pauses need `pause_enabled=True` like any other pause.

### 5) Make `resume_token` survive restarts
Pause records live in memory on the planner by default. For multi-worker or restart-prone deployments, give the planner a `StateStore` that implements `SupportsPlannerState`:
- `save_planner_state(token, payload) -> None`;
- `load_planner_state(token) -> dict | None`.

Attach it with `ReactPlanner(state_store=store)`. A failing save does not block the pause: it is logged as `state_store_save_failed`, and the in-memory record still works in this process. See [[penguiflow-statestore]].

### 6) Pass the same identity on every call
On both `run()` and `resume()`, pass `tool_context={"session_id": ..., "tenant_id": ..., "user_id": ...}`.
- **Same `session_id`.** The planner runs each session on its own internal planner, and the in-memory pause record lives there. A resume with a different or missing `session_id` only finds the token through a state store; otherwise it raises `KeyError`.
- **Resume `tool_context` replaces the stored one**, it isn't merged. Use it to supply the answer (set `approvals`, add the missing field) and to re-supply clients.
- **Stored `tool_context` is all-or-nothing.** If you omit `tool_context`, the planner uses the one saved with the pause. Persistence keeps it only if every value is JSON-serializable; one client object drops the whole dict.

### 7) Keep payloads small and safe
JSON-friendly only, and no secrets. Upload large binaries to the artifact store and reference them by id; truncate long strings.

### 8) Special case: OAuth pauses
A `ToolNode` with `AuthType.OAUTH2_USER` and no cached token pauses with `reason="external_event"` and this payload: `{"pause_type": "oauth", "provider": ..., "display_name": ..., "auth_url": ..., "scopes": [...], "state": ...}`. The UI opens `auth_url`. Your callback calls `OAuthManager.handle_callback(code, state)`, which stores the token. Then call `planner.resume(...)` with the same `user_id`. See [[penguiflow-mcp-integration]].

## Troubleshooting (fast checks)
- **`planner.resume(...)` raises `KeyError`**: the token was already used, the process restarted, the resume used a different `session_id`, or the store lacks `save_planner_state` / `load_planner_state`.
- **Resume is missing clients**: the stored `tool_context` was dropped (non-JSON value). Always re-pass `tool_context` on resume.
- **Pauses never happen**: the tool doesn't call `ctx.pause(...)`. With `pause_enabled=False`, `ctx.pause` makes the tool fail with `RuntimeError("Pause/resume is disabled for this planner")`, which the model sees as a tool error.
- **The same approval keeps re-prompting**: the resume `tool_context` didn't carry the flag, or the tool reads a different key.
- **A guardrail pause repeats after approval**: the retried call is evaluated again and the rule fires again. Rules don't see `tool_context`, so track approvals in the rule itself (see [[penguiflow-guardrails]]).
- **OAuth keeps re-pausing after resume**: the callback stored the token under a different `(user_id, provider)` than the tool reads.
- **A token resumes twice**: the in-memory record is removed on resume, but the planner never deletes from the store. Delete on load in `load_planner_state`.
- **Cross-tenant replay**: tenant-scope your store keys (`tenant:<id>:planner:pause:<token>`).
- **Payload too large**: offload it to artifacts and reference it by id.

## Worked example
- `examples/react_pause_resume/main.py`: an approval-gated tool with an explicit pause and resume.

## References (load only as needed)
- `references/pause-contract.md`: `ctx.pause(...)`, pause reasons, the raise-and-continue model, payload rules, tool design patterns.
- `references/resume-mechanics.md`: `planner.resume(...)` semantics, `tool_context` replacement, `user_input`, OAuth resume, what survives.
- `references/durability-and-statestore.md`: `SupportsPlannerState`, save/load semantics, TTL, token security, cross-worker recipes.
