# Pack Formats and Schemas

Four formats, recognized by file suffix. Pick by audience and tooling.

## Format: Markdown (`*.skill.md`)

Most readable. Recommended for in-repo packs that humans review. Only the YAML frontmatter is read; one skill per file.

```markdown
---
name: ops.handle_incident                # unique across all packs
title: Handle a production incident      # human-readable
trigger: When an on-call incident is declared and you need a repeatable response.
task_type: domain                        # browser | api | code | domain | unknown
required_tool_names: [pagerduty_ack, slack_post]
tags: [incident, oncall]
description: Standard on-call response for production incidents.
preconditions:
  - The incident has a named owner.
steps:
  - Confirm impact and affected tenants/projects.
  - Identify the failing dependency and roll back if needed.
  - Mitigate user impact first, then diagnose root cause.
  - File a follow-up incident review issue.
failure_modes:
  - If metrics are missing, check telemetry pipeline health first.
  - If the rollback fails, escalate to platform on-call.
---
```

The body below the frontmatter is discarded. Put everything the model should see into the frontmatter fields.

## Format: YAML (`*.skill.yaml` / `*.skill.yml`)

Same fields as the Markdown frontmatter. A file holds one mapping or a list of them.

```yaml
name: ops.handle_incident
title: Handle a production incident
trigger: When an on-call incident is declared.
task_type: domain
steps:
  - Confirm impact.
  - Identify failing dependency.
  - Mitigate before diagnose.
```

Use when:
- Editing in a YAML-aware editor / pipeline.
- Generating skills from a script.

## Format: JSON (`*.skill.json`)

A single skill object, or a list of skill objects in one file.

```json
{
  "name": "ops.handle_incident",
  "title": "Handle a production incident",
  "trigger": "When an on-call incident is declared.",
  "task_type": "domain",
  "steps": [
    "Confirm impact.",
    "Identify failing dependency.",
    "Mitigate before diagnose."
  ]
}
```

Use when:
- Importing from a system that emits JSON.
- Generating skills programmatically with a schema validator in the loop.

## Format: JSONL (`*.skill.jsonl`)

One skill object per line; blank lines are skipped. Bulk import friendly.

```
{"name": "ops.handle_incident", "trigger": "When an incident is declared.", "steps": ["Confirm impact."]}
{"name": "ops.rollback_deploy", "trigger": "When a deploy breaks production.", "steps": ["Roll back."]}
```

Use when:
- Bulk-loading thousands of skills from an export.
- Working with stream processors.

## Field reference

Every entry is validated as `penguiflow.skills.SkillDefinition`.

### Required
- `trigger: str` — when the skill applies. Searched and rendered.
- `steps: list[str]` — the playbook. A single string becomes one step. Rendered, not searched.

### Recommended
- `name: str` — unique across every pack and provider. Dot-namespaced (`area.action`) so `namespace=` filters work. Without it, the loader names the skill `pack.<pack name>.<slug of title, else trigger, else file name>`, which changes when that text changes.
- `title: str` — human-readable. Searched and rendered.
- `task_type` — `"browser"` | `"api"` | `"code"` | `"domain"` | `"unknown"` (default). Any other value fails validation and the skill is skipped. `skill_search`/`skill_list` can filter on it.
- `tags: list[str]` — searched, and filterable with `tags=` in `skill_search`/`skill_list`. Not rendered.
- `description: str` — searched, not rendered.
- `preconditions: list[str]`, `failure_modes: list[str]` — rendered after the steps (dropped first when the token budget is tight).

### Applicability gating
- `required_tool_names: list[str]` — tool names that must all be allowed for the request.
- `required_namespaces: list[str]` — namespaces (the part of an allowed tool's name before the first `.`, or `core` when there is none) that must all be present.
- `required_tags: list[str]` — tags that must all be declared on the allowed tools.

All three are AND-combined. Empty/absent = no constraint on that axis. They're rendered with the skill.

### Extra keys
Any other key (`inputs`, `outputs`, `notes`, `version`, …) is accepted and stored, but never searched or rendered. Don't put guidance there.

### Set by the store (don't set them)
- `id` — `sk_` plus 12 hex characters of the SHA-256 of `name`.
- `origin_ref` — the `SkillPackConfig.name` that last wrote the skill.

## Validation rules

The loader:
- Walks a pack directory recursively and reads only files with the suffixes above (`SkillPackConfig.format=` restricts it to one format). A `path` that points to a single file needs a recognized suffix or `format=`.
- Skips an entry that fails validation (empty `trigger` or `steps`, unknown `task_type`, wrong types) and logs `skill_pack_invalid`.
- Skips a Markdown file whose frontmatter is missing or isn't valid YAML, without a warning.
- Raises `ValueError` for a malformed `.skill.json`, `.skill.yaml` or `.skill.jsonl` file, which fails planner construction.
- Logs `skill_pack_missing` and loads nothing when `path` doesn't exist.
- Renames a duplicate `name` within a pack to the first free `<name>_2`, `<name>_3`, ... across all the pack's files, and logs `skill_pack_duplicate_name`. Duplicates aren't dropped. A pack that reuses a name another pack already loaded takes it over in the store, logged as `skill_pack_name_conflict`. Keep names unique.

Across packs, `name` is unique in the store: with `update_existing_pack_skills=True` (the default), a later entry in `skill_packs` overwrites an earlier one with the same name. Runtime providers win over all packs.

Packs are re-synced into `<cache_dir>/skills.db` every time a planner is built: changed skills are updated (`update_existing_pack_skills`), skills removed from a pack are deleted (`prune_missing_pack_skills`), and skills of packs no longer in the config are deleted (`SkillsConfig.prune_packs_not_in_config`). All three default to True.

## Layout in a project

```
packs/
  ops/                                 # SkillPackConfig(name="ops", path="packs/ops")
    handle_incident.skill.md
    rollback_deployment.skill.md
    escalate_to_oncall.skill.md
  rich_output/
    report_layout.skill.md
    chart_selection.skill.md
  domain/
    finance_quarter_close.skill.yaml
    hr_offboarding.skill.yaml
```

One skill per file is the common convention — easier to review and version. JSONL is the exception when you must bulk-import. Pack paths are resolved relative to the process's working directory.

## Authoring tips

### Crisp triggers
Retrieval is SQLite full-text search (FTS5, with a regex fallback) over `name`, `title`, `trigger`, `description` and `tags`. It matches words, not meaning. Write the trigger in user-intent terms ("when an incident is declared", not "when sev > 0") and include the verbs the user will say.

### Concrete steps
Each step is a short imperative. Avoid hedging ("maybe consider doing X"). Avoid implementation detail unless it's universal.

### Failure modes
Cover the top 2-3 ways the skill goes wrong and what to do. The planner sees them as part of the skill text and can apply them.

### Tool references
If your skill mentions tools by name, declare `required_tool_names` to gate the skill's visibility. Otherwise the skill may surface in a request where the tool isn't allowed (its name is then redacted to "a suitable tool").

### Version your skills
Treat skills like code. PR them. Run tests against skill content if you have a verification pipeline (LLM-as-judge on "does this skill correctly say to do X?").

### Don't put secrets in skills
Skills enter the system prompt. Never include:
- API keys, tokens.
- Internal-only URLs or hostnames.
- Customer-identifying data.
- Production-specific secrets.

Pack skills are visible to every tenant. `redact_pii=True` is a defense in depth, not a license to store secrets.
