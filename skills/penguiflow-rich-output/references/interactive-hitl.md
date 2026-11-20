# Interactive Tools: `ui_form`, `ui_confirm`, `ui_select_option`

These tools pause the run so a person can answer. They emit **no** `artifact_chunk` and return no `artifact_ref`. The component reaches your app as the pause payload, and the answer comes back through `planner.resume(token, user_input=...)`. Source: `penguiflow/rich_output/nodes.py`; the general pause and resume contract lives in [[penguiflow-hitl-pause-resume]].

## What a call does

1. It validates the arguments (`UIFormArgs`, `UIConfirmArgs` or `UISelectOptionArgs`; all `extra="forbid"`).
2. It dumps them to camelCase props, without `None` values, and checks them against the `form`, `confirm` or `select_option` registry schema. The component must be in `RichOutputConfig.allowlist`.
3. It calls `ctx.pause("await_input", {"tool": ..., "component": ..., "props": ..., "registry_version": ...})`.

`run()` (or `resume()`) then returns:

```python
from penguiflow.planner import PlannerPause

pause = PlannerPause(
    reason="await_input",
    payload={
        "tool": "ui_confirm",
        "component": "confirm",
        "props": {"message": "Ship it?", "confirmLabel": "Confirm", "cancelLabel": "Cancel", "variant": "warning"},
        "registry_version": "2025-12-24",
    },
    resume_token="...",
)
```

The planner also emits a `tool_call_result` event whose `result_json` is `{"pause": "await_input", "payload": {...}}`.

## Arguments

```python
from penguiflow.rich_output.tools import (
    FormField,
    SelectOptionItem,
    UIConfirmArgs,
    UIFormArgs,
    UISelectOptionArgs,
)

form = UIFormArgs(
    title="Send email",
    description="Compose and confirm",
    fields=[
        FormField(name="to", type="email", label="To", required=True),
        FormField(name="subject", type="text", label="Subject", required=True),
        FormField(name="body", type="textarea", label="Body", help_text="Plain text"),
        FormField(name="priority", type="select", options=["low", "high"], default="low", width="half"),
    ],
    submit_label="Send",          # alias submitLabel; default "Submit"
    cancel_label="Cancel",        # alias cancelLabel; default None
    layout="vertical",            # vertical | horizontal | inline
)

confirm = UIConfirmArgs(
    title="Delete this record?",
    message="This action cannot be undone.",   # required
    confirm_label="Delete",       # default "Confirm"
    cancel_label="Keep",          # default "Cancel"
    variant="danger",             # info | warning | danger | success
    details="Affects 3 child rows.",
)

select = UISelectOptionArgs(
    title="Target environment",
    options=[
        SelectOptionItem(value="prod", label="Production", description="Live traffic"),
        SelectOptionItem(value="staging", label="Staging"),
    ],
    multiple=False,
    min_selections=1,             # alias minSelections
    max_selections=None,          # alias maxSelections
    layout="list",                # list | grid | cards
    searchable=False,
)
```

- `FormField.type`: `text`, `number`, `email`, `password`, `url`, `tel`, `textarea`, `select`, `multiselect`, `checkbox`, `radio`, `switch`, `date`, `datetime`, `time`, `file`, `range` or `color`.
- `FormField` also takes `placeholder`, `disabled`, `validation` (`FormFieldValidation(min, max, min_length, max_length, pattern, message)`) and `options` (strings or `FormFieldOption(value, label)`).
- `SelectOptionItem` also takes `icon`, `disabled` and `metadata`.

## Rendering and resuming

```python
import json

from penguiflow.planner import PlannerPause

result = await planner.run("Email the Q3 summary to finance", tool_context={"session_id": "s1"})
while isinstance(result, PlannerPause) and result.reason == "await_input":
    component = result.payload["component"]  # "form" | "confirm" | "select_option"
    props = result.payload["props"]
    answer: dict[str, object] = ...  # your UI renders component/props and collects the answer
    result = await planner.resume(result.resume_token, user_input=json.dumps(answer))
```

- `user_input` is a string. The planner adds it to the next prompt as a user message, `Resume input: <text>`. JSON-encode structured answers, and include a cancel signal if the UI offers one (for example `{"cancelled": true}`).
- The `ui_*` tool is not run again. Its step's observation stays `{"pause": "await_input", "payload": ...}`, and the LLM continues from the resume input.
- `tool_context=` on `resume()` replaces the stored tool context, so pass the session identity again if you override it. Don't put the answer there: the LLM never sees `tool_context`.
- `resume()` removes the in-memory pause record, so a token works once in the process that paused. For resumes in another process, configure a `StateStore` with planner-state support ([[penguiflow-hitl-pause-resume]]).

## Wiring requirements

1. `pause_enabled=True` on the planner (the default). With pauses disabled, the call fails with `Pause/resume is disabled for this planner`, and the LLM sees that as a tool error.
2. `form`, `confirm` and `select_option` in `RichOutputConfig.allowlist` (they are in `DEFAULT_ALLOWLIST`). The three tools are registered either way, but a call for a component that isn't allowlisted fails validation.
3. A frontend that renders the pause payload and calls back into your host API, which then calls `resume()`.

## When to use `ctx.pause(...)` directly

In your own tool, use `ctx.pause("approval_required", {...})` when the pause belongs to that tool's side effect, such as approving a write. Use the `ui_*` tools when the LLM itself needs structured input from the person. Either way, nothing after the pause runs in that tool call, and anything before it has already happened.

## Cross-channel rules

Downstream agents can't render forms. For A2A serving, hide `ui_form`, `ui_confirm` and `ui_select_option` with a `ToolVisibilityPolicy` (see [[penguiflow-reactplanner-config]] step 9). `A2AService` sets `tool_context["delivery_channel"] = "a2a"`, and your prompt guidance can key on it.
