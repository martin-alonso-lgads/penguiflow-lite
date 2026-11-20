# `ShortTermMemoryConfig` and Friends

All of these live in `penguiflow.planner.memory` and are plain dataclasses that validate in `__post_init__` (a bad value raises `ValueError`).

## `ShortTermMemoryConfig`

Top-level configuration passed to `ReactPlanner(short_term_memory=...)`. Every field, with its default:

```python
from penguiflow.planner.memory import (
    MemoryBudget,
    MemoryIsolation,
    ShortTermMemoryConfig,
)

ShortTermMemoryConfig(
    strategy="none",  # or "truncation" | "rolling_summary"
    budget=MemoryBudget(),
    isolation=MemoryIsolation(),
    summarizer_model=None,
    include_trajectory_digest=True,
    recovery_backlog_limit=20,
    retry_attempts=3,
    retry_backoff_base_s=2.0,
    degraded_retry_interval_s=30.0,
    token_estimator=None,  # (str) -> int
    on_turn_added=None,  # async (ConversationTurn) -> None
    on_summary_updated=None,  # async (old: str, new: str) -> None
    on_health_changed=None,  # async (old: MemoryHealth, new: MemoryHealth)
)
```

### `strategy`

| Value | Behavior |
|---|---|
| `none` | No memory injected (default; effectively off). |
| `truncation` | Keep the last `full_zone_turns` turns; drop older. Deterministic, no LLM dependency. |
| `rolling_summary` | Keep recent turns + an LLM-generated rolling summary of older ones. While degraded, only recent turns are shown. |

Pick `truncation` first. Switch to `rolling_summary` only after measuring prompt token usage and confirming a reliable summarizer model.

### `summarizer_model`
The LLM used to refresh the rolling summary. `None` (the default) reuses the planner's own LLM client. Use a cheap, fast model (for example `gpt-4o-mini`). Summarizer failures degrade health; while degraded, memory shows recent turns only.

### `include_trajectory_digest`
When `True` (the default), each stored turn carries a compressed digest of the run's tool calls (`TrajectoryDigest`). Increases prompt size but lets the model reason about prior tool decisions. Set to `False` to trim prompts.

### `recovery_backlog_limit`, `retry_attempts`, `retry_backoff_base_s`, `degraded_retry_interval_s`
Summarizer retry and recovery knobs (defaults 20, 3, 2.0, 30.0); see `strategies-and-health.md`. Tune only if you have visibility into summarizer error rates.

### `token_estimator`
Function `(str) -> int` used to estimate token counts for budget enforcement. The default is `len(text) // 4 + 1`. Provide a real tokenizer for accurate budgets.

### Hooks
- `on_turn_added(turn)` — called after a turn is appended (not for a turn the `error` policy rejects).
- `on_summary_updated(old, new)` — called when a new rolling summary is stored.
- `on_health_changed(old, new)` — called when `MemoryHealth` changes.

All hooks run **fire-and-forget** in background tasks. Exceptions are swallowed. Keep them lightweight (metrics, structured logging) — do not block on I/O. For durability, use a `StateStore`.

## `MemoryBudget`

```python
from penguiflow.planner.memory import MemoryBudget

MemoryBudget(
    full_zone_turns=5,
    summary_max_tokens=1000,
    total_max_tokens=10000,
    overflow_policy="truncate_oldest",  # or "truncate_summary" | "error"
)
```

| Field | Purpose |
|---|---|
| `full_zone_turns` | Number of most-recent turns kept as full messages. 3-8 typical. |
| `summary_max_tokens` | Max tokens for the rolling summary. A longer summary is clipped, keeping its end. Must be <= `total_max_tokens`. |
| `total_max_tokens` | Cap on the whole memory payload, checked on every `add_turn`. `0` disables the check. |
| `overflow_policy` | What to do when the payload exceeds `total_max_tokens`. |

### Overflow policies

- `truncate_oldest` (default) — Drop pending turns first, then the oldest recent turns, and clear the summary as a last resort.
- `truncate_summary` — Clip the rolling summary to `summary_max_tokens` first. When the summary already fits (or is left out while degraded) and the payload is still over `total_max_tokens`, it continues like `truncate_oldest`.
- `error` — `add_turn` raises `MemoryBudgetExceeded` and leaves memory as it was (the turn is not kept). With `rolling_summary`, `pending_turns` don't count toward this check, since they are being summarized, so the prompt can briefly exceed `total_max_tokens` until the summary lands. Under `ReactPlanner` the exception is caught and logged as a `memory_add_turn_failed` warning: the run still returns, and that turn is not persisted. Use it only when you drive `DefaultShortTermMemory` yourself (tests, hard-bound tooling).

Production recommendation: `truncate_oldest`.

## `MemoryIsolation`

```python
from penguiflow.planner.memory import MemoryIsolation

MemoryIsolation(
    tenant_key="tenant_id",
    user_key="user_id",
    session_key="session_id",
    require_explicit_key=True,
)
```

`tenant_key`/`user_key`/`session_key` are dotted paths looked up in `tool_context` (for example `"auth.tenant_id"`). A derived key needs only the session value: a missing tenant becomes `"default"` and a missing user `"anonymous"`. The composite key is `f"{tenant_id}:{user_id}:{session_id}"` (`MemoryKey.composite()`).

| `require_explicit_key` | Behavior when there is no `memory_key` and no session value in `tool_context` |
|---|---|
| `True` (default) | Memory is off for that call. |
| `False` | Every such call on the planner instance shares one anonymous key, `default:anonymous:<random>`. **Unsafe for multi-tenant** — only for single-tenant prototypes. |

The default fails closed only on a missing session id, not on a missing tenant or user. In multi-tenant services, pass an explicit `MemoryKey`, or make sure `tool_context` always carries `tenant_id` and `user_id`.

## `MemoryKey`

```python
from penguiflow.planner.memory import MemoryKey

key = MemoryKey(tenant_id="t1", user_id="u1", session_id="s1")
assert key.composite() == "t1:u1:s1"
```

The explicit form. Pass via `planner.run(..., memory_key=key)` or `planner.resume(..., memory_key=key)`. It always wins over derived keys.

When to use explicit vs derived:
- **Explicit** — services, multi-tenant, anywhere a bug in `tool_context` could leak across tenants.
- **Derived** — single-tenant prototypes, scripts, demos.

## `ConversationTurn`

The atomic unit STM stores. The planner builds one per finished run:

```python
import time

from penguiflow.planner.memory import ConversationTurn

ConversationTurn(
    user_message="What's the weather?",
    assistant_response="Sunny, 24°C.",
    trajectory_digest=None,  # TrajectoryDigest | None
    artifacts_shown={},
    artifacts_hidden_refs=[],
    ts=time.time(),
)
```

`assistant_response` is the finish payload's `raw_answer`, or the payload as JSON. `artifacts_shown` and `artifacts_hidden_refs` are stored and persisted but never rendered into the prompt, and the planner leaves them empty; they're for custom memory implementations.

## `TrajectoryDigest`

```python
from penguiflow.planner.memory import TrajectoryDigest

TrajectoryDigest(
    tools_invoked=["search_web"],
    observations_summary='- search_web: {"results": ["..."]}',
    reasoning_summary=None,
    artifacts_refs=[],
)
```

A compressed record of a turn's tool calls, built only when `include_trajectory_digest=True` (the default) and at least one tool call succeeded. Trade-off: more grounding for the model vs more tokens.

## What lands in `llm_context`

When memory is active, the planner merges this into `llm_context` (here for `rolling_summary`; `truncation` has only `recent_turns`):

```json
{
  "conversation_memory": {
    "recent_turns": [
      {
        "user": "user message",
        "assistant": "assistant response",
        "trajectory_digest": {
          "tools_invoked": ["tool_a", "tool_b"],
          "observations_summary": "- tool_a: {...}",
          "reasoning_summary": "...",
          "artifacts_refs": []
        }
      }
    ],
    "summary": "rolling summary text (when one exists)",
    "pending_turns": [{"user": "...", "assistant": "..."}]
  }
}
```

`pending_turns` are turns that left the full zone but aren't summarized yet. The prompt builder removes `conversation_memory` from the `llm_context` message and renders it as a separate `<read_only_conversation_memory>` system message.

## Defaults summary

| Setting | Library default | Multi-tenant recommendation | Prototype |
|---|---|---|---|
| `strategy` | `"none"` (off) | `"truncation"` to start, `"rolling_summary"` later | `"truncation"` |
| `full_zone_turns` | `5` | 3-5 | 5-8 |
| `summary_max_tokens` | `1000` | 800-1000 | unchanged |
| `total_max_tokens` | `10000` | 8000 | 16000 |
| `overflow_policy` | `"truncate_oldest"` | `"truncate_oldest"` | `"truncate_oldest"` |
| `require_explicit_key` | `True` | `True` | `False` if single-tenant |
| `include_trajectory_digest` | `True` | `False` for tight token budgets | unchanged |
| `summarizer_model` | `None` (planner's LLM) | `"gpt-4o-mini"` or similar | unchanged |
| `memory_key` source | n/a | Explicit `MemoryKey(...)` | Derived from `tool_context` |
