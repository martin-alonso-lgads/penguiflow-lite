# `ObservationGuardrailConfig`: Context-Overflow Safety Net

This is a reliability clamp, not a policy decision. It keeps one oversized tool result from flooding the next prompt. It is always on and independent of `GuardrailGateway`. Source: `_clamp_observation` in `penguiflow/planner/payload_builders.py`.

## Config

```python
from penguiflow.planner import ReactPlanner
from penguiflow.planner.models import ObservationGuardrailConfig

planner = ReactPlanner(
    ...,
    observation_guardrail=ObservationGuardrailConfig(
        max_observation_chars=50_000,   # >= 1000; the clamp triggers above this
        max_field_chars=10_000,         # >= 100; per-string cap when truncating
        truncation_suffix="\n... [truncated: {truncated_chars} chars]",
        preserve_structure=True,
        auto_artifact_threshold=20_000, # >= 0; 0 disables the artifact path
        preview_length=500,             # >= 0
    ),
)
```

The values shown are the defaults. There is no `enabled` flag. `observation_guardrail=None` (or omitting it) gives the default config; it does **not** disable the clamp. To effectively turn it off, raise `max_observation_chars` above any output you expect.

## What happens to a tool result

The planner serializes the tool's output model to JSON and measures its length in characters.

1. **At or under `max_observation_chars`:** unchanged. `max_field_chars` plays no part here; a 15,000-char field passes through.
2. **Over it, with `auto_artifact_threshold > 0` and the size at least that threshold:** the JSON is stored with `artifact_store.put_text(..., namespace="observation.<tool>")`. The observation becomes `{"artifact": <ArtifactRef dump>, "summary": "Large observation stored as artifact (... chars). Artifact ID: ...", "preview": <first preview_length chars>...}`. With the defaults, every clamped observation takes this path, because the 20,000 threshold is below the 50,000 cap.
3. **Otherwise, or if `put_text` raises:** the observation is truncated.
   - With `preserve_structure=True`: every string longer than `max_field_chars` is cut and gets the suffix, nested dicts are handled the same way, and lists longer than 20 items keep 20 plus `"... [N more items]"`. If the result is still over the cap, the largest **top-level** strings are cut to `preview_length`. The result is not guaranteed to fit.
   - With `preserve_structure=False`: the observation becomes `{"truncated_observation": <the first chars of the JSON>, "truncation_note": <suffix>}`.

Each clamp emits a planner event, `observation_clamped`, with `extra={"original_size", "clamped_size", "method": "artifact"|"truncate", "reduction_pct"}`, and logs `observation_clamped`.

The clamped value replaces the observation everywhere: in the trajectory, in what the LLM sees, and in the `tool_call_result` guardrail event, which runs after the clamp. Only the artifact keeps the full content.

## The artifact store matters

Without a real store the planner uses `NoOpArtifactStore`. Its `put_text` succeeds and returns a ref, but stores nothing: the ref's `source` says `"Content truncated (no ArtifactStore configured)"`. The LLM gets a ref and a preview, and the full output is gone. Pass `ReactPlanner(artifact_store=...)`, or a `state_store` that exposes one (see [[penguiflow-statestore]]), if the content must survive.

The scope of a stored observation comes from `tool_context` (`session_id`, `tenant_id`, `user_id`, `trace_id`). It is scoped only when `session_id` is set.

No built-in tool reads an artifact back into the prompt. If the LLM needs slices of a stored observation, add a tool that calls `ctx.artifacts.download(artifact_id)` and returns the part it needs. The rich-output `list_artifacts` tool only lists artifacts.

## Tuning

- Size `max_observation_chars` from the model's context window and your other prompt content. About 4 characters per token is a rough guide.
- Watch `observation_clamped` per tool. A tool that clamps often should return less, or store its own artifact and return a ref.
- Set `auto_artifact_threshold=0` only if a lossy, truncated observation is acceptable.
- Setting `max_observation_chars` too low turns ordinary results into refs and previews, which the LLM can't read further.
