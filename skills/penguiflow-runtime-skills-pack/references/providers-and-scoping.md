# Providers, Scoping, and Composition

The `SkillProvider` protocol lets host apps supply skills at runtime, next to (or instead of) the static pack loader. Use for tenant- and user-specific skills.

## `SkillProvider` protocol

The protocol (`penguiflow.skills.SkillProvider`) has six methods, and callers don't check for missing ones, so implement all of them. Each query method takes a typed request from `penguiflow.skills` plus a keyword-only `tool_context` and an optional `capability_context`:

```python
from collections.abc import Mapping, Sequence

from penguiflow.skills import (
    RetrievalResponse,
    SkillCapabilityContext,
    SkillDirectoryEntry,
    SkillListRequest,
    SkillListResponse,
    SkillQuery,
    SkillResultDetailed,
    SkillsDirectoryConfig,
    SkillSearchQuery,
    SkillSearchResponse,
)


class MyProvider:
    async def get_relevant(
        self,
        query: SkillQuery,
        *,
        tool_context: Mapping[str, object],
        capability_context: SkillCapabilityContext | None = None,
    ) -> RetrievalResponse: ...

    async def search(
        self,
        query: SkillSearchQuery,
        *,
        tool_context: Mapping[str, object],
        capability_context: SkillCapabilityContext | None = None,
    ) -> SkillSearchResponse: ...

    async def get_by_name(
        self,
        names: list[str],
        *,
        tool_context: Mapping[str, object],
        capability_context: SkillCapabilityContext | None = None,
    ) -> list[SkillResultDetailed]: ...

    async def list(
        self,
        req: SkillListRequest,
        *,
        tool_context: Mapping[str, object],
        capability_context: SkillCapabilityContext | None = None,
    ) -> SkillListResponse: ...

    async def directory(
        self,
        config: SkillsDirectoryConfig,
        *,
        tool_context: Mapping[str, object],
        capability_context: SkillCapabilityContext | None = None,
    ) -> Sequence[SkillDirectoryEntry]: ...

    async def format_for_injection(
        self,
        skills: Sequence[SkillResultDetailed],
        *,
        max_tokens: int,
    ) -> tuple[str, int, int, bool]: ...
```

- `tool_context` is the run's `tool_context` — the scoping vehicle. Read `tenant_id`/`project_id`/`user_id` from it.
- `capability_context` lists the tools, namespaces and tool tags allowed for the request (built with `build_skill_capability_context(...)` from `penguiflow.skills.provider`). Apply it for applicability filtering when present.
- `get_relevant` drives the pre-flight injection: return the skills plus `formatted_context` (the `<skills_context>` text that goes into the system prompt).
- `format_for_injection` returns `(text, raw_tokens_est, final_tokens_est, was_trimmed)`; `skill_get` uses it.

Response shapes: `SkillSearchResponse(skills=[SkillSearchResult(...)], query=..., search_type=...)`, `SkillListResponse(skills=[SkillListEntry(...)], page=..., page_size=..., total=...)`, `RetrievalResponse(skills=..., formatted_context=..., query=..., search_type=..., top_k=..., raw_tokens_est=..., final_tokens_est=..., was_summarized=...)`.

## Wiring

```python
from penguiflow.planner import ReactPlanner
from penguiflow.skills import SkillPackConfig, SkillsConfig

planner = ReactPlanner(
    llm="gpt-4o-mini",
    catalog=...,
    skills_provider=MyProvider(),
    skills=SkillsConfig(
        enabled=True,
        skill_packs=[SkillPackConfig(name="ops", path="packs/ops")],
    ),
)
```

Or with a factory — `SkillProviderFactory = Callable[[SkillsConfig], SkillProvider]`:

```python
from penguiflow.planner import ReactPlanner
from penguiflow.skills import SkillProvider, SkillsConfig


def factory(config: SkillsConfig) -> SkillProvider:
    return MyProvider(config=config)


planner = ReactPlanner(
    llm="gpt-4o-mini",
    catalog=...,
    skills_provider_factory=factory,
    skills=SkillsConfig(enabled=True),
)
```

The factory receives the planner's `SkillsConfig` once, at construction. Use it when the provider needs the configuration (`cache_dir`, `top_k`, `max_tokens`, packs).

## Composition rules

When both static packs and a runtime provider are configured, the planner wraps them in a `CompositeSkillProvider([runtime, local])`:

1. Each call goes to the runtime provider first, then to the local SQLite store (loaded from `skill_packs`). Each is asked for `top_k`/`limit` times the number of providers.
2. Results are concatenated and deduplicated by `name`, first one wins, so **the runtime provider wins on `name` collision**. Local packs are fallback.
3. The merged list is cut to the requested size, and the pre-flight context is re-rendered within `SkillsConfig.max_tokens`.

With a runtime provider and no `skill_packs`, the planner uses the runtime provider alone. With neither, it uses an empty local store.

This lets runtime customize without rewriting the base pack: a tenant-specific override of `ops.handle_incident` while keeping the rest of the pack intact.

PII redaction (`redact_pii`) and disallowed-tool redaction are done by the local provider for pack skills only. A runtime provider must redact its own output.

## Hard rules

- `skills_provider` or `skills_provider_factory` with `SkillsConfig(enabled=False)` raises `ValueError` at construction.
- Passing both `skills_provider` and `skills_provider_factory` raises `ValueError`.
- A provider with no `skills=` argument makes the planner enable skills with defaults.

## Tenant scoping

### Pack skills are global
The local store saves pack skills with no tenant or project, and its filter treats such rows as visible to everyone. Every tenant sees every pack skill, whatever the pack's `scope_mode`. (`scope_mode` is stored, but with no tenant or project attached it doesn't restrict anything.)

### The built-in filter is fail-closed
For rows that do carry a tenant or project, the local filter reads `tenant_id` and `project_id` from `tool_context`. Such a row is returned only when the matching id is present and equal; with no id in `tool_context`, only unscoped rows come back. `user_id` isn't used.

### Tenant-specific skills belong in a runtime provider
The provider methods receive the run's `tool_context`. Use it for:
- DB filtering (`WHERE tenant_id = $1`).
- ACL checks ("does this user have access to this skill?").
- Per-tenant overrides ("tenant A uses a custom version of the email skill").

### Pattern: tenant override
Read scoping fields from `tool_context` inside each method, and return empty typed responses when `tenant_id` is missing — the fail-closed contract for multi-tenant deployments.

```python
from collections.abc import Mapping
from typing import Any

from penguiflow.skills import (
    SkillCapabilityContext,
    SkillResultDetailed,
    SkillSearchQuery,
    SkillSearchResponse,
    SkillSearchResult,
)


class TenantProvider:  # plus list, directory, get_relevant, format_for_injection
    def __init__(self, db: Any) -> None:
        self.db = db  # e.g. an asyncpg pool

    async def search(
        self,
        query: SkillSearchQuery,
        *,
        tool_context: Mapping[str, object],
        capability_context: SkillCapabilityContext | None = None,
    ) -> SkillSearchResponse:
        tenant_id = tool_context.get("tenant_id")
        if not tenant_id:  # fail-closed
            return SkillSearchResponse(
                skills=[], query=query.query, search_type=query.search_type
            )
        rows = await self.db.fetch(
            "SELECT name, title, trigger, task_type, rank FROM skills "
            "WHERE tenant_id = $1 AND search_index @@ plainto_tsquery($2) "
            "ORDER BY rank DESC LIMIT $3",
            tenant_id,
            query.query,
            query.limit,
        )
        return SkillSearchResponse(
            skills=[
                SkillSearchResult(
                    name=row["name"],
                    title=row["title"],
                    trigger=row["trigger"],
                    task_type=row["task_type"],
                    score=float(row["rank"]),
                )
                for row in rows
            ],
            query=query.query,
            search_type=query.search_type,
        )

    async def get_by_name(
        self,
        names: list[str],
        *,
        tool_context: Mapping[str, object],
        capability_context: SkillCapabilityContext | None = None,
    ) -> list[SkillResultDetailed]:
        tenant_id = tool_context.get("tenant_id")
        if not tenant_id or not names:
            return []
        rows = await self.db.fetch(
            "SELECT name, title, trigger, steps, failure_modes FROM skills "
            "WHERE tenant_id = $1 AND name = ANY($2)",
            tenant_id,
            list(names),
        )
        return [
            SkillResultDetailed(
                name=row["name"],
                title=row["title"],
                trigger=row["trigger"],
                steps=list(row["steps"]),
                failure_modes=list(row["failure_modes"]),
            )
            for row in rows
        ]
```

### Pattern: user personalization
```python
from collections.abc import Mapping

from penguiflow.skills import (
    SkillCapabilityContext,
    SkillListEntry,
    SkillListRequest,
    SkillListResponse,
)


class UserPersonaProvider:  # plus the other five methods
    async def list(
        self,
        req: SkillListRequest,
        *,
        tool_context: Mapping[str, object],
        capability_context: SkillCapabilityContext | None = None,
    ) -> SkillListResponse:
        prefs = await load_user_preferences(tool_context.get("user_id"))
        entries = [
            SkillListEntry(name=p.skill_name, title=p.title, trigger=p.trigger)
            for p in prefs
        ]
        start = (req.page - 1) * req.page_size
        return SkillListResponse(
            skills=entries[start : start + req.page_size],
            page=req.page,
            page_size=req.page_size,
            total=len(entries),
        )
```

`load_user_preferences` stands for your own lookup.

## Host-side ACL patterns

Skills can encode operational power — a "rollback prod" skill in the wrong hands is dangerous.

Layer authz at the provider:

```python
from collections.abc import Mapping

from penguiflow.skills import SkillCapabilityContext, SkillResultDetailed


class AclProvider:  # plus the other five methods
    async def get_by_name(
        self,
        names: list[str],
        *,
        tool_context: Mapping[str, object],
        capability_context: SkillCapabilityContext | None = None,
    ) -> list[SkillResultDetailed]:
        user = await self.users.load(tool_context.get("user_id"))
        skills = await self.repo.get_skills(names)
        return [skill for skill in skills if user.can_access(skill.name)]
```

Don't rely on the planner or applicability metadata to enforce ACLs. The provider is the enforcement point.

## Caching

The pre-flight retrieval runs once per run (its result is stored in the trajectory, so `resume()` reuses it). Every `skill_search`/`skill_get`/`skill_list` call goes straight to the provider; the local provider queries SQLite each time. If your provider is expensive:

- Cache `list`/`search` results per `(tenant_id, query)`.
- Invalidate when skill content changes (LISTEN/NOTIFY on Postgres; pub/sub on Redis).
- Don't cache `get_by_name` — it's already by name and rare.

Beware staleness — a user updating a skill expects to see it immediately, not after a cache TTL.

## Versioning

Skills have no version field (a `version:` key in a pack is stored but unused). If your runtime skills evolve, version them in your own store. The provider can:
- Return only the latest version (default).
- Return a specific version if the caller asks.
- Track when a skill was last shown to a user (avoid re-showing recently-seen guidance).

For audit, log `(skill name, version)` per retrieval; the planner emits `skills_retrieved`, `skill_search_query` and `skill_get` events you can hook (see `retrieval-and-tools.md`).

## Composition with [[penguiflow-rich-output]]

A common pattern: skills steer rich-output behavior.

A skill says: "When the user asks for quarterly numbers, build a chart with `build_chart_echarts` then a table with `build_table`, compose into `render_grid`."

The planner retrieves the skill, follows the steps. Use applicability gating (`required_tool_names: [render_grid, build_chart_echarts, build_table]`) so the skill only surfaces when those tools are active.

## Anti-patterns

| Anti-pattern | Why it's wrong |
|---|---|
| Tenant-private guidance in a static pack | Pack skills are global; every tenant sees them |
| Provider without ACLs in multi-tenant | One mis-scoped query leaks tenant data |
| Caching `get_by_name` results indefinitely | Updates invisible to users |
| Provider returning all skills regardless of tenant | Catastrophic in multi-tenant |
| Embedding secrets in dynamic skill text | Same risk as static packs — LLM-visible, and not redacted for you |
| Composing without name precedence awareness | Confusion when runtime overrides local pack silently |
