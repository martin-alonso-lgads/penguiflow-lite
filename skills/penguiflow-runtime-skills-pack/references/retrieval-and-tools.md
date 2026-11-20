# Retrieval, Tools, and `skill_propose`

How the planner actually finds and uses skills at runtime.

## The skill tool surface

When `SkillsConfig.enabled=True`, the planner adds three tools (unless your catalog already has tools with those names), all with `loading_mode=ALWAYS`:

| Tool | Args | Returns |
|---|---|---|
| `skill_search` | `query`, `search_type="fts"` (`fts`/`regex`/`exact`), `limit=8` (1-20), `task_type`, `tags=[]`, `namespace` | `SkillSearchResponse(skills=[{name, title, trigger, task_type, score}], query, search_type)` |
| `skill_get` | `names` (1-10), `format="injection"` (or `"raw"`), `max_tokens=1500` (200-6000) | `SkillGetResponse(skills=[SkillResultDetailed], formatted_context)`; `formatted_context` is empty for `"raw"` |
| `skill_list` | `page=1`, `page_size=20` (1-100), `task_type`, `origin` (`pack`/`learned`), `tags=[]`, `namespace` | `SkillListResponse(skills=[{name, title, trigger, task_type}], page, page_size, total)` |

`tags` is an AND filter against the skill's declared tags. `namespace` is a dot-prefix match against the skill name (`name == namespace` or `name.startswith(namespace + ".")`). Empty values disable the filters.

`skill_search` is keyword search, not semantic: SQLite FTS5 (bm25 ranking, porter stemming) over `name`, `title`, `trigger`, `description` and `tags`. If FTS5 isn't available it falls back to a regex match (`fts_fallback_to_regex=True`), else to exact matching; the response's `search_type` says which ran.

The planner can call these at any step. They are ordinary catalog tools, so tool policy and visibility apply to them like any other tool (e.g., hide them for A2A specialist responses where the LLM shouldn't browse skills).

## Pre-flight injection ("relevant skills")

At the **start** of every `planner.run(...)`, the planner runs an automatic retrieval:

1. Search with the run's query (`SkillQuery(task=query, top_k=SkillsConfig.top_k)`, FTS).
2. Drop skills that fail applicability (the request's allowed tools, namespaces and tool tags); keep searching until `top_k` applicable skills or no more matches.
3. Redact PII (if `redact_pii`) and the names of disallowed tools.
4. Render them as a `<skills_context>` block within `max_tokens`: each skill gets name, title, trigger, `required_*`, steps, then preconditions and failure modes. If a skill doesn't fit, its optional sections go, then its steps are cut to 3; if it still doesn't fit, no more skills are added.
5. Append the block to the **system prompt** (not `llm_context`), so the LLM sees it in its first prompt — no tool call needed.

The result is stored in the trajectory, so `resume()` reuses it, and the planner emits a `skills_retrieved` event.

Pros:
- Skills surface even when the LLM doesn't know to call `skill_search`.
- One round-trip cheaper than tool-call retrieval.

Cons:
- `top_k` mistakes (irrelevant retrievals) waste tokens.
- Retrieval uses only the current run's query; each new `run()` retrieves again from scratch.

Tune:
- `top_k=6` — typical.
- `max_tokens=2000` — keeps the injection bounded.
- Trimming to `max_tokens` is automatic; there is no summarization step.

## Directory rendering

`SkillsConfig.directory` (a `SkillsDirectoryConfig`) is **on by default**. It adds a `<skill_directory>` block to the system prompt, before `<skills_context>`, with one line per skill: `- <name> — <label>`, where the label comes from `include_fields` (default `["name", "title", "trigger"]`; `task_type` is also allowed).

```python
from penguiflow.skills import SkillsConfig, SkillsDirectoryConfig

SkillsConfig(
    enabled=True,
    directory=SkillsDirectoryConfig(
        max_entries=30,  # default (1-200)
        include_fields=["name", "title"],
        selection_strategy="pinned_then_recent",  # or "pinned_then_top"
    ),
)
```

Entries are the packs' `pinned_skill_names` first, then the most recently used (or most used) skills, after applicability filtering. Set `SkillsDirectoryConfig(enabled=False)` to drop the block.

When to keep it:
- Interactive UIs ("what can you help with?").
- Multi-turn conversations where the user may pivot topics.

When to turn it off:
- Token budget is tight.
- Skills are domain-specific and discoverability isn't a goal.

## Tool-aware retrieval

The planner combines skill applicability with tool visibility:

- If a skill's `required_tool_names`, `required_namespaces` or `required_tags` aren't satisfied by the tools allowed for this request → the skill is filtered out (pre-flight, directory, `skill_search`, `skill_get`, `skill_list`).
- If a skill's text names a known tool that isn't allowed → the name is replaced with "a suitable tool", or "a suitable tool (use tool_search)" when `tool_search` is allowed.

This prevents capability leakage: a request without access to `db.write` won't see (or be guided toward) skills that name `db.write` directly. Both happen in the local (pack) provider; a runtime provider applies `capability_context` itself.

## `skill_propose` drafting

Enable:
```python
from penguiflow.skills import SkillProposalConfig, SkillsConfig

SkillsConfig(enabled=True, proposal=SkillProposalConfig(enabled=True))
```

This adds a `skill_propose` tool and a line of prompt guidance about it. The LLM calls it with `source_material` (required) and optional `task_type`, `title_hint`, `trigger_hint` and `required_tool_names`/`required_namespaces`/`required_tags` hints. The planner's own LLM drafts the skill, and the tool returns a `SkillProposeResponse`:

```python
from penguiflow.skills import (
    SkillDefinition,
    SkillProposalDraft,
    SkillProposeResponse,
)

SkillProposeResponse(
    draft=SkillProposalDraft(
        skill=SkillDefinition(
            name="reviews.q4_review",
            title="Conduct a Q4 review",
            trigger="When the user asks for a Q4 retrospective.",
            task_type="domain",
            steps=["Gather metrics from the quarter.", "Summarize wins."],
            failure_modes=["If metrics are missing, ask for the source."],
        ),
        warnings=[],
        assumptions=["The quarter ends in December."],
    )
)
```

**Critical:** `skill_propose` **does not persist anything**. It's a pure drafting tool. The host app is responsible for:
1. Receiving the draft (it's the tool's observation; read it from the trajectory or a `skill_propose` event hook).
2. Showing it to a reviewer (admin UI, PR, ticket).
3. Persisting it (writing it to a pack file, adding it to the runtime provider).
4. Rebuilding the planner so the pack is re-synced.

This keeps skill curation safe: agent-assisted drafting without auto-publishing.

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| Pre-flight injection misses obvious skills | `trigger`/`title` words don't match the user's words | Rewrite triggers in user-intent terms |
| Wrong or stale skills retrieved | A pack changed but the planner wasn't rebuilt, or `update_existing_pack_skills=False` | Rebuild the planner; as a last resort delete `<cache_dir>/skills.db` |
| Skills visible via `skill_list` but not in pre-flight | Not matched by the query, or `top_k` reached | Improve the trigger; check the `skills_retrieved` event |
| Token budget exceeded | `max_tokens` low or skills are huge | Lower `top_k`, raise `max_tokens`, or trim skill text |
| `skill_propose` returns garbage | LLM doesn't have good source material | Pre-fetch source content; give the model clear context |

## Observability hooks

`PlannerEvent` types emitted by the skills subsystem, with their `extra` keys:
- `skill_pack_loaded` — `pack_name`, `skill_count`, `updated_count`; at planner construction, one per pack.
- `skills_retrieved` — `skill_count`, `top_k_used`, `raw_tokens_est`, `final_tokens_est`, `was_summarized` (true when content was trimmed); pre-flight.
- `skill_directory_rendered` — `count`; when the directory block is added.
- `skill_search_query` — `query`, `requested_search_type`, `effective_search_type`, `results_count`, `tags`, `namespace`.
- `skill_get` — `names`, `returned_count`, `max_tokens`, `final_tokens_est`.
- `skill_list` — `filters` (`task_type`, `origin`, `tags`, `namespace`), `returned_count`.
- `skill_propose` — `task_type`, the three `required_*` hint lists, `warning_count`, `assumption_count`.

`skill_pack_loaded` fires during `ReactPlanner(...)`, before you can attach an `event_callback` after construction; pass `event_callback=` to the constructor to see it.

Track:
- Top-k retrieval hit rate (skills shown vs subsequently used).
- `skill_search` calls per run (high count = pre-flight retrieval is failing).
- `skill_propose` invocation rate (gauge of agent-driven authoring activity).

## Cross-skill patterns

### Pattern: rich-output layout skill

```markdown
---
name: rich_output.report_layout
trigger: When you need to produce a multi-section report with charts and tables.
task_type: domain
required_tool_names: [render_report, build_chart_echarts, build_table]
steps:
  - Build charts and tables with build_* tools first.
  - Compose them into render_report sections that reference the built artifacts.
  - Set the title to match the user's intent verbatim.
---
```

### Pattern: safety skill for a destructive tool

```markdown
---
name: safety.confirm_before_delete
trigger: When the user asks to delete data.
task_type: domain
required_tool_names: [delete_record, ui_confirm]
steps:
  - Call ui_confirm with a clear preview before delete_record.
  - If the user declines, propose alternatives (archive, soft-delete).
  - Never call delete_record without confirmation.
---
```

### Pattern: skill on tool discovery

```markdown
---
name: discovery.use_tool_search
trigger: When you can't find a tool by name.
task_type: unknown
required_tool_names: [tool_search, tool_get]
steps:
  - Call tool_search with the capability description (not a guessed tool name).
  - Inspect results; call tool_get on the most promising one.
  - Then call the tool.
---
```

These steer planner behavior beyond what the prompt alone teaches.

## Anti-patterns

- **Skills that duplicate prompt content** — wastes tokens; the prompt already says it.
- **Skills with vague triggers** — retrieval misses them.
- **Skills mentioning forbidden tools** — capability leak; gate with `required_tool_names`.
- **Skills as documentation** — they're operational playbooks, not docs. Docs live elsewhere.
- **One mega-skill** — split into focused skills; retrieval picks the right one.
- **Skipping `name` field** — generated names derive from the title or trigger and change with them; explicit names version-control cleanly.
- **Invented `task_type` values** (`tooling`, `safety`, `meta`) — validation fails and the skill is skipped with only a `skill_pack_invalid` log warning.
