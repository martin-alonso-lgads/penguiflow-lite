# Components and `render_*` Tools

Source: `penguiflow/rich_output/` (`runtime.py`, `tools.py`, `nodes.py`, `validate.py`, `registry.py`, `registry.json`). The LLM calls these tools; this page lists the arguments it must send. The examples build the argument models directly, which validates them exactly the way the planner does. Every model is `extra="forbid"`, so an unknown key fails the call.

## Which tools exist

`attach_rich_output_nodes(registry, config=RichOutputConfig(enabled=True, ...))` returns `[]` when `enabled=False`. Otherwise it returns:

| Always | Only when the component is in `allowlist` |
|---|---|
| `render_component`, `describe_component`, `list_artifacts`, `ui_form`, `ui_confirm`, `ui_select_option` | `echarts` → `render_chart_echarts`, `build_chart_echarts`<br>`datagrid` → `render_table`, `build_table`<br>`report` → `render_report`<br>`grid` → `render_grid`, `build_grid`<br>`tabs` → `render_tabs`, `build_tabs`<br>`accordion` → `render_accordion`, `build_accordion` |

The tool name sets live in `penguiflow.rich_output.tools`: `RICH_OUTPUT_RENDER_TOOL_NAMES` and `RICH_OUTPUT_BUILD_TOOL_NAMES`. There is no `build_report`, `build_markdown` or `render_markdown`.

`attach_rich_output_nodes` also configures the process-wide runtime (`configure_rich_output`), so the most recent call decides the allowlist and limits for every planner in the process. When `render_component` is in the catalog and `include_prompt_catalog=True` (the default), the planner appends the allowlisted component catalog to its system prompt.

## Components

The default `allowlist` is `DEFAULT_ALLOWLIST`: `markdown`, `json`, `echarts`, `mermaid`, `plotly`, `datagrid`, `metric`, `report`, `grid`, `tabs`, `accordion`, `code`, `latex`, `callout`, `image`, `video`, `form`, `confirm`, `select_option`. The bundled registry also defines `html` and `embed`, which you must allowlist explicitly. `RichOutputConfig.registry_path` and `RichOutputExtension.registry_patch` change or add components.

Required props, from `registry.json`. The props use camelCase.

| Component | Required | Common optional |
|---|---|---|
| `markdown` | `content` | `allowHtml`, `syntaxHighlight`, `mathEnabled` |
| `json` | `data` | `expandLevel`, `sortKeys` |
| `echarts` | `option` | `height`, `width`, `theme`, `loading` |
| `mermaid` | `code` | `theme` |
| `plotly` | `data` | `layout`, `config`, `height` |
| `datagrid` | `columns`, `rows` | `pageSize`, `sortable`, `filterable`, `exportable` |
| `metric` | `value`, `label` | `format`, `prefix`, `suffix`, `change`, `sparkline` |
| `code` | `code` | `language`, `filename`, `showLineNumbers`, `diff` |
| `latex` | `expression` | `displayMode` |
| `callout` | `content` | `type`, `title`, `collapsible` |
| `image` / `video` | `src` | `alt`, `caption` / `poster`, `controls` |
| `report` | `sections` | `title`, `subtitle`, `toc`, `footer` |
| `grid` | `items` | `columns`, `gap`, `equalHeight` |
| `tabs` | `tabs` | `defaultTab`, `variant` |
| `accordion` | `items` | `allowMultiple` |
| `form` / `confirm` / `select_option` | `fields` / `message` / `options` | see `interactive-hitl.md` |

`describe_component(name=...)` returns `{"component": <the full registry entry, with propsSchema and example>}`. It does not check the allowlist, and an unknown name fails the call.

## `render_component`

```python
from penguiflow.rich_output.tools import RenderComponentArgs

RenderComponentArgs(
    component="markdown",
    props={"content": "Revenue grew **12%** in Q3."},
    id="q3_note",        # optional stable id
    title="Q3 note",     # optional
    metadata=None,       # optional dict, forwarded in the event meta
)
```

`render_component` is the only way to render the components that have no typed tool. Its props are validated against the component's JSON schema, like every renderer's.

## Typed render tools

Each typed tool builds the component's props from its own arguments. `id` and `artifact_metadata` (alias `artifactMetadata`) go into the event, not the props. So does `title`, except that `render_report` also keeps it as a prop, and `render_chart_echarts` copies it into `option.title.text` when the option has no title. Snake_case names and their camelCase aliases are both accepted.

```python
from penguiflow.rich_output.tools import (
    AccordionItem,
    DataGridColumn,
    GridItem,
    RenderAccordionArgs,
    RenderChartEChartsArgs,
    RenderGridArgs,
    RenderReportArgs,
    RenderTableArgs,
    RenderTabsArgs,
    ReportComponentItem,
    ReportSection,
    TabItem,
)

chart = RenderChartEChartsArgs(
    option={
        "xAxis": {"type": "category", "data": ["Q1", "Q2", "Q3"]},
        "yAxis": {"type": "value"},
        "series": [{"type": "bar", "data": [10, 20, 15]}],
    },
    title="Revenue",          # becomes option.title.text if the option has none
    height="320px",
    id="revenue_chart",
)

table = RenderTableArgs(
    columns=[
        DataGridColumn(field="region", header="Region"),
        DataGridColumn(field="revenue", header="Revenue", format="currency", align="right"),
    ],
    rows=[{"region": "EMEA", "revenue": 1200}, {"region": "APAC", "revenue": 950}],
    page_size=25,             # alias pageSize
    title="Revenue by region",
)

report = RenderReportArgs(
    title="Q3 review",
    sections=[
        ReportSection(title="Summary", content="Revenue grew **12%**."),
        ReportSection(
            title="Detail",
            components=[
                ReportComponentItem(component="markdown", props={"content": "Notes"}, caption="Analyst notes"),
            ],
            subsections=[ReportSection(title="Method", content="...")],
        ),
    ],
    toc=True,
)

grid = RenderGridArgs(
    columns=2,
    items=[
        GridItem(component="metric", props={"value": 1200, "label": "EMEA"}),
        GridItem(component="metric", props={"value": 950, "label": "APAC"}, col_span=1),
    ],
)

tabs = RenderTabsArgs(
    tabs=[
        TabItem(label="Summary", content="Plain text or markdown"),
        TabItem(label="Chart", component="echarts", props={"option": {"series": []}}),
    ],
    default_tab=0,            # alias defaultTab
    variant="line",           # line | enclosed | pills
)

accordion = RenderAccordionArgs(
    items=[
        AccordionItem(title="Assumptions", content="..."),
        AccordionItem(title="Raw data", component="json", props={"data": {"a": 1}}, default_open=True),
    ],
    allow_multiple=True,
)
```

| Tool | Fields | Child items |
|---|---|---|
| `render_chart_echarts` | `option` (required), `title`, `height`, `width`, `theme`, `loading`, `id`, `artifact_metadata` | — |
| `render_table` | `columns: list[DataGridColumn]`, `rows: list[dict]` (both required), `page_size=10`, `sortable=True`, `filterable`, `selectable`, `exportable`, `striped=True`, `compact`, `title`, `id`, `artifact_metadata` | `DataGridColumn(field, header, width, sortable, filterable, format, align)`; `format` is `text`/`number`/`currency`/`percent`/`date`/`datetime`/`boolean` |
| `render_report` | `sections: list[ReportSection]` (required), `title`, `subtitle`, `metadata`, `toc`, `footer`, `id`, `artifact_metadata` | `ReportSection(id, title, content, components, subsections)`; `ReportComponentItem`: `artifact_ref` **or** `component` + `props`, plus `caption` |
| `render_grid` | `items: list[GridItem]` (required), `columns=2`, `gap="1rem"`, `equal_height=True`, `title`, `id`, `artifact_metadata` | `GridItem`: `artifact_ref` **or** `component` + `props`, plus `col_span`, `row_span`, `title` |
| `render_tabs` | `tabs: list[TabItem]` (required), `default_tab=0`, `variant`, `title`, `id`, `artifact_metadata` | `TabItem(label, ...)`: exactly one of `content`, `artifact_ref`, or `component` + `props`; plus `id`, `icon`, `disabled` |
| `render_accordion` | `items: list[AccordionItem]` (required), `allow_multiple=False`, `title`, `id`, `artifact_metadata` | `AccordionItem(title, ...)`: at most one of `content`, `artifact_ref`, or `component` + `props`; plus `default_open` |

`artifact_ref` slots take refs from `build_*` or earlier renders; see `builders-and-artifact-refs.md`.

## What a render emits and returns

A `render_*` call emits one planner event:

```python
from penguiflow.planner import PlannerEvent


def on_event(event: PlannerEvent) -> None:
    if event.event_type == "artifact_chunk" and event.extra["artifact_type"] == "ui_component":
        chunk = event.extra["chunk"]   # {"id", "component", "props", "title"}
        meta = event.extra["meta"]     # {"source_tool", "registry_version", ...artifact_metadata}
        ...  # hand chunk["component"] + chunk["props"] to the renderer
```

The event's `extra` also carries `stream_id="ui"`, `seq` and `done=True`. Session updates project it as a `RESULT` update with the same `artifact_type`, `stream_id`, `seq`, `chunk` and `done`. Props arrive with any `artifact_ref` already resolved.

The tool returns `RenderComponentResult(ok, component, artifact_ref, dedupe_key, summary, skipped)`. `artifact_ref` (e.g. `"artifact_3"`) is set during a planner run. If the LLM repeats the same render in the very next step, the planner skips it and returns `skipped="duplicate_render"` with the previous ref.

## Validation and limits

Before emitting, every renderer checks:
1. The component is in the registry (`Unknown component`) and in the allowlist (`Component '...' is not allowed.`).
2. The serialized payload fits `max_payload_bytes` (default 250,000).
3. The props match the component's JSON schema (`Invalid props for 'markdown': 'content' is a required property`).
4. Nested `component` + `props` children of `report`, `grid`, `tabs` and `accordion` pass the same checks, up to depth 6.

A failure becomes a tool error that tells the LLM to call `describe_component` and retry. `max_total_bytes` (default 2,000,000) is tracked in the call's `tool_context`, which the planner copies for each tool call. Under `ReactPlanner` it therefore only bounds a single call; use `max_payload_bytes` for the real limit.

## Operational defaults

- Allowlist exactly the components your frontend renders.
- Prefer a typed tool when one exists; use `render_component` for the rest.
- Keep `describe_component` and `list_artifacts` visible so the LLM can recover from schema errors.
- Don't put secrets in props: they go straight to the frontend.
