# Strategies and Memory Health

## `truncation` strategy

Deterministic. No LLM dependency.

Behavior:
- Keeps the most recent `full_zone_turns` turns.
- Then drops the oldest turns while the payload is over `total_max_tokens`.
- Health is always `healthy`.

Use when:
- Sessions are short.
- You can't afford LLM cost / latency for memory maintenance.
- You don't need long-context awareness beyond the last few turns.

What the LLM sees:
```json
"conversation_memory": {
  "recent_turns": [{"user": "...", "assistant": "..."}]
}
```

No `summary`, no `pending_turns`.

## `rolling_summary` strategy

LLM-maintained summary + recent turns.

Behavior:
- When there are more than `full_zone_turns` turns, the oldest move to `pending_turns`, and a background summarization starts right away.
- The summarizer gets the previous summary plus the pending turns and returns a new summary, which replaces the old one; the pending turns are then cleared.
- The summary is clipped to `summary_max_tokens` (keeping its end).
- If the summarizer keeps failing, memory becomes `degraded` and shows recent turns only.

What the LLM sees (healthy, summary in flight or done):
```json
"conversation_memory": {
  "recent_turns": [{"user": "...", "assistant": "..."}],
  "summary": "<session_summary>\nUser's name is Alice. Prefers terse responses.\n</session_summary>",
  "pending_turns": [{"user": "...", "assistant": "..."}]
}
```

`summary` and `pending_turns` appear only when non-empty. The planner's summarizer wraps its output in `<session_summary>` tags.

What the LLM sees (degraded):
```json
"conversation_memory": {
  "recent_turns": [{"user": "...", "assistant": "..."}]
}
```

The summary is hidden while degraded; older turns wait in a backlog for recovery.

## `MemoryHealth` lifecycle

States (the `MemoryHealth` enum):
- `HEALTHY` (`"healthy"`) — The last summarization succeeded (or none has failed yet).
- `RETRY` (`"retry"`) — A summarization failed; it is retried in the background after `retry_backoff_base_s * 2 ** (failures - 1)` seconds.
- `DEGRADED` (`"degraded"`) — More than `retry_attempts` consecutive failures. Pending turns move to a backlog, and the prompt shows recent turns only.
- `RECOVERING` (`"recovering"`) — A recovery attempt on the backlog is in flight.

Transitions fire `on_health_changed(old, new)`. Wire this to your metrics sink to alert on summarizer reliability.

### Recovery loop

When degraded:
1. Each new turn keeps only the last `full_zone_turns` turns; older ones go to the backlog, which holds at most `recovery_backlog_limit` turns (the oldest are dropped).
2. A recovery attempt starts on `add_turn` if at least `degraded_retry_interval_s` seconds passed since the last one. Health becomes `recovering` while it runs.
3. Success replaces the summary, clears the backlog and returns to `healthy`.
4. Failure returns to `degraded`, and the next attempt waits for the interval again.

## Trajectory digests

When `include_trajectory_digest=True` (the default), the planner builds a `TrajectoryDigest` for each finished run that made at least one successful tool call:

```python
from penguiflow.planner.memory import TrajectoryDigest

TrajectoryDigest(
    tools_invoked=["search_web", "search_docs"],
    observations_summary=(
        '- search_web: {"hits": 5}\n- search_docs: {"hits": 2}'
    ),
    reasoning_summary="Combined both search backends.",
    artifacts_refs=[],
)
```

- `tools_invoked`: the successful tool calls, in order.
- `observations_summary`: one `- <tool>: <observation JSON>` line per call, each cut to 400 characters.
- `reasoning_summary`: the final step's `thought`.
- `artifacts_refs`: left empty by the planner.

Trade-off:
- **On** (default) — the model sees what tools were used; better continuity for multi-turn debugging.
- **Off** — smaller prompts; cheaper.

Keep it on for high-value sessions (support escalations, debugging). Turn it off for high-throughput chat.

## Choosing a summarizer model

Production criteria:
- **Cheap** — a summarization runs each time turns leave the full zone, so roughly once per turn once the zone is full. Cost compounds.
- **Fast** — it runs in the background, but a slow summarizer leaves more `pending_turns` in the prompt.
- **Reliable** — failures cause health degradation; uptime matters.
- **Good at compression** — small models that summarize crisply.

Reasonable choices: `gpt-4o-mini`, `gpt-4.1-mini`, or the small fast model of your provider (it is built with the same LLM backend as the planner). With `summarizer_model=None`, the planner's own LLM summarizes.

Avoid the same model as your main planner unless prompt cost is negligible.

## Token estimator

The default heuristic is `len(text) // 4 + 1` (rough English approximation). For production:

```python
import tiktoken

from penguiflow.planner.memory import ShortTermMemoryConfig

enc = tiktoken.encoding_for_model("gpt-4o")
stm = ShortTermMemoryConfig(
    strategy="truncation",
    token_estimator=lambda text: len(enc.encode(text)),
)
```

Real tokenizers give accurate budget enforcement. Worth the cost if you're optimizing prompts tightly. `tiktoken` is not a PenguiFlow dependency; install it yourself.

## Operational defaults by workload

### Chat agent, multi-tenant SaaS
- `strategy="rolling_summary"`
- `full_zone_turns=5`
- `summary_max_tokens=800`
- `total_max_tokens=8000`
- `overflow_policy="truncate_oldest"`
- `include_trajectory_digest=False`
- `summarizer_model="gpt-4o-mini"`
- `require_explicit_key=True`, plus an explicit `MemoryKey` per call

### Single-user assistant
- `strategy="truncation"`
- `full_zone_turns=10`
- `total_max_tokens=16000`
- `require_explicit_key=False`

### Debugging / support tool
- `strategy="rolling_summary"`
- `full_zone_turns=8`
- `include_trajectory_digest=True` (the default; keeps tool history visible)
- `summarizer_model="gpt-4o-mini"`

### Hard token cap
- `strategy="truncation"`
- `total_max_tokens=4000`
- `overflow_policy="truncate_oldest"` (under the planner, `"error"` only logs `memory_add_turn_failed`; it doesn't fail the run)

## Anti-patterns

- **Long-term memory** — STM is bounded; don't use it as a knowledge base. Build retrieval separately.
- **Storing secrets** — anything in STM enters the prompt. Treat it as LLM-visible.
- **Anonymous keys in production** — `require_explicit_key=False` + no `tool_context` keys = one anonymous session shared by every keyless call on the planner. Catastrophic in multi-tenant.
- **Session id without tenant** — a derived key from `session_id` alone lands under tenant `"default"`, so tenants that reuse session ids share memory.
- **Relying on `error` overflow under the planner** — it logs instead of raising, so it enforces nothing. Use a truncation policy.
- **Heavy hooks** — hooks are fire-and-forget but they still consume the event loop. Don't do DB writes there; use a `StateStore`.
