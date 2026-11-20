# Durability and `SupportsPlannerState`

## Why durability matters

Without a `StateStore`, a pause record lives in memory, on the planner that served the run's `session_id`. If the worker restarts, or the resume lands on another worker, `planner.resume(...)` raises `KeyError`.

Single-process scripts and demos can skip durability. Anything else (multi-worker, autoscaling, restart-prone) must wire it.

## The capability

`penguiflow.state.SupportsPlannerState` is detected by duck typing:

```python
from typing import Any, Protocol


class SupportsPlannerState(Protocol):
    async def save_planner_state(self, token: str, payload: dict[str, Any]) -> None: ...
    async def load_planner_state(self, token: str) -> dict[str, Any] | None: ...
```

When the planner has `state_store=...` and pauses:
1. It records the pause in memory.
2. It calls `save_planner_state(resume_token, payload)`. This is best-effort: an error is logged as `state_store_save_failed`, and the in-memory record still works.
3. On `resume(resume_token)`, it looks in memory first (on the session's planner), then calls `load_planner_state(resume_token)`. A `None` result, or a load error, raises `KeyError`.

The payload is a JSON-ready dict:
- `trajectory`: the serialized trajectory, including the pause step;
- `reason` and `payload`: the pause itself;
- `constraints`: the deadline remaining and the hops used;
- `tool_context`: the run's `tool_context`, or `None` if any value isn't JSON-serializable.

Store it and return it unchanged.

## Backends

### In memory (default)
With no `state_store`, pauses live only in the planner's memory. `penguiflow.state.InMemoryStateStore` implements the capability too, and deletes on load, but it is still one process.

### Redis
```python
import json
from typing import Any


class RedisPlannerStore:
    def __init__(self, redis: Any, ttl_s: int = 3600) -> None:
        self.redis = redis              # e.g. redis.asyncio.Redis
        self.ttl_s = ttl_s

    async def save_planner_state(self, token: str, payload: dict[str, Any]) -> None:
        await self.redis.set(f"planner:pause:{token}", json.dumps(payload), ex=self.ttl_s)

    async def load_planner_state(self, token: str) -> dict[str, Any] | None:
        raw = await self.redis.getdel(f"planner:pause:{token}")   # consume on load
        return json.loads(raw) if raw is not None else None
```

Recommended TTL: at least the user's attention span for the pause, so 1-24 hours for approvals and 5-15 minutes for OAuth.

### Database
Same shape, persisted to a `(token, payload_json, created_at)` table. Add an index on `created_at` for periodic cleanup.

### Custom
Implement the capability on the same `StateStore` you use for events, memory and bindings. One store can hold many capabilities; see [[penguiflow-statestore]].

## Consume-on-load semantics

The planner removes a token from its in-memory records when it resumes, but it **never** deletes it from the store. If `load_planner_state` returns the same payload twice, for example from two workers or after a retry, the run resumes twice and its side effects repeat.

Make the store consume on load, as `GETDEL` does above, or:

```python
async def load_planner_state(self, token: str) -> dict[str, Any] | None:
    payload = await self.kv.get(f"planner:pause:{token}")
    if payload is None:
        return None
    await self.kv.delete(f"planner:pause:{token}")    # delete on read
    return payload
```

Trade-off: if the resume fails after the load succeeded, the pause is lost. For flows where duplicates are critical, use a transactional pattern: read, mark in-flight, process, mark consumed.

## TTL

Pause records should expire:
- **storage hygiene**: orphaned pauses accumulate;
- **security**: stale tokens are attack surface;
- **UX**: a stale pause shouldn't suddenly resume after the user forgot about it.

Suggested TTLs by reason:
- `approval_required`: 30 min to 24 h. Guardrail pauses carry `pause.timeout_s` (default 300 s) as a hint you can apply.
- `await_input`: 10 to 60 min.
- `external_event` (OAuth): 5 to 15 min. `OAuthManager` rejects callbacks older than 10 minutes.
- `constraints_conflict`: 30 min to 24 h.

Enforce TTLs in the store (`SET ... EX` in Redis, TTL columns in a database). The planner has no notion of TTL.

## Token security

Tokens are random `uuid4` hex strings, so they are unguessable. Still:
- **Don't log them.** They can appear in error traces, structured logs and debug output; filter them. The planner itself logs only the first 8 characters.
- **Don't show them to the wrong tenant.** If a UI lists pending pauses, scope the list by tenant.
- **Don't share storage namespaces across tenants.** Prefix store keys with `tenant:<id>:` if the store is shared.
- **Don't put them in unauthenticated URLs.** A token in a URL leaks through referrers and logs.

For high-security flows, add checks in the host app:
- verify that the user resuming is the user who paused;
- require a fresh auth cookie at resume time;
- rate-limit resume attempts per token.

## Cross-worker recipes

### Stateless workers and a shared store
```python
from penguiflow.planner import ReactPlanner

planner = ReactPlanner(
    llm="gpt-4o-mini",
    catalog=catalog,
    state_store=RedisPlannerStore(redis_client),
)
```
Any worker can resume any pause, because the in-memory miss falls back to the store. This is the standard SaaS pattern. Pass the run's `session_id` and full `tool_context` on resume.

### Sticky sessions (not recommended)
Routing resumes back to the worker that paused works without a store, but it complicates deployments and doesn't survive the death of a worker.

### Distributed lock for resume
For resumes where duplicates are critical, take a per-token lock at the start of the resume:

```python
from typing import Any

from penguiflow.planner import PlannerFinish, PlannerPause, ReactPlanner


async def resume_with_lock(planner: ReactPlanner, token: str, **kwargs: Any) -> PlannerFinish | PlannerPause:
    async with redis_lock(f"resume:{token}", ttl=60):    # your lock helper
        return await planner.resume(token, **kwargs)
```

Two simultaneous resumes of the same token now run one after the other. Combined with consume-on-load, the second one gets `KeyError`.

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `KeyError` on resume after a restart | No `state_store`, or it lacks the two methods | Configure the store; implement both methods |
| `KeyError` on another worker | Same as above: in-memory records are per process | A shared store |
| Pause persisted, but resume `KeyError` | Token expired, or already consumed | Lengthen the TTL, or show an "expired" state in the UI |
| Same pause resumes twice | No consume-on-load | Delete on read in `load_planner_state` |
| Resumed run has no `tool_context` | The stored one had a non-JSON value and was dropped | Re-pass `tool_context` on resume |
| Token in an error log | Log filter missing | Redact `resume_token` in log formatters |
| Cross-tenant resume succeeds | Store keys not tenant-scoped | Prefix keys with `tenant:<id>:` |
| Save fails silently | Store exception (`state_store_save_failed` in the logs) | Check the store; the pause works in memory but won't survive a restart |

## Observability

Track:
- pause count by `reason` and tool (guardrail pauses carry `payload["guardrail"]["rule_id"]`);
- time to resume (p50/p95/p99) by reason;
- resume failure rate (`KeyError` versus other errors);
- store save and load failure rates;
- TTL expiry rate: pauses that expired without a resume.

A spike in expiries suggests the UI flow is too slow, or the TTL too short.
