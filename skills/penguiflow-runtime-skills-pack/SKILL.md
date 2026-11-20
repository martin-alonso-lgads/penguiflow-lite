---
name: penguiflow-runtime-skills-pack
description: Author "skill packs" — playbooks the PenguiFlow ReactPlanner retrieves at runtime and injects into its system prompt, also reachable via `skill_search`/`skill_get`/`skill_list` (NOT the same as the SKILL.md usage guides in the project's `skills/` directory; these are runtime artifacts stored in a SQLite skill store). Configure `SkillsConfig(enabled=True, skill_packs=[SkillPackConfig(...)], top_k=..., redact_pii=True)`, pick a pack format (`*.skill.md`/`.yaml`/`.json`/`.jsonl`), set a valid `task_type` (`browser`/`api`/`code`/`domain`/`unknown`), declare applicability via `required_tool_names`/`required_namespaces`/`required_tags`, keep pack skills global and put tenant-specific skills in a runtime `skills_provider`/`skills_provider_factory`, optionally enable non-persisting `skill_propose` drafting and tune the `SkillsDirectoryConfig` listing. Use when a user says "author a skill pack", "playbooks for my planner", "skill_search", "runtime skills provider", or names `SkillsConfig`.
---

# Authoring Runtime Skill Packs for the Planner

## Important: naming collision (read first)

This skill is about **planner-consumed skill packs** — curated playbooks that the planner injects into its system prompt and that the LLM can also fetch via `skill_search` / `skill_get` / `skill_list`. These are PenguiFlow runtime artifacts stored in a SQLite skill store.

These are **NOT** the same as the `SKILL.md` files inside the project's `skills/` directory. Those are **usage guides** for people and coding agents building with PenguiFlow. The two share the word "skill" but live in completely different layers:

| Layer | What it is | Format | Audience |
|---|---|---|---|
| Usage guides (this directory) | How-to guides for building with PenguiFlow | `SKILL.md` + frontmatter + `references/*` | Developers and coding agents |
| Planner skill packs (this skill) | Playbooks the ReactPlanner retrieves | `*.skill.md`/`.skill.yaml`/`.skill.json`/`.skill.jsonl` | The LLM driving the planner at runtime |

Don't try to reuse SKILL.md files as planner skill packs (or vice versa). Different schemas, different consumers.

## When to use
- Codify operational procedures ("how we handle an incident") as planner-retrievable playbooks.
- Standardize agent behavior across teams without copy-pasting prompt text.
- Surface domain conventions (titles, layouts, when to use which chart) to the planner.
- Author capability-gated guidance (applicability fields), plus tenant- or persona-specific guidance through a runtime provider.

## When NOT to use
- Long-term memory or knowledge base → not the right tool; build retrieval separately.
- Tools — skills are documents, they don't execute.
- Frequently-changing data — packs are versioned like code, not live data.
- Trying to override planner configuration knobs — that's [[penguiflow-reactplanner-config]].

## Hard boundaries
This skill covers **authoring and configuring** runtime skill packs and the providers that supply them. Planner knobs (always-visible tools, ToolPolicy, etc.) live in `penguiflow-reactplanner-config`. Rich-output integration with skills lives in `penguiflow-rich-output`.

## Workflow

### 1) Decide static packs vs runtime provider
| Choice | Use when |
|---|---|
| **Static packs** (`skill_packs=[...]`) | In-repo curated playbooks, versioned with code, visible to every tenant. |
| **Runtime provider** (`skills_provider=...`) | Per-tenant or per-user skills, dynamic content from a host service. |
| **Both** | Static packs as the baseline, runtime overlays for tenant-specific guidance. Runtime wins on name collision. |

### 2) Author a pack
Pick a format: `*.skill.md` (YAML frontmatter, most readable; the Markdown body is ignored), `*.skill.yaml`/`.yml`, `*.skill.json`, `*.skill.jsonl` (bulk import). Required: `trigger` (when to use), `steps` (the playbook). Recommended: `name`, `title`, `task_type` (one of `browser`, `api`, `code`, `domain`, `unknown`; default `unknown`; any other value makes the loader skip the skill), applicability (`required_tool_names`/`required_namespaces`/`required_tags`), `preconditions`, `failure_modes`. Full schema and a runnable Markdown example in `references/pack-formats.md`.

### 3) Wire `SkillsConfig`
```python
from penguiflow.planner import ReactPlanner
from penguiflow.skills import SkillPackConfig, SkillsConfig

skills = SkillsConfig(
    enabled=True,  # default False; must opt in
    cache_dir=".penguiflow",  # default; SQLite store at <cache_dir>/skills.db
    skill_packs=[SkillPackConfig(name="ops", path="packs/ops")],
    top_k=6,  # default (1-20)
    max_tokens=2000,  # default (200-10000)
    redact_pii=True,  # default
)

planner = ReactPlanner(llm="gpt-4o-mini", catalog=..., skills=skills)
```
At construction the planner loads the packs into the store and adds `skill_search`/`skill_get`/`skill_list` (always loaded). At the start of each `run()` it retrieves up to `top_k` skills relevant to the query and appends them to the **system prompt** as `<skills_context>`, after a `<skill_directory>` listing (on by default).

### 4) (Optional) Wire a runtime provider
```python
from penguiflow.planner import ReactPlanner
from penguiflow.skills import SkillsConfig

planner = ReactPlanner(
    llm="gpt-4o-mini",
    catalog=...,
    skills_provider=TenantSkillProvider(),  # or skills_provider_factory=...
    skills=SkillsConfig(enabled=True, skill_packs=[...]),
)
```

`TenantSkillProvider` implements all six `SkillProvider` methods (`get_relevant`, `search`, `get_by_name`, `list`, `directory`, `format_for_injection`); see `references/providers-and-scoping.md` for the signatures and a full example. Runtime providers are Python-only — use for per-tenant skills loaded from a host service, user personalization, or composition with the host app's permission model. Static packs and runtime providers compose; runtime wins on name collision.

### 5) Scope per tenant/project
Pack skills are **always global**: the store saves them without a tenant or project, so every caller sees them, whatever the pack's `scope_mode`. Never put tenant-private guidance in a pack.

The built-in scope filter reads `tenant_id` and `project_id` from `tool_context` and fails closed: a skill scoped to a tenant or project is returned only when that id is present and matches. For tenant-specific skills, use a runtime provider that reads `tool_context` and returns nothing when `tenant_id` is missing.

### 6) Use applicability metadata
Per-skill `required_tool_names` / `required_namespaces` / `required_tags`. The planner checks them against the tools allowed for the request (after tool visibility), their namespaces (the part of the name before the first `.`, or `core`), and the tags declared on those tools. A skill surfaces only when **all** populated requirements are satisfied. Separately, names of disallowed tools in skill text are replaced with "a suitable tool" (plus "use tool_search" when `tool_search` is allowed).

### 7) (Optional) `skill_propose` drafting and directory
`SkillsConfig(..., proposal=SkillProposalConfig(enabled=True))` adds a non-persisting `skill_propose` drafting tool. The `<skill_directory>` listing is on by default; tune it with `directory=SkillsDirectoryConfig(max_entries=..., include_fields=[...])` or turn it off with `SkillsDirectoryConfig(enabled=False)`.

### 8) Treat packs as LLM-visible
Anything in a skill enters the system prompt. **No secrets, no PII, no tenant-private identifiers.** `redact_pii=True` (emails, phone numbers, token-like strings, URL query strings) is a safety net, not a substitute for clean authoring, and it applies only to pack skills; a runtime provider must redact its own output.

## Troubleshooting (fast checks)
- **`ValueError: skills_provider / skills_provider_factory require skills.enabled=True`** — you passed a provider with `SkillsConfig(enabled=False)`. Omit `skills=` (the planner then enables defaults) or set `enabled=True`. Passing both a provider and a factory also raises.
- **`skill_search is not configured`** — the tool ran on a planner with no skill provider, e.g. you added the skill tools to your own catalog while skills are disabled.
- **LLM calls `skill_propose` and it doesn't exist** — `proposal.enabled` is False, so the tool isn't registered.
- **Pack ignored / no skills available** — path doesn't exist on the worker (`skill_pack_missing` warning); Markdown frontmatter isn't valid YAML (file skipped silently); an entry fails validation (`skill_pack_invalid` warning: empty `trigger`/`steps`, or `task_type` outside `browser|api|code|domain|unknown`). A malformed `.skill.json`/`.skill.yaml`/`.skill.jsonl` file raises `ValueError` when the planner is built.
- **Skill surfaces but mentions a forbidden tool** — applicability misconfigured; add `required_tool_names` or rewrite the skill to use `tool_search`.
- **Cross-tenant skill leak** — pack skills are global by design. Move tenant-specific skills to a runtime provider that filters on `tool_context["tenant_id"]`.
- **Top-k retrieval misses obvious skills** — search is SQLite full-text (FTS5) over name, title, trigger, description and tags, with a regex fallback; steps aren't searched. Rewrite `trigger` with the words users say.
- **Skills bloat prompts** — lower `max_tokens` and `top_k`, or trim skill text. The formatter already drops `preconditions`/`failure_modes`, then cuts to 3 steps, then stops adding skills to fit `max_tokens`.

## Worked example
- No bundled example ships a skill pack; `tests/test_skills_pack_loader.py` and `tests/test_skills_runtime_provider.py` show canonical pack layouts and runtime-provider wiring.

## References (load only as needed)
- `references/pack-formats.md` — every supported format with full schemas, applicability fields, validation rules.
- `references/providers-and-scoping.md` — `SkillProvider` protocol, runtime providers, composition with local packs, tenant scoping, host-side ACL patterns.
- `references/retrieval-and-tools.md` — `skill_search`/`skill_get`/`skill_list`, pre-flight injection, directory rendering, tool-aware redaction, `skill_propose` lifecycle.
