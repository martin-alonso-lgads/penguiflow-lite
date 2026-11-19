# Builders and `artifact_ref` Composition

`build_*` tools take the same arguments as their `render_*` twins, but they emit nothing. They validate the component, store it in the planner's in-run `ArtifactRegistry`, and return a ref the LLM can place into a parent component. Source: `penguiflow/rich_output/nodes.py` and `penguiflow/planner/artifact_registry.py`.

## The builders

| Builder | Args model | Twin |
|---|---|---|
| `build_chart_echarts` | `BuildChartEChartsArgs` | `render_chart_echarts` |
| `build_table` | `BuildTableArgs` | `render_table` |
| `build_grid` | `BuildGridArgs` | `render_grid` |
| `build_tabs` | `BuildTabsArgs` | `render_tabs` |
| `build_accordion` | `BuildAccordionArgs` | `render_accordion` |

There is no builder for `report` or `markdown`, or for components without a typed tool. Each builder is only registered when its component is allowlisted.

A builder returns `BuildComponentResult(ok, component, artifact_ref, dedupe_key, summary, skipped)`, for example `{"ok": true, "component": "echarts", "artifact_ref": "artifact_0", "summary": "Built echarts", ...}`. A builder needs an active planner run; called outside one, it raises. Renderers register their component too, so a `render_*` result's `artifact_ref` can also be reused.

## Where refs go

Put `{"artifact_ref": "<ref>"}` where a child component would go:

| Parent | Slot |
|---|---|
| `render_grid` / `build_grid` | `items[].artifact_ref` (keeps `colSpan`, `rowSpan`, `title`) |
| `render_tabs` / `build_tabs` | `tabs[].artifact_ref` |
| `render_accordion` / `build_accordion` | `items[].artifact_ref` |
| `render_report` | `sections[].components[].artifact_ref` (keeps `caption`) |
| `render_component` | Any dict in `props` that has an `artifact_ref` key |

Each item takes one source: an `artifact_ref`, *or* `component` + `props` (tabs and accordion items can also hold `content`). Mixing them fails validation. Before validating and emitting, the renderer swaps each ref dict for the stored `{"component", "props"}` and keeps the item's other keys. The frontend only ever sees resolved components.

## A composed dashboard

These are the arguments of three tool calls, in order:

```python
from penguiflow.rich_output.tools import (
    BuildChartEChartsArgs,
    BuildTableArgs,
    DataGridColumn,
    GridItem,
    RenderGridArgs,
    RenderReportArgs,
    ReportComponentItem,
    ReportSection,
)

# 1) build_chart_echarts -> {"artifact_ref": "artifact_0", ...}
chart = BuildChartEChartsArgs(
    option={"xAxis": {"type": "category", "data": ["Q1", "Q2"]}, "series": [{"type": "line", "data": [3, 5]}]},
    title="Revenue",
)

# 2) build_table -> {"artifact_ref": "artifact_1", ...}
table = BuildTableArgs(
    columns=[DataGridColumn(field="region", header="Region"), DataGridColumn(field="revenue", format="currency")],
    rows=[{"region": "EMEA", "revenue": 1200}],
)

# 3) render_grid, reusing both refs plus an inline child
dashboard = RenderGridArgs(
    columns=2,
    items=[
        GridItem(artifact_ref="artifact_0", col_span=2),
        GridItem(artifact_ref="artifact_1"),
        GridItem(component="markdown", props={"content": "Source: warehouse"}),
    ],
)

# The same chart can go into a report later in the run
report = RenderReportArgs(
    title="Q3 review",
    sections=[
        ReportSection(
            title="Trend",
            components=[ReportComponentItem(artifact_ref="artifact_0", caption="Revenue by quarter")],
        ),
    ],
)
```

A ref is not consumed by use: it can go into any number of parents within the run. A builder's output can itself contain refs, for example a `build_grid` of built charts referenced from `render_tabs`.

## `list_artifacts`

```python
from penguiflow.rich_output.tools import ListArtifactsArgs

ListArtifactsArgs(kind="ui_component", source_tool="build_chart_echarts", limit=10)
```

- `kind` is `"all"` (the default), `"ui_component"`, `"binary"` or `"tool_artifact"`; `"tool_artifact"` is treated like `"all"`.
- `limit` defaults to 25 and keeps the **last** N entries.
- With `"all"` or `"binary"`, the listing also includes artifacts in the scoped `ArtifactStore` (`ctx.artifacts.list()`).
- The result is `{"ok": true, "artifacts": [...]}`. Each entry has `ref`, `kind`, `source_tool`, `component`, `title`, `summary`, `artifact_id`, `mime_type`, `size_bytes`, `created_step`, `renderable` and `metadata`.

Binary artifacts in the registry (stored by tools during the run, or by the observation clamp) can be referenced too. They resolve to an `image`, `embed` (PDF) or `markdown` component that points at the artifact URL.

## Lifetime

- A new `run()` starts an empty registry, and refs count from `artifact_0`.
- The registry is snapshotted into `trajectory.metadata["artifact_registry"]`, so refs survive a pause and `resume()` of the same run.
- A ref from another run fails with `Unknown artifact_ref 'artifact_0'`.
- For anything that must outlive the run, store it with `ctx.artifacts.upload(...)` (see [[penguiflow-statestore]]), or persist the emitted `artifact_chunk` in your app.

## Operational defaults

- Build only what you reuse; each `build_*` call is a tool step and costs LLM tokens.
- Treat refs as opaque, run-scoped ids; the frontend never needs them.
- If the LLM repeats an identical build in the next step, the planner returns the previous ref with `skipped="duplicate_build"`.
