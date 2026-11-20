# Persistence and Hooks

## Why persist memory

Without persistence, memory lives in the planner instance (one memory object per composite key). Restarts and worker swaps drop it. Acceptable for single-process demos; unacceptable for production services.

## `SupportsMemoryState` capability

`DefaultShortTermMemory` (the built-in implementation) persists via two methods on the store passed as `ReactPlanner(state_store=...)`. The protocol is `penguiflow.state.SupportsMemoryState`:

```python
from typing import Any, Protocol


class SupportsMemoryState(Protocol):
    async def save_memory_state(
        self, key: str, state: dict[str, Any]
    ) -> None: ...

    async def load_memory_state(self, key: str) -> dict[str, Any] | None: ...
```

The planner detects the methods by duck typing. When it has a `state_store` and `short_term_memory` is configured:
- Every `run()` and `resume()` calls `load_memory_state(key)` and, if it returns a dict, replaces the in-process memory with it.
- After a run that finishes (`PlannerFinish`), the turn is added and `save_memory_state(key, state)` is called. A paused run records no turn.
- Load and save errors are logged (`memory_hydrate_failed`, `memory_persist_failed`) and don't fail the run.

The `key` argument is the `MemoryKey` composite (`"{tenant_id}:{user_id}:{session_id}"`), but the contract allows arbitrary composite keys — see "Reserved keyspace" below. `state` is the `DefaultShortTermMemory.to_dict()` snapshot (`version`, `health`, `summary`, `turns`, `pending`, `backlog`, `config_snapshot`).

**With `rolling_summary`:** the state is saved right after the turn is added, and saved again in the background once the summarization is done. The next run on the same planner waits for that second save before it loads, so the summary reaches both the store and the prompt. Across processes the store is last-write-wins: a worker that loads before another worker's summary save completes sees the turns as `pending_turns` and summarizes them itself.

## Reserved keyspace: `kv:v1:`

The same `save_memory_state` / `load_memory_state` methods back the durable tool KV facade `ctx.kv`. Its keys look like `kv:v1:<tenant_id>:<user_id>:<session_id>:session:<namespace>:<key>` (or `...:task:<task_id>:<namespace>:<key>` for task scope).

Implications for `StateStore` authors:
- Don't assume `key` is always `tenant:user:session` — it can be any string.
- Don't apply tenant/user/session parsing logic inside the store.
- Treat the key as opaque; store and return state as-is.

For the durable backend choice, see [[penguiflow-statestore]].

## Backends

### In-memory (no persistence)
Default when no `state_store` is passed. Memory lives in the planner instance. Use for tests and prototypes.

### Redis
```python
import json
from typing import Any

from redis.asyncio import Redis


class RedisMemoryStore:
    def __init__(self, redis: Redis, ttl_s: int = 72 * 3600) -> None:
        self.redis = redis
        self.ttl_s = ttl_s

    async def save_memory_state(
        self, key: str, state: dict[str, Any]
    ) -> None:
        payload = json.dumps(state)
        await self.redis.set(f"memory:{key}", payload, ex=self.ttl_s)

    async def load_memory_state(self, key: str) -> dict[str, Any] | None:
        raw = await self.redis.get(f"memory:{key}")
        return json.loads(raw) if raw else None
```

`examples/memory_redis/flow.py` takes a different route: it builds a `DefaultShortTermMemory` itself, passes it as `short_term_memory=`, and round-trips `to_dict()`/`from_dict()` through a Redis-like client around each run, without `state_store=`. `examples/memory_persistence/flow.py` shows the `state_store=` route.

### Database
Same shape, different driver. Either:
- One row per `(key, state_json)` (simple).
- Normalized schema with separate tables for turns and summaries (richer queries, harder migrations).

Pick the simple JSON-column approach unless you need to query over memory contents.

### Custom
Implement the two methods on any object:

```python
from typing import Any


class MyStore:
    def __init__(self, svc: Any, namespace: str = "stm") -> None:
        self.svc = svc
        self.namespace = namespace

    async def save_memory_state(
        self, key: str, state: dict[str, Any]
    ) -> None:
        await self.svc.put(f"{self.namespace}:{key}", state)

    async def load_memory_state(self, key: str) -> dict[str, Any] | None:
        return await self.svc.get(f"{self.namespace}:{key}")
```

The store can implement other `StateStore` capabilities too (events, bindings, pause state). Just add the methods.

A custom `ShortTermMemory` instance passed as `short_term_memory=` is persisted only if it has its own `persist(store, key)` / `hydrate(store, key)` methods (`ShortTermMemoryPersistence`).

## Hook semantics

All three hooks are **fire-and-forget** background tasks:

```python
from collections.abc import Awaitable, Callable

from penguiflow.planner.memory import ConversationTurn, MemoryHealth

OnTurnAdded = Callable[[ConversationTurn], Awaitable[None]]
OnSummaryUpdated = Callable[[str, str], Awaitable[None]]
OnHealthChanged = Callable[[MemoryHealth, MemoryHealth], Awaitable[None]]
```

### Execution model
- The memory schedules each hook via `asyncio.create_task(...)` and keeps a reference until it finishes.
- Hooks run concurrently.
- Exceptions are **swallowed** intentionally (memory can't fail the planner run).
- The planner does **not** await hook completion before continuing.
- `on_health_changed` fires only when the state actually changes.

### What to put in hooks
- Lightweight metrics (`increment counter`, `record histogram`).
- Structured logging.
- Cheap audit events.

### What NOT to put in hooks
- Blocking I/O (synchronous DB writes, network calls without timeouts).
- Memory-critical state writes (use a `StateStore` instead).
- Long-running operations (>10ms is risky on a busy event loop).

### Example: metrics and logs
```python
import logging
from collections import Counter

from penguiflow.planner.memory import (
    ConversationTurn,
    MemoryHealth,
    ShortTermMemoryConfig,
)

logger = logging.getLogger("my_app.stm")
transitions: Counter[str] = Counter()


async def on_health_changed(old: MemoryHealth, new: MemoryHealth) -> None:
    transitions[f"{old.value}->{new.value}"] += 1


async def on_turn_added(turn: ConversationTurn) -> None:
    logger.info(
        "stm.turn_added",
        extra={
            "user_chars": len(turn.user_message),
            "assistant_chars": len(turn.assistant_response),
        },
    )


async def on_summary_updated(old: str, new: str) -> None:
    logger.info(
        "stm.summary_updated",
        extra={"old_chars": len(old), "new_chars": len(new)},
    )


stm = ShortTermMemoryConfig(
    strategy="rolling_summary",
    on_turn_added=on_turn_added,
    on_summary_updated=on_summary_updated,
    on_health_changed=on_health_changed,
)
```

### Example: cheap audit (don't await heavy)
```python
import asyncio

from penguiflow.planner.memory import ConversationTurn

audit_queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=1000)


async def on_turn_added(turn: ConversationTurn) -> None:
    # cheap event push, non-blocking
    audit_queue.put_nowait({"ts": turn.ts, "kind": "turn_added"})
```

If the audit queue is bounded and full, `put_nowait` raises `QueueFull`. Since hook exceptions are swallowed, this is safe — but you lose the audit. Decide whether to upsize the queue or push to a different sink.

## Verifying persistence works

1. Start the planner with `state_store=YourStore()` and a `MemoryKey`.
2. Run a `planner.run(...)` that asks the LLM to remember a fact ("My favorite color is teal.").
3. Restart the worker.
4. Run a new `planner.run(...)` with the **same** `MemoryKey` and ask the LLM to recall.

If the model recalls correctly, persistence works. If it doesn't:
- Check the store: `await store.load_memory_state("<your-key>")` should return a dict.
- Check the key: `MemoryKey("t1", "u1", "s1").composite()` is `"t1:u1:s1"`.
- Check the strategy: `none` won't store anything.
- Check the logs for `memory_persist_failed` / `memory_hydrate_failed`.

## Multi-worker considerations

- **Shared store required.** Each worker must read/write from the same backend (Redis, DB).
- **Concurrency** — two workers can race on the same key, and the last write wins, so one of two concurrent turns can be lost. Serialize turns per session if that matters.
- **Hot keys** — high-traffic sessions hit the same key repeatedly. Use a fast backend (Redis, in-memory cache layer in front of DB).
- **TTL** — set a TTL on memory entries (e.g., 24-72h). Stale sessions accumulate; clean them up.

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| Memory empty after restart | Store missing or `state_store` not passed to planner | Confirm `state_store` is configured; check `load_memory_state` returns non-null |
| Cross-tenant data in memory | Key collision (same composite for different tenants, e.g. a derived key with only `session_id`) | Always include `tenant_id` in the key or `tool_context` |
| Hook errors silent | Exceptions are swallowed | Add logging inside hook body; assertions in tests |
| Slow `run()` start | `load_memory_state` is slow | Cache reads; use a faster backend; reduce stored state size |
| Memory bloat over weeks | No TTL | Add TTL on the storage backend |
