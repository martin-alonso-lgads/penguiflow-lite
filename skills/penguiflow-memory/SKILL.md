---
name: penguiflow-memory
description: Add short-term conversation memory to a PenguiFlow ReactPlanner — configure `ShortTermMemoryConfig` with strategy (`none`/`truncation`/`rolling_summary`), `MemoryBudget` token caps + overflow policy, `MemoryIsolation` key paths and `MemoryKey(tenant_id, user_id, session_id)` scoping, optionally persist via `StateStore.save_memory_state`/`load_memory_state`, wire `on_turn_added`/`on_summary_updated`/`on_health_changed` hooks, and tune `summarizer_model`/`include_trajectory_digest`. Use when a user says "add memory", "session memory", "conversation memory", "multi-tenant memory", "rolling summary", or names `ShortTermMemoryConfig`, `MemoryKey`, `MemoryIsolation`.
---

# PenguiFlow Short-Term Memory (Planner STM)

## When to use
- Sessions span multiple `planner.run(...)` calls.
- You want the planner to remember recent turns and/or a rolling summary.
- Multi-tenant: you need every call scoped to a tenant, user and session.
- Pause/resume cycles where context must survive.

## When NOT to use
- Long-term memory or knowledge bases (vector DBs, retrieval) → not in scope; build it separately (an `llm_context_hooks` hook can inject it as `external_memory`, see `examples/react_memory_context/main.py`).
- Per-trace observability/audit (use [[penguiflow-statestore]] for events/trajectories).
- Cross-session global state (use your app's DB).
- HITL pause state durability → [[penguiflow-hitl-pause-resume]] (memory persistence and pause persistence are distinct StateStore capabilities).

## Hard boundaries
STM is a **bounded prompt-injection layer**. It merges recent turns and an optional rolling summary into `llm_context["conversation_memory"]`, and the prompt builder renders that as a separate read-only system message (`<read_only_conversation_memory>`). It is not a vector store, not a knowledge base, not a long-term cache. Anything in STM becomes LLM-visible — never store secrets.

## Workflow

### 1) Decide a strategy
| Strategy | Behavior |
|---|---|
| `none` | No memory injected (default; memory off). |
| `truncation` | Keep the last `full_zone_turns` turns; drop older. |
| `rolling_summary` | Keep recent turns + an LLM-maintained rolling summary of older ones; shows only recent turns while the summarizer is degraded. |

Start with `truncation` for new agents. Switch to `rolling_summary` only after measuring prompt token usage and confirming you have a reliable summarizer model.

### 2) Build `ShortTermMemoryConfig`
```python
from penguiflow.planner.memory import (
    MemoryBudget,
    MemoryIsolation,
    ShortTermMemoryConfig,
)

stm = ShortTermMemoryConfig(
    strategy="rolling_summary",  # default is "none": must opt in
    budget=MemoryBudget(
        full_zone_turns=5,
        summary_max_tokens=1000,
        total_max_tokens=10000,
        overflow_policy="truncate_oldest",  # or truncate_summary | error
    ),
    isolation=MemoryIsolation(
        tenant_key="tenant_id",
        user_key="user_id",
        session_key="session_id",
        require_explicit_key=True,  # library default
    ),
    summarizer_model="gpt-4o-mini",  # default None: the planner's own LLM
    include_trajectory_digest=True,  # library default
)
```

Full field reference in `references/config-and-budgets.md`.

### 3) Attach to `ReactPlanner`
```python
from penguiflow.planner import ReactPlanner

planner = ReactPlanner(
    llm="gpt-4o-mini",
    catalog=...,  # your tools
    short_term_memory=stm,
)
```

`short_term_memory=` also accepts your own `ShortTermMemory` instance; the planner then uses that one object for every key (see `examples/memory_custom/flow.py`).

### 4) Scope every call with a `MemoryKey`
```python
from penguiflow.planner.memory import MemoryKey

key = MemoryKey(tenant_id="t1", user_id="u1", session_id="s1")

await planner.run(
    "Remember my favorite color is teal.",
    memory_key=key,
    tool_context={"tenant_id": "t1", "user_id": "u1", "session_id": "s1"},
)
```

Two ways to supply the key:
- **Explicit** — pass `memory_key=MemoryKey(...)` on every `run()`/`resume()`. It always wins. **Recommended for services.**
- **Derived** — the planner reads the `MemoryIsolation` dotted paths from `tool_context`. Only `session_id` is required: a missing tenant becomes `"default"` and a missing user `"anonymous"`, so two tenants that reuse a session id share memory. Always put `tenant_id` and `user_id` in `tool_context` if you rely on this.

With `require_explicit_key=True` (the default), a call with no `memory_key` and no `session_id` in `tool_context` gets no memory. With `False`, every such call on the planner instance shares one anonymous key (`default:anonymous:<random>`).

### 5) (Optional) Persist memory across processes
Without a store, memory lives in the planner instance, one memory per composite key. For multiple workers or restarts, pass `state_store=` a store implementing `SupportsMemoryState`:
- `save_memory_state(key: str, state: dict) -> None`
- `load_memory_state(key: str) -> dict | None`

Every `run()` and `resume()` hydrates from the store (`load_memory_state`). After a run that finishes (`PlannerFinish`), the turn is added and the state saved. A paused run records no turn. With `rolling_summary`, the state is saved again once the background summary is done, and the next run on that planner waits for that save before it loads (see `references/persistence-and-hooks.md`). See [[penguiflow-statestore]] for backends. The same methods also back `ctx.kv`, under keys like `kv:v1:<tenant_id>:<user_id>:<session_id>:session:<namespace>:<key>`, so implementations must treat keys as opaque strings, not only `tenant:user:session`.

### 6) (Optional) Wire hooks for metrics
`ShortTermMemoryConfig` accepts `on_turn_added(turn)`, `on_summary_updated(old, new)`, `on_health_changed(old, new)` — all async, **fire-and-forget**. Exceptions are swallowed. Use for lightweight metrics/logs only; durability goes through `StateStore`. Full signatures in `references/persistence-and-hooks.md`.

### 7) Observe what the LLM sees
When STM is active, the memory payload has `recent_turns`, plus `summary` and `pending_turns` for `rolling_summary`. It reaches the model as the `<read_only_conversation_memory>` system message; check for it in the messages your LLM client receives. If it's missing, key resolution failed.

## Troubleshooting (fast checks)
- **No memory in the prompt** — key resolution failed; pass `memory_key=MemoryKey(...)` explicitly or put `session_id` (and `tenant_id`/`user_id`) in `tool_context`.
- **Memory bloats prompts** — lower `full_zone_turns` (3-5), set `include_trajectory_digest=False`, move large tool outputs to artifacts.
- **Rolling summary never appears** — confirm `strategy="rolling_summary"`. Summaries start only once there are more than `full_zone_turns` turns, and they run in the background. Check that `summarizer_model` (or the planner's LLM, when it's `None`) is reachable, and watch `on_health_changed` for `degraded`.
- **`memory_add_turn_failed` warnings** — with `overflow_policy="error"`, the planner catches `MemoryBudgetExceeded` and logs it; the run still returns, and that turn is neither kept in memory nor persisted. Use `truncate_oldest` for services.
- **Cross-session leak** — sessions are **not** tenant boundaries, and a derived key with only `session_id` uses tenant `"default"`. Pass an explicit `MemoryKey` or always set `tenant_id` and `user_id`.
- **`memory_context_not_json_serialisable` warning** — a custom memory returned a non-JSON patch, so it was skipped. (A non-JSON `llm_context` raises `TypeError` at `run()`.)
- **Hooks not firing** — exceptions are swallowed; add logging inside hook bodies.
- **Memory doesn't survive restart** — attach a `StateStore` with `save_memory_state`/`load_memory_state`.

## Worked examples
- `examples/memory_basic/flow.py` — the planner remembers earlier turns.
- `examples/memory_truncation/flow.py` — `truncation` with a token budget.
- `examples/memory_callbacks/flow.py` — `rolling_summary` with the three hooks.
- `examples/memory_custom/flow.py` — a custom `ShortTermMemory` instance.
- `examples/memory_persistence/flow.py` — hydrate and persist through `state_store=`.
- `examples/memory_redis/flow.py` — round-trip `DefaultShortTermMemory.to_dict()`/`from_dict()` through a Redis-like client yourself, without `state_store=`.
- `examples/react_memory_context/main.py` — not STM: an `llm_context_hooks` hook that injects `external_memory`.

## References (load only as needed)
- `references/config-and-budgets.md` — `ShortTermMemoryConfig`, `MemoryBudget`, `MemoryIsolation`, `MemoryKey`, every field.
- `references/strategies-and-health.md` — `truncation` vs `rolling_summary`, `MemoryHealth` lifecycle, summarizer reliability, trajectory digests.
- `references/persistence-and-hooks.md` — `StateStore` capability, key composition rules, hook semantics, custom memory backends.
