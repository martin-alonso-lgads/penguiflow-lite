---
name: penguiflow-rich-output
description: Add structured UI components (charts, tables, reports, grids, tabs, accordions, forms) to a PenguiFlow ReactPlanner — enable rich output via `attach_rich_output_nodes(registry, config=RichOutputConfig(enabled=True, allowlist=[...]))`, pick the right tool family (generic `render_component`, typed `render_chart_echarts`/`render_table`/`render_report`/`render_grid`/`render_tabs`/`render_accordion`, non-emitting `build_*` builders whose `artifact_ref` goes into parent `artifact_ref` slots, interactive `ui_form`/`ui_confirm`/`ui_select_option` that pause with `reason="await_input"` and resume via `planner.resume(token, user_input=...)`), send arguments that match the `extra="forbid"` models in `penguiflow.rich_output.tools`, fetch schemas with `describe_component`, list artifacts with `list_artifacts`, and consume `artifact_chunk` events with `artifact_type="ui_component"` on the frontend. Use when a user says "render a chart", "show a table", "rich output", "UI components", "interactive form", or names `render_component`, `RichOutputConfig`, `attach_rich_output_nodes`.
---

# PenguiFlow Rich Output

## When to use
- The agent needs to render charts, tables, reports, grids, tabs, accordions — not just markdown.
- You need interactive UI tools (`ui_form`, `ui_confirm`, `ui_select_option`) for structured user input.
- You want planner-side validation of component props against a registry.
- You want builder tools that compose reusable components referenced by `artifact_ref`.

## When NOT to use
- Frontend has no rich renderer → stick to text/markdown.
- A2A specialist serving downstream machines → use [[penguiflow-reactplanner-config]] step 9 (hide UI-only tools).
- Streaming raw tokens (not components) → use [[penguiflow-streaming]].
- Persisting binary artifacts (PDFs, images) → use [[penguiflow-statestore]] (`ArtifactStore`).

## Hard boundaries
This skill is the **rich-output tool surface** and component contract. The frontend renderer is your app's responsibility — this skill specifies what the backend emits and the wire shape, not how to draw it. The `penguiflow-reactplanner-config` skill no longer enumerates rich-output bullets; this skill owns them.

## Workflow

### 1) Enable rich output
```python
from penguiflow import ModelRegistry
from penguiflow.catalog import build_catalog
from penguiflow.planner import ReactPlanner
from penguiflow.rich_output import RichOutputConfig, attach_rich_output_nodes

registry = ModelRegistry()
rich_nodes = attach_rich_output_nodes(
    registry,
    config=RichOutputConfig(
        enabled=True,  # default False: attach returns [] until you opt in
        allowlist=["markdown", "echarts", "datagrid", "report", "grid", "tabs", "confirm"],
        # include_prompt_catalog=True, max_payload_bytes=250_000 (defaults)
    ),
)
catalog = build_catalog(rich_nodes, registry)  # add your own nodes to the list
planner = ReactPlanner(..., catalog=catalog)
```
- `allowlist` defaults to `DEFAULT_ALLOWLIST` (19 components). Set it to what your frontend renders.
- Typed `render_*`/`build_*` tools are only registered for allowlisted components. `render_component`, `describe_component`, `list_artifacts` and the three `ui_*` tools are always registered.
- The config is process-wide: the latest `attach_rich_output_nodes` call wins.
- The planner appends the allowlisted component catalog to its system prompt.

### 2) Keep the core tools visible
With tool discovery or deferral (see [[penguiflow-reactplanner-config]]), keep `render_component`, `describe_component`, `list_artifacts`, the `ui_*` tools you use, and the typed `render_*`/`build_*` tools you rely on always visible.

### 3) Pick the right tool family
| Family | Tools | Emits UI? | Use for |
|---|---|---|---|
| Generic renderer | `render_component(component, props, id?, title?, metadata?)` | Yes | Any allowlisted component; the only route for `markdown`, `json`, `mermaid`, `plotly`, `metric`, `code`, `latex`, `callout`, `image`, `video`. |
| Typed renderers | `render_chart_echarts`, `render_table`, `render_report`, `render_grid`, `render_tabs`, `render_accordion` | Yes | Common components, with typed args. |
| Builders | `build_chart_echarts`, `build_table`, `build_grid`, `build_tabs`, `build_accordion` | No | Build once, return an `artifact_ref`, place it in parents. There is no `build_report`. |
| Interactive | `ui_form`, `ui_confirm`, `ui_select_option` | No (pauses) | Structured user input; see step 5. |
| Introspection | `describe_component(name)`, `list_artifacts(kind, source_tool, limit)` | No | Fetch a props schema; list this run's artifacts. |

Every args model is `extra="forbid"`, so the names must be exact. Common traps:
- Table columns are `{field, header, format}`.
- Report sections are `{title, content, components, subsections}`.
- Grid items take `colSpan`.
- Tabs take `defaultTab`; accordion items take `title`.
- Charts take `artifact_metadata`.
- Child refs are `artifact_ref`.
- Markdown props are `{"content": ...}`.

See `references/components-and-render-tools.md`.

### 4) Understand the wire contract
Each `render_*` call emits one planner event:
```python
event_type = "artifact_chunk"
extra = {
    "stream_id": "ui",
    "seq": 0,
    "done": True,
    "artifact_type": "ui_component",
    "chunk": {"id": None, "component": "echarts", "props": {}, "title": None},
    "meta": {"source_tool": "render_chart_echarts", "registry_version": "..."},
}
```
- `props` arrive validated, with any `artifact_ref` already resolved. Session updates carry the same data as a `RESULT` update.
- Renderers and builders both return `{"ok": true, "component": ..., "artifact_ref": "artifact_N", "summary": ...}`.
- Builders emit nothing; their refs go into `artifact_ref` slots of `render_grid`/`render_tabs`/`render_accordion` items and report section `components`. See `references/builders-and-artifact-refs.md`.

### 5) Handle interactive tools
`ui_form`, `ui_confirm` and `ui_select_option` validate their props, then call `ctx.pause("await_input", {"tool", "component", "props", "registry_version"})`.
- `run()` returns `PlannerPause(reason="await_input", payload=...)`. No `artifact_chunk` is emitted, so render `payload["component"]` with `payload["props"]` from the pause.
- Resume with `planner.resume(result.resume_token, user_input=json.dumps(answer))`; the LLM sees it as `Resume input: ...`.
- Pause durability is covered in [[penguiflow-hitl-pause-resume]]; payload and args in `references/interactive-hitl.md`.

### 6) Use `describe_component(name)` for schema-on-demand
It returns `{"component": <registry entry with propsSchema and example>}`. Validation errors already tell the LLM to call it, so keep it visible.

### 7) List artifacts mid-run
`list_artifacts(kind="all"|"ui_component"|"binary"|"tool_artifact", source_tool=None, limit=25)` returns `{"ok": true, "artifacts": [...]}`. It covers this run's registry (builds, renders, binary artifacts) and, for `all`/`binary`, the scoped `ArtifactStore`. Use it to find a ref to reuse.

### 8) Cross-channel rules
- For A2A specialists serving machines, hide rich-output tools via `ToolVisibilityPolicy`. `A2AService` already sets `tool_context["delivery_channel"]="a2a"` for prompt guidance to key on (see [[penguiflow-reactplanner-config]]).
- For a markdown-only fallback, allowlist `markdown` and let the planner use `render_component`.

## Troubleshooting (fast checks)
- **`Component '...' is not allowed.`** — add it to `RichOutputConfig.allowlist`.
- **`Extra inputs are not permitted`** — a wrong arg name (e.g. `default_index`, `component_artifact_ref`, or `metadata` on `render_chart_echarts`); check `references/components-and-render-tools.md`.
- **`Invalid props for '<component>'`** — props don't match `registry.json`; the error tells the LLM to call `describe_component`.
- **Frontend doesn't render** — confirm it handles `artifact_chunk` with `artifact_type="ui_component"`, and renders `ui_*` components from the `PlannerPause` payload.
- **Typed tool missing** — its component isn't allowlisted, or `enabled=False` (attach returned `[]`).
- **`Unknown artifact_ref '...'`** — the ref is from another run; refs live in one run (including its pause/resume).
- **`Pause/resume is disabled for this planner`** — a `ui_*` tool ran with `pause_enabled=False`.
- **LLM ignores the user's form answer** — send it as `user_input` on `resume()`; `tool_context` is never shown to the LLM.
- **A2A caller gets empty answer** — UI-only tools were called for a machine consumer; apply visibility policy + textual `llm_context` guidance.

## Worked examples
- `tests/test_rich_output_nodes.py` (render, build, ref composition, `list_artifacts`, `ui_*` pauses), `tests/test_rich_output_tools.py` and `tests/test_rich_output_runtime.py`.

## References (load only as needed)
- `references/components-and-render-tools.md` — registered tools, components and required props, `render_component` and typed tool args, the event, validation and limits.
- `references/builders-and-artifact-refs.md` — `build_*` tools, `artifact_ref` slots, a composed dashboard, `list_artifacts`, ref lifetime.
- `references/interactive-hitl.md` — `ui_form`/`ui_confirm`/`ui_select_option` args, the pause payload, the resume loop.
