# ReactPlanner Configuration Templates (copy/paste)

This file is intentionally “template-heavy”. Keep `SKILL.md` lean and load this only when you need concrete code.

## Always-visible tool set (best practice)

If you use tool discovery + deferred activation, ensure these tools are **always visible** (and never denied by policy):

```py
ALWAYS_VISIBLE_TOOLS = {
    # discovery
    "tool_search",
    "tool_get",
    # skills
    "skill_search",
    "skill_get",
    "skill_list",
}

RICH_OUTPUT_TOOLS = {
    # always registered when rich output is enabled
    "render_component",
    "describe_component",
    "list_artifacts",
    "ui_form",
    "ui_confirm",
    "ui_select_option",
    # typed tools, registered per component in RichOutputConfig.allowlist
    "render_chart_echarts",  # "echarts"
    "build_chart_echarts",
    "render_report",  # "report"
    "render_table",  # "datagrid"
    "build_table",
    "render_grid",  # "grid"
    "build_grid",
    "render_tabs",  # "tabs"
    "build_tabs",
    "render_accordion",  # "accordion"
    "build_accordion",
}

ALWAYS_VISIBLE_PATTERNS = [
    # tool discovery
    "tool_search",
    "tool_get",
    # skills
    "skill_search",
    "skill_get",
    "skill_list",
    # tasks (keep visible if background tasks are enabled in your host app)
    "tasks.*",
]
```

Notes:
- `ToolSearchConfig.always_loaded_patterns` uses glob patterns (via `fnmatch`).
- Only add rich output tools to the always-visible set if rich output is enabled.
- There is no `finish` tool: the planner ends a run with the `final_response` opcode, which isn't in the catalog and can't be hidden. The default `always_loaded_patterns` is `["tasks.*", "tool_search", "tool_get"]`.

## Native LLM: minimal configuration

```py
from penguiflow.planner.react import ReactPlanner

planner = ReactPlanner(
    llm={"model": "openai/gpt-4o", "api_key": "...", "base_url": None},
    catalog=...,  # your tools (or nodes= plus registry=)
    use_native_llm=True,  # default False (LiteLLM client)
    temperature=0.0,
    json_schema_mode=True,
)
```

Model string formats supported by the native layer include:
- `"openai/gpt-4o"` or `"gpt-4o"`
- `"anthropic/claude-3-5-sonnet"` or `"claude-3-5-sonnet"`
- `"google/gemini-2.0-flash"` or `"gemini-2.0-flash"`
- `"nim/<...>"`, `"bedrock/<...>"`, `"openrouter/<...>"`, etc.

## Thinking / reasoning knobs (native reasoning + effort)

ReactPlanner exposes:
- `use_native_reasoning: bool` (default `True`)
- `reasoning_effort: str | None` (commonly `"low" | "medium" | "high"`, provider/model dependent)

Recommended defaults:
- Keep `use_native_reasoning=True`.
- Set `reasoning_effort="medium"` for most production agents.
- Use `"low"` for latency-sensitive UX loops, `"high"` for complex planning / heavy synthesis.

```py
planner = ReactPlanner(
    llm="openai/gpt-4o",
    catalog=...,
    use_native_llm=True,
    use_native_reasoning=True,
    reasoning_effort="medium",
)
```

## Tool discovery + deferred activation (recommended “enterprise default”)

```py
from penguiflow.planner.models import ToolSearchConfig
from penguiflow.catalog import ToolLoadingMode

tool_search = ToolSearchConfig(
    enabled=True,
    default_loading_mode=ToolLoadingMode.DEFERRED,
    always_loaded_patterns=ALWAYS_VISIBLE_PATTERNS,
    activation_scope="session",  # requires tool_context["session_id"] for run/resume
    max_search_results=10,
)
```

Call sites must pass `tool_context["session_id"]` whenever `activation_scope="session"`:

```py
result = await planner.run(
    "…",
    tool_context={
        "session_id": session_id,   # required for session-scoped tool activation
        "tenant_id": tenant_id,     # memory keys, your visibility policy, runtime skill providers
        "project_id": project_id,   # optional; read by runtime skill providers
    },
)
```

## Budgets and safety limits (recommended “guardrails on by default”)

These knobs are designed to prevent runaway runs and oversized context:

```py
from penguiflow.planner.models import ObservationGuardrailConfig

planner = ReactPlanner(
    llm="openai/gpt-4o",
    catalog=...,
    use_native_llm=True,
    # LLM transport-level limits
    llm_timeout_s=60.0,
    llm_max_retries=3,
    # Run-level limits
    deadline_s=120.0,        # wall-clock seconds
    hop_budget=40,           # max tool invocations
    # Context growth control
    token_budget=20_000,     # triggers trajectory summarization when history grows (heuristic estimate)
    summarizer_llm="openai/gpt-4o-mini",  # optional cheaper model for summarization
    # Output-size safety net for tool observations
    observation_guardrail=ObservationGuardrailConfig(
        max_observation_chars=50_000,
        auto_artifact_threshold=20_000,
    ),
    # Parallel safety
    absolute_max_parallel=50,
)
```

Operational guidance:
- Start with `deadline_s` + `hop_budget` even for internal agents.
- Set `token_budget` when you expect long sessions; provide `summarizer_llm` to control cost.
- Treat `ObservationGuardrailConfig` as mandatory in multi-tenant environments.

## Allowlisting: ToolPolicy (static) + ToolVisibilityPolicy (dynamic)

### ToolPolicy (static, set at planner init)

Use this for “this agent must never call X”, or for a fixed allowlist.

```py
from penguiflow.planner.models import ToolPolicy

tool_policy = ToolPolicy(
    # If set, this is a strict allowlist.
    allowed_tools=None,
    # Always deny dangerous tools here.
    denied_tools=set(),
    # Optional: enforce required tags on every tool used by this planner.
    require_tags=set(),
)
```

If you use an allowlist, bake in the always-visible set:

```py
tool_policy = ToolPolicy(
    allowed_tools=set(user_allowlist) | ALWAYS_VISIBLE_TOOLS,
)
```

### ToolVisibilityPolicy (dynamic, per run/resume)

Use this for per-tenant/user/request filtering without building a new planner instance.

Hard rule: if you apply this, it must keep the always-visible set visible.

```py
from collections.abc import Mapping, Sequence
from penguiflow.catalog import NodeSpec
from penguiflow.planner.models import ToolVisibilityPolicy


class TenantToolVisibility(ToolVisibilityPolicy):
    def __init__(self, *, allowed: set[str]) -> None:
        self._allowed = set(allowed) | ALWAYS_VISIBLE_TOOLS

    def visible_tools(self, specs: Sequence[NodeSpec], tool_context: Mapping[str, object]) -> Sequence[NodeSpec]:
        return [spec for spec in specs if spec.name in self._allowed]
```

Usage:

```py
tool_visibility = TenantToolVisibility(allowed=tenant_tool_allowlist)
result = await planner.run("…", tool_context=tool_context, tool_visibility=tool_visibility)
```

## Memory hooks (ShortTermMemoryConfig)

If you enable short-term memory, pass **tenant/user/session identifiers** in `tool_context` (or pass an explicit `memory_key`). A derived key needs only `session_id`: a missing `tenant_id` becomes `"default"` and a missing `user_id` `"anonymous"`, so always send all three in multi-tenant apps. Details in [[penguiflow-memory]].

```py
from penguiflow.planner.memory import MemoryIsolation, ShortTermMemoryConfig

short_term_memory = ShortTermMemoryConfig(
    strategy="rolling_summary",
    isolation=MemoryIsolation(
        tenant_key="tenant_id",
        user_key="user_id",
        session_key="session_id",
        require_explicit_key=True,  # default: no memory without a memory_key or a session_id
    ),
    summarizer_model="openai/gpt-4o-mini",  # optional; uses planner LLM if omitted
    # Optional hooks (best-effort, must be awaitable):
    # on_turn_added=...,
    # on_summary_updated=...,
    # on_health_changed=...,
)
```

Then:

```py
result = await planner.run(
    "…",
    tool_context={"tenant_id": tenant_id, "user_id": user_id, "session_id": session_id},
)
```

If you want to bypass `tool_context` extraction, you can pass an explicit `memory_key`:

```py
from penguiflow.planner.memory import MemoryKey

result = await planner.run(
    "…",
    memory_key=MemoryKey(tenant_id=tenant_id, user_id=user_id, session_id=session_id),
)
```

## Pre-flight memory context hook (LLMContextHook)

Use `llm_context_hooks` when you need to inject **external/persistent memory** (profiles, preferences, entitlements, prior summaries) into `llm_context` **once before the first LLM call**.

Important behavior:
- Hooks run on `planner.run(...)` only (pre-flight, not on `resume()`) and are best-effort: a failing hook is logged (`llm_context_hook_failed` event) and skipped.
- Hooks run **after** short-term memory context is applied, so your hook should usually **merge**, not replace.
- Do not put secrets in `llm_context` (it is LLM-visible). Keep secrets in `tool_context`.

```py
from collections.abc import Mapping
from typing import Any

from penguiflow.planner.llm_context_hooks import LLMContextHookInput


class ExternalMemoryHook:
    # Optional attributes used by the runtime for logging/merging behavior.
    name = "external_memory"
    overwrite = False

    async def before_run(self, inp: LLMContextHookInput) -> Mapping[str, Any] | None:
        # Provide your own store/service via tool_context (tool-only, not LLM-visible).
        store = inp.tool_context.get("memory_store")
        if store is None or inp.memory_key is None:
            return None

        # Example: fetch user profile/preferences by tenant/user (implementation-defined).
        profile = await store.get_user_profile(
            tenant_id=inp.memory_key.tenant_id,
            user_id=inp.memory_key.user_id,
        )
        if not profile:
            return None

        # Merge-friendly: add a new key instead of overwriting "memories".
        return {"user_profile": profile}
```

Wire it into the planner:

```py
from penguiflow.planner.react import ReactPlanner

planner = ReactPlanner(
    llm="openai/gpt-4o",
    catalog=...,
    use_native_llm=True,
    llm_context_hooks=[ExternalMemoryHook()],
)
```

And pass the store + stable IDs at call time:

```py
result = await planner.run(
    "…",
    tool_context={
        "tenant_id": tenant_id,
        "user_id": user_id,
        "session_id": session_id,
        "memory_store": memory_store,
    },
)
```

## Guardrails (optional)

If you integrate guardrails, pass a `guardrail_gateway` into the planner and keep the conversation window small:

```py
from penguiflow.planner.react import ReactPlanner

planner = ReactPlanner(
    llm="openai/gpt-4o",
    catalog=...,
    use_native_llm=True,
    guardrail_gateway=guardrail_gateway,
    guardrail_conversation_history_turns=1,
)
```

## Steering (optional)

Steering lets your host app inject operator messages/cancellation while a run is in progress.

```py
from penguiflow.steering import SteeringCancelled, SteeringInbox

steering_inbox = SteeringInbox()
try:
    result = await planner.run(
        "…", steering=steering_inbox, tool_context=tool_context
    )
except SteeringCancelled:
    ...  # the run was cancelled through the inbox
```

To push steering events:

```py
from penguiflow.state.models import SteeringEvent, SteeringEventType

await steering_inbox.push(
    SteeringEvent(
        session_id=session_id,
        task_id="foreground",
        event_type=SteeringEventType.CANCEL,
        payload={"reason": "operator_cancelled", "hard": True},
    )
)
```

Common patterns:
- `INJECT_CONTEXT`: add operator notes/corrections without restarting the run (`payload={"text": ...}`).
- `CANCEL`: `planner.run()` raises `SteeringCancelled` at the next check (between steps, or while a tool runs). In a `StreamingSession`, cancelling a task also cancels its child tasks unless they were spawned with `propagate_on_cancel="isolate"`.

## HITL (pause/resume) patterns

Pause/resume is enabled by default (`pause_enabled=True`). When a run pauses, `planner.run(...)` returns a `PlannerPause` with a `resume_token`.

Minimum host responsibilities:
- Store the `resume_token` and show the pause payload to the user/operator.
- Call `planner.resume(token, user_input=...)` when you have input.

```py
from penguiflow.planner import PlannerPause

outcome = await planner.run("…", tool_context=tool_context)
if isinstance(outcome, PlannerPause):
    # Persist outcome.resume_token + outcome.payload in your app
    ...

# Later:
outcome2 = await planner.resume(
    outcome.resume_token, user_input="approved", tool_context=tool_context
)
```

Durable HITL (recommended):
- Pass `state_store=...` into `ReactPlanner(...)`.
- Your `state_store` should implement `SupportsPlannerState`:
  - `async save_planner_state(token: str, payload: dict) -> None`
  - `async load_planner_state(token: str) -> dict | None`

## Skills: enable and load packs

```py
from penguiflow.skills.models import SkillsConfig, SkillPackConfig

skills = SkillsConfig(
    enabled=True,
    redact_pii=True,
    top_k=6,
    max_tokens=2000,
    skill_packs=[
        SkillPackConfig(
            name="product",
            path="packs/product",  # file or directory of *.skill.* files
            # format="md" | "yaml" | "json" | "jsonl"  (optional)
        ),
    ],
)

planner = ReactPlanner(llm="openai/gpt-4o", catalog=..., skills=skills)
```

Operational notes:
- Skills are loaded at planner init when `SkillsConfig.enabled=True`, and the relevant ones are added to the system prompt at the start of each run.
- Pack skills are global: every tenant and project sees them, whatever `scope_mode` says. Put tenant-specific skills in a runtime provider (`skills_provider=`) that filters on `tool_context["tenant_id"]`; see [[penguiflow-runtime-skills-pack]].
- If a `ToolVisibilityPolicy` hides tools for a request, skills that require them are filtered out and skill text naming them is redacted. Tools removed by `ToolPolicy` aren't known to the planner at all, so their names aren't redacted; gate those skills with `required_tool_names`.

## Rich output: enable tools + prompt catalog

```py
from penguiflow.registry import ModelRegistry
from penguiflow.rich_output.runtime import RichOutputConfig, attach_rich_output_nodes

registry = ModelRegistry()

rich_output = RichOutputConfig(
    enabled=True,
    # allowlist=("markdown", "mermaid", "datagrid", ...)
    include_prompt_catalog=True,
)

rich_nodes = attach_rich_output_nodes(registry, config=rich_output)
```

If you enable rich output, also:
- Add `RICH_OUTPUT_TOOLS` (the ones your `allowlist` registers) to `ALWAYS_VISIBLE_TOOLS` / `ALWAYS_VISIBLE_PATTERNS`.
- Include `rich_nodes` in the planner tool catalog (`nodes=` with the same `registry=`).

## Background tasks: enable + expose tasks.* tools (host app responsibility)

ReactPlanner can emit background-task opcodes (`task.subagent` / `task.tool`). Your host app must expose task tools (e.g. `tasks.spawn`) and a task service.

```py
from penguiflow.planner.models import BackgroundTasksConfig

background_tasks = BackgroundTasksConfig(
    enabled=True,  # adds background-task guidance to the system prompt
    allow_tool_background=False,  # True: tools with extra["background"] run as tasks
)

planner = ReactPlanner(
    llm="openai/gpt-4o", catalog=..., background_tasks=background_tasks
)
```

`BackgroundTasksConfig` uses `extra="forbid"`: it has only `enabled`, `include_prompt_guidance`, `allow_tool_background`, `default_group_merge_strategy`, `default_group_report`, `auto_seal_groups_on_foreground_yield`, `retain_turn_timeout_s`, `background_continuation_max_hops` and `background_continuation_cooldown_s`, and any other field raises `ValidationError`. The planner reads the first three; sessions read the rest from the config passed to `StreamingSession.configure_background_tasks(...)` or `InProcessTaskService(background_config=...)`.

`tasks.*` tools expect the foreground `tool_context` to include a task service under `"task_service"`:

```py
tool_context = {
    "session_id": session_id,
    "task_service": task_service,  # required for tasks.spawn / tasks.list / ...
}
```

Operator-oriented defaults to consider:
- Task limits live in `SessionLimits`, passed to `SessionManager(limits=...)` (defaults: 8 tasks per session, 4 in the background, 3 running at once, 900 s per task).
- Per-task options are `tasks.spawn` arguments: `context_depth="summary"` (or `"none"`) for privacy/cost unless tasks need full context, and `propagate_on_cancel="cascade"` (the default) to avoid orphaned work.
- Proactive report-back is set up with `setup_proactive_reporting()` or `StreamingSession.configure_proactive_reporting()`.

```py
from penguiflow import SessionLimits, SessionManager

manager = SessionManager(
    limits=SessionLimits(
        max_tasks_per_session=50,
        max_background_tasks=10,
        max_concurrent_tasks=5,
        max_task_runtime_s=3600,
    ),
)
```

See [[penguiflow-background-tasks]] for the task service, `tasks.*` arguments and merge strategies.

To expose the default `tasks.*` tool set in your catalog, you can include prebuilt specs:

```py
from penguiflow.sessions.task_tools import build_task_tool_specs

task_specs = build_task_tool_specs()
```

`build_task_tool_specs()` returns `NodeSpec` entries with their own registry; use it as a starting point if you already pass `catalog=` into `ReactPlanner(...)`.

## Full “golden” factory (native LLM + discovery + skills + rich output)

This skeleton assumes you build the tool catalog from `nodes + registry` (recommended when you want `ToolSearchConfig.default_loading_mode=DEFERRED` to apply broadly).

```py
from penguiflow.node import Node
from penguiflow.planner.models import ToolExamplesConfig, ToolSearchConfig
from penguiflow.planner.react import ReactPlanner
from penguiflow.registry import ModelRegistry
from penguiflow.rich_output.runtime import RichOutputConfig, attach_rich_output_nodes
from penguiflow.skills.models import SkillPackConfig, SkillsConfig
from penguiflow.catalog import ToolLoadingMode


def build_planner(*, llm_api_key: str) -> ReactPlanner:
    registry = ModelRegistry()

    # 1) Your app tools (examples only)
    nodes: list[Node] = [
        # Node(my_tool_fn, name="my_tool"),
    ]

    # 2) Optional: rich output tools
    rich_nodes = attach_rich_output_nodes(
        registry,
        config=RichOutputConfig(enabled=True, include_prompt_catalog=True),
    )
    nodes.extend(rich_nodes)

    # 3) Planner configuration knobs
    always_visible_patterns = [
        "tool_search",
        "tool_get",
        "skill_search",
        "skill_get",
        "skill_list",
        "tasks.*",
        # rich output (the default allowlist registers all of these; the
        # globs also match your own tools with these prefixes)
        "render_*",
        "build_*",
        "describe_component",
        "list_artifacts",
        "ui_*",
    ]
    tool_search = ToolSearchConfig(
        enabled=True,
        default_loading_mode=ToolLoadingMode.DEFERRED,
        always_loaded_patterns=always_visible_patterns,
        activation_scope="session",
    )

    skills = SkillsConfig(
        enabled=True,
        redact_pii=True,
        skill_packs=[
            SkillPackConfig(name="default", path="packs/default"),
        ],
    )

    planner = ReactPlanner(
        llm={"model": "openai/gpt-4o", "api_key": llm_api_key},
        use_native_llm=True,
        nodes=nodes,
        registry=registry,
        temperature=0.0,
        json_schema_mode=True,
        tool_search=tool_search,
        tool_examples=ToolExamplesConfig(enabled=True),
        skills=skills,
        stream_final_response=True,
    )
    return planner
```
