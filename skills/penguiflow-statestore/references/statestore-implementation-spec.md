# StateStore Implementation Specification

> **Applies to:** penguiflow-lite 7.0.0
> **Audience:** Downstream teams implementing custom StateStore backends
> **Last Updated:** November 2026

This document provides a complete specification for implementing a StateStore backend compatible with PenguiFlow. It covers the protocol definition, data types, method contracts, integration points, distributed deployment patterns, and production best practices.

---

## Table of Contents

1. [Overview](#overview)
2. [Protocol Definition](#protocol-definition)
3. [Data Types](#data-types)
4. [Required Methods](#required-methods)
5. [Optional Duck-Typed Methods](#optional-duck-typed-methods)
6. [Context Versioning & Divergence Detection](#context-versioning--divergence-detection)
7. [Steering Payload Sanitization](#steering-payload-sanitization)
8. [Merge Strategies](#merge-strategies)
9. [Memory Health States & Recovery](#memory-health-states--recovery)
10. [Artifact Store](#artifact-store)
11. [Integration Points](#integration-points)
12. [Distributed Deployment](#distributed-deployment)
13. [Method Contracts & Semantics](#method-contracts--semantics)
14. [Reference Implementations](#reference-implementations)
15. [Compatibility Adapters](#compatibility-adapters)
16. [Telemetry Integration](#telemetry-integration)
17. [Database Schema Recommendations](#database-schema-recommendations)
18. [Testing Your Implementation](#testing-your-implementation)
19. [Error Handling](#error-handling)
20. [Performance Considerations](#performance-considerations)
21. [Implementation Checklist](#implementation-checklist)

---

## Overview

### What is StateStore?

`StateStore` is a **Protocol** (duck-typed interface) that enables PenguiFlow to persist:

- **Audit events** (`StoredEvent`): session task/update/steering snapshots written by `StateStoreSessionAdapter`, plus any events your application writes
- **Remote bindings** (A2A task and conversation associations) for A2A servers and `A2AAgentToolset` continuity
- **Planner pause state** (OAuth/HITL) so a paused run can resume on another worker
- **Memory state** (short-term conversation memory) for session persistence
- **Task lifecycle** (foreground/background tasks) for session management
- **Steering events** (user interventions) for bidirectional control
- **Trajectories and planner events** for replay and debugging
- **Artifacts** (binary/large-text outputs) for rich tool results

### Why Implement a StateStore?

| Feature | Without StateStore | With StateStore |
|---------|-------------------|-----------------|
| Session task/update/steering history | In-memory (`InMemorySessionStateStore`), lost on restart | Persisted, queryable |
| Trace history (`penguiflow-admin history`) | Not available | Returns the `StoredEvent`s written for a trace |
| Distributed resume (OAuth/HITL) | Same planner instance only | Any planner sharing the store |
| Session memory persistence | In-memory, per planner instance | Shared across instances |
| Remote binding tracking | Not tracked | Persisted; `A2AAgentToolset` reuses remote contexts |
| Background task records | Lost on restart | Reloaded by `StreamingSession.hydrate()` (running work is not restarted) |
| Steering event history | Ephemeral | Full audit trail |

### Design Philosophy

1. **Optional but recommended** - Agents work without a StateStore, but persistence features require one
2. **Protocol-based (duck-typing)** - No inheritance required; just implement the methods
3. **Errors mostly propagate** - The library catches and logs StateStore failures only on a few paths (A2A server binding saves, planner pause saves, memory hydrate/persist, background trajectory and planner-event writes). Session writes, including `save_event` through `StateStoreSessionAdapter`, propagate to the caller. Treat every method as "must not throw" and degrade gracefully (timeouts, circuit breakers, dead-letter queues, etc.); see [Error Handling](#error-handling)
4. **Async-only** - All methods must be async/awaitable
5. **Idempotent writes** - `save_event` may be called multiple times for the same event (retries)
6. **Capability detection** - Optional features detected via `hasattr()` / `getattr()` at runtime

---

## Protocol Definition

**Location:** `penguiflow/state/protocol.py` (import path remains `penguiflow.state`)

```python
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from penguiflow.state import RemoteBinding, StoredEvent


@runtime_checkable
class StateStore(Protocol):
    """Protocol for durable state adapters used by PenguiFlow."""

    async def save_event(self, event: StoredEvent) -> None:
        """Persist a runtime event."""
        ...

    async def load_history(self, trace_id: str) -> Sequence[StoredEvent]:
        """Return the ordered history for a trace id."""
        ...

    async def save_remote_binding(self, binding: RemoteBinding) -> None:
        """Persist the mapping between a trace and an external worker."""
        ...
```

### Optional Capability Protocols

PenguiFlow defines additional protocols for optional features (all `@runtime_checkable`, all exported from `penguiflow.state`):

```python
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from penguiflow.artifacts import ArtifactStore
from penguiflow.planner import PlannerEvent, Trajectory
from penguiflow.state import RemoteBinding, StateUpdate, SteeringEvent, TaskState


@runtime_checkable
class SupportsPlannerState(Protocol):
    async def save_planner_state(self, token: str, payload: dict[str, Any]) -> None: ...
    async def load_planner_state(self, token: str) -> dict[str, Any] | None: ...

@runtime_checkable
class SupportsConversationBindings(Protocol):
    async def find_binding(
        self,
        *,
        router_session_id: str,
        agent_url: str,
        remote_skill: str,
        tenant_id: str | None = None,
        user_id: str | None = None,
    ) -> RemoteBinding | None: ...

    async def list_bindings(self, *, router_session_id: str) -> Sequence[RemoteBinding]: ...

    async def mark_binding_terminal(self, *, trace_id: str, context_id: str | None, task_id: str) -> None: ...

@runtime_checkable
class SupportsMemoryState(Protocol):
    async def save_memory_state(self, key: str, state: dict[str, Any]) -> None: ...
    async def load_memory_state(self, key: str) -> dict[str, Any] | None: ...

@runtime_checkable
class SupportsTasks(Protocol):
    async def save_task(self, state: TaskState) -> None: ...
    async def list_tasks(self, session_id: str) -> Sequence[TaskState]: ...
    async def save_update(self, update: StateUpdate) -> None: ...
    async def list_updates(self, session_id: str, *, task_id: str | None = None,
                           since_id: str | None = None, limit: int = 500) -> Sequence[StateUpdate]: ...

@runtime_checkable
class SupportsSteering(Protocol):
    async def save_steering(self, event: SteeringEvent) -> None: ...
    async def list_steering(self, session_id: str, *, task_id: str | None = None,
                            since_id: str | None = None, limit: int = 500) -> Sequence[SteeringEvent]: ...

@runtime_checkable
class SupportsTrajectories(Protocol):
    async def save_trajectory(self, trace_id: str, session_id: str, trajectory: Trajectory) -> None: ...
    async def get_trajectory(self, trace_id: str, session_id: str) -> Trajectory | None: ...
    async def list_traces(self, session_id: str, limit: int = 50) -> list[str]: ...

@runtime_checkable
class SupportsPlannerEvents(Protocol):
    async def save_planner_event(self, trace_id: str, event: PlannerEvent) -> None: ...
    async def list_planner_events(self, trace_id: str) -> list[PlannerEvent]: ...

@runtime_checkable
class SupportsArtifacts(Protocol):
    @property
    def artifact_store(self) -> ArtifactStore | None: ...
```

### Protocol Checking

PenguiFlow validates StateStore implementations at runtime. The admin CLI (`penguiflow-admin`) checks the object its factory returns against the `runtime_checkable` protocol, which only tests that the three required methods exist:

```python
from penguiflow.state import StateStore


def check_store(instance: object) -> StateStore:
    if not isinstance(instance, StateStore):
        msg = "StateStore factories must implement save_event/load_history/save_remote_binding"
        raise TypeError(msg)
    return instance
```

### Capability Detection Helpers

```python
from penguiflow.state import missing_capabilities, require_capabilities

store = ...  # your StateStore

# Check which methods are missing
missing = missing_capabilities(store, ["save_task", "list_tasks"])

# Fail fast if critical methods are missing (raises TypeError)
require_capabilities(store, feature="sessions", methods=["save_task", "list_tasks"])
```

---

## Data Types

### StoredEvent

Represents a persisted audit event (for example the session/task snapshots written by the session store adapter).

```python
@dataclass(slots=True)
class StoredEvent:
    trace_id: str | None       # Trace identifier (nullable for global events)
    ts: float                  # Unix timestamp (time.time())
    kind: str                  # Event type (see Event Types below)
    node_name: str | None      # Emitting component, if applicable
    node_id: str | None        # Emitting component instance ID (the task id for session pseudo-events)
    payload: Mapping[str, Any] # Structured event data
```

#### Event Types (`kind` field)

`StoredEvent.kind` is a **string discriminator**.

Important: `kind` values are an **open set**. StateStore backends MUST accept novel/unknown kinds and persist them as opaque strings. Applications may write their own audit events through `save_event`.

1) **Session/task pseudo-events**: when a session store is adapted onto a core `StateStore` (`StateStoreSessionAdapter`), it persists session state as audit-log entries under `trace_id="session:{session_id}"`, and reads them back with `load_history` to answer `list_tasks` / `list_updates` / `list_steering`.

| Event Kind | Description |
|------------|-------------|
| `session.task` | Task lifecycle snapshot persisted (`TaskStateModel` dump) |
| `session.update` | Task streaming/progress update persisted (`StateUpdate` dump) |
| `session.steering` | Steering event persisted (`SteeringEvent` dump) |

2) **Planner/tool events are *not* `StoredEvent`s.** They are `PlannerEvent`s (see `SupportsPlannerEvents`) and have their own `event_type` values such as `stream_chunk`, `llm_stream_chunk`, and `artifact_chunk`.

> **Callout (important):** If you want to persist tool/LLM streaming events for a UI client or trace replay, implement `SupportsPlannerEvents` (`save_planner_event` / `list_planner_events`). Do **not** attempt to cram these into `save_event` as `StoredEvent.kind="stream_chunk"`; PenguiFlow treats those as different channels.

#### Payload Structure

The `payload` dict contains event-specific fields. For the session pseudo-events it is the JSON-mode dump of the record (task state, update, or steering event). Backends should store it as JSON and never interpret it beyond indexing.

### RemoteBinding

Associates a trace with a remote A2A task and, for `A2AAgentToolset` continuity, with the router conversation that called it.

```python
@dataclass(slots=True)
class RemoteBinding:
    trace_id: str           # Local trace id (the A2A server uses the task id)
    context_id: str | None  # Remote A2A context ID
    task_id: str            # Remote A2A task ID
    agent_url: str          # Remote agent URL
    router_session_id: str | None = None  # Local manager/router session ID
    remote_skill: str | None = None       # Remote specialist skill name/id
    tenant_id: str | None = None          # Tenant scope for reuse isolation
    user_id: str | None = None            # User scope for reuse isolation
    last_remote_task_id: str | None = None
    is_terminal: bool = False             # Terminal bindings are not reused
    metadata: Mapping[str, Any] = field(default_factory=dict)
```

The identity of a binding is `(trace_id, context_id, task_id)`. The seven fields after `agent_url` are additive: the A2A server only sets the first four, `A2AAgentToolset` sets all of them.

### TaskState

Represents the complete state of a foreground or background task.

```python
@dataclass(slots=True)
class TaskState:
    task_id: str                           # Unique task identifier
    session_id: str                        # Session this task belongs to
    status: TaskStatus                     # Current lifecycle state
    task_type: TaskType                    # FOREGROUND or BACKGROUND
    priority: int                          # Scheduling priority (higher = more urgent)
    context_snapshot: TaskContextSnapshot  # Full execution context at spawn time
    trace_id: str | None = None            # Distributed tracing ID
    result: Any | None = None              # Task result payload
    error: str | None = None               # Error message if failed
    description: str | None = None         # Human-readable task description
    progress: dict[str, Any] | None = None # Progress tracking metadata
    created_at: datetime = field(default_factory=_utc_now)
    updated_at: datetime = field(default_factory=_utc_now)

    def update_status(self, status: TaskStatus) -> None:
        """Update status and refresh updated_at timestamp."""
        self.status = status
        self.updated_at = _utc_now()
```

`TaskState` is a mutable dataclass. To serialise it, use `TaskStateModel.from_state(state).model_dump(mode="json")` (what `StateStoreSessionAdapter` does) and rebuild it from `TaskStateModel.model_validate(...)`.

#### TaskStatus Enum

```python
class TaskStatus(str, Enum):
    PENDING = "PENDING"       # Awaiting execution
    RUNNING = "RUNNING"       # Currently executing
    PAUSED = "PAUSED"         # Paused for approval
    COMPLETE = "COMPLETE"     # Completed successfully
    FAILED = "FAILED"         # Execution failed
    CANCELLED = "CANCELLED"   # Cancelled by user/parent
```

#### TaskType Enum

```python
class TaskType(str, Enum):
    FOREGROUND = "FOREGROUND"  # Interactive, blocks session
    BACKGROUND = "BACKGROUND"  # Async, runs in parallel
```

### TaskContextSnapshot

Captures the full execution context at task spawn time. Used for context isolation and divergence detection.

```python
class TaskContextSnapshot(BaseModel):
    session_id: str                                    # Session identifier
    task_id: str                                       # Task identifier
    trace_id: str | None = None                        # Distributed tracing ID
    spawned_from_task_id: str = "foreground"           # Parent task ID
    spawned_from_event_id: str | None = None           # Triggering event ID
    spawned_at: datetime = Field(default_factory=_utc_now)
    spawn_reason: str | None = None                    # Why task was created
    query: str | None = None                           # User query if applicable
    propagate_on_cancel: str = "cascade"               # "cascade" or "isolate"
    notify_on_complete: bool = True                    # Emit notification on completion

    # Context versioning for divergence detection
    context_version: int | None = None                 # Version at spawn time
    context_hash: str | None = None                    # SHA256 hash at spawn time

    # Copies of context at spawn time (deep, JSON-safe copies for background tasks)
    llm_context: dict[str, Any] = Field(default_factory=dict)
    tool_context: dict[str, Any] = Field(default_factory=dict)
    memory: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
```

### StateUpdate

Represents a streaming progress update from a task.

```python
class StateUpdate(BaseModel):
    session_id: str                        # Session identifier
    task_id: str                           # Task identifier
    trace_id: str | None = None            # Distributed tracing ID
    update_id: str = Field(default_factory=lambda: uuid.uuid4().hex)  # Deduplication key
    update_type: UpdateType                # Type of update
    content: Any                           # Dynamic payload
    step_index: int | None = None          # Current step number
    total_steps: int | None = None         # Total expected steps
    created_at: datetime = Field(default_factory=_utc_now)
```

#### UpdateType Enum

```python
class UpdateType(str, Enum):
    THINKING = "THINKING"           # Internal agent reasoning
    PROGRESS = "PROGRESS"           # Step-by-step progress
    TOOL_CALL = "TOOL_CALL"         # Tool invocation events
    RESULT = "RESULT"               # Final results or answer chunks
    ERROR = "ERROR"                 # Error occurred
    CHECKPOINT = "CHECKPOINT"       # Approval/pause points
    STATUS_CHANGE = "STATUS_CHANGE" # Task status transitions
    NOTIFICATION = "NOTIFICATION"   # UI notifications
```

### SteeringEvent

Represents a bidirectional control event (user intervention).

```python
class SteeringEvent(BaseModel):
    session_id: str                                   # Session identifier
    task_id: str                                      # Target task
    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    event_type: SteeringEventType                     # Type of steering
    payload: dict[str, Any] = Field(default_factory=dict)
    trace_id: str | None = None                       # Distributed tracing ID
    source: str = "user"                              # Event origin ("user", "system", "agent")
    created_at: datetime = Field(default_factory=_utc_now)

    def to_injection(self) -> str:
        """Convert to JSON injection for LLM prompt context."""
        ...
```

#### SteeringEventType Enum

```python
class SteeringEventType(str, Enum):
    INJECT_CONTEXT = "INJECT_CONTEXT"   # Inject contextual information
    REDIRECT = "REDIRECT"               # Change execution goal/instruction
    CANCEL = "CANCEL"                   # Cancel execution with optional reason
    PRIORITIZE = "PRIORITIZE"           # Adjust task priority
    PAUSE = "PAUSE"                     # Pause execution
    RESUME = "RESUME"                   # Resume paused execution
    APPROVE = "APPROVE"                 # Approve pending decision/patch
    REJECT = "REJECT"                   # Reject pending decision/patch
    USER_MESSAGE = "USER_MESSAGE"       # User sends message while task is working
```

---

## Required Methods

### 1. `save_event(event: StoredEvent) -> None`

**Purpose:** Persist an audit event for audit/replay.

**When Called:** By `StateStoreSessionAdapter` for every session task, update and steering write when the store only implements the core protocol; by your own code for application audit events.

**Contract:**
- MUST be idempotent (duplicate calls for same event should not fail)
- MUST NOT block indefinitely (use timeouts)
- SHOULD handle `trace_id=None` for global events (recommended: normalise to a sentinel like `"__global__"`)
- MAY store events asynchronously, but `StateStoreSessionAdapter` reads session events back through `load_history`, so session state lags by whatever delay you add

**Parameters:**
| Parameter | Type | Description |
|-----------|------|-------------|
| `event` | `StoredEvent` | The event to persist |

**Example Implementation:**
```python
async def save_event(self, event: StoredEvent) -> None:
    trace_id = event.trace_id or "__global__"
    raw = json.dumps(
        {
            "trace_id": trace_id,
            "ts": event.ts,
            "kind": event.kind,
            "node_name": event.node_name,
            "node_id": event.node_id,
            "payload": dict(event.payload),
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    event_fp = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    await self._pool.execute(
        """
        INSERT INTO flow_events (trace_id, ts, kind, node_name, node_id, event_fp, payload)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (trace_id, event_fp) DO NOTHING  -- Idempotent
        """,
        trace_id,
        event.ts,
        event.kind,
        event.node_name,
        event.node_id,
        event_fp,
        json.dumps(dict(event.payload)),
    )
```

### 2. `load_history(trace_id: str) -> Sequence[StoredEvent]`

**Purpose:** Retrieve all events for a trace, ordered by timestamp.

**When Called:** By `StateStoreSessionAdapter` (trace `session:{session_id}`) and by `penguiflow-admin history` / `replay`.

**Contract:**
- MUST return events in chronological order (ascending `ts`)
- MUST return empty sequence if trace not found (NOT raise exception)
- MUST return a `Sequence[StoredEvent]` (list, tuple, etc.)

**Parameters:**
| Parameter | Type | Description |
|-----------|------|-------------|
| `trace_id` | `str` | The trace identifier to query |

**Returns:** `Sequence[StoredEvent]` - Ordered list of events

**Example Implementation:**
```python
async def load_history(self, trace_id: str) -> Sequence[StoredEvent]:
    rows = await self._pool.fetch(
        """
        SELECT trace_id, ts, kind, node_name, node_id, payload
        FROM flow_events
        WHERE trace_id = $1
        ORDER BY ts ASC, id ASC  -- Secondary sort for same-ts events
        """,
        trace_id,
    )
    return [
        StoredEvent(
            trace_id=row["trace_id"],
            ts=row["ts"],
            kind=row["kind"],
            node_name=row["node_name"],
            node_id=row["node_id"],
            payload=json.loads(row["payload"]) if row["payload"] else {},
        )
        for row in rows
    ]
```

### 3. `save_remote_binding(binding: RemoteBinding) -> None`

**Purpose:** Persist A2A remote task associations, and the conversation bindings `A2AAgentToolset` reuses.

**When Called:**
- by `A2AService` for each task run when `A2AConfig.agent_url` is set (best-effort: failures are logged);
- by `A2AAgentToolset` after each remote call, when the run has `tool_context["session_id"]` (failures propagate to the tool call).

**Contract:**
- MUST be idempotent (same binding may be saved multiple times)
- SHOULD support upsert semantics (update if exists)
- SHOULD use the composite key `(trace_id, context_id, task_id)`, treating a null `context_id` as a value
- MUST preserve additive A2A router-continuity fields when present:
  `router_session_id`, `remote_skill`, `tenant_id`, `user_id`,
  `last_remote_task_id`, `is_terminal`, `metadata`
- MUST remain backward-compatible with legacy bindings where the additive fields are absent/null

**Parameters:**
| Parameter | Type | Description |
|-----------|------|-------------|
| `binding` | `RemoteBinding` | The binding to persist |

**Example Implementation:**
```python
async def save_remote_binding(self, binding: RemoteBinding) -> None:
    await self._pool.execute(
        """
        INSERT INTO remote_bindings (
            trace_id, context_id, task_id, agent_url,
            router_session_id, remote_skill, tenant_id, user_id,
            last_remote_task_id, is_terminal, metadata
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
        ON CONFLICT (trace_id, context_id, task_id) DO UPDATE
        SET agent_url = EXCLUDED.agent_url,
            router_session_id = EXCLUDED.router_session_id,
            remote_skill = EXCLUDED.remote_skill,
            tenant_id = EXCLUDED.tenant_id,
            user_id = EXCLUDED.user_id,
            last_remote_task_id = EXCLUDED.last_remote_task_id,
            is_terminal = EXCLUDED.is_terminal,
            metadata = EXCLUDED.metadata,
            updated_at = NOW()
        """,
        binding.trace_id,
        binding.context_id,
        binding.task_id,
        binding.agent_url,
        binding.router_session_id,
        binding.remote_skill,
        binding.tenant_id,
        binding.user_id,
        binding.last_remote_task_id,
        binding.is_terminal,
        json.dumps(dict(binding.metadata or {})),
    )
```

---

## Optional Duck-Typed Methods

These methods are **not part of the `StateStore` Protocol** but are detected at runtime via `hasattr()` / `getattr()`. Implement them to enable advanced features. Each group also has its own `Supports*` protocol (see [Optional Capability Protocols](#optional-capability-protocols)).

### 4. `find_binding(...) -> RemoteBinding | None`

**Purpose:** Enable `A2AAgentToolset` manager/router agents to reuse a specialist's remote `contextId` across turns.

**When Called:** `A2AAgentToolset` (and `A2ARouterToolset`, which delegates through it) calls this before invoking a remote specialist, when the run has `tool_context["session_id"]`. The store is taken from `tool_context["state_store"]`, falling back to the planner's `planner.state_store`. If the method is missing, the call still works but starts a fresh remote context. `tenant_id` and `user_id` are passed only when your method accepts those keywords.

**Signature:**
```python
async def find_binding(
    self,
    *,
    router_session_id: str,
    agent_url: str,
    remote_skill: str,
    tenant_id: str | None = None,
    user_id: str | None = None,
) -> RemoteBinding | None:
    ...
```

**Contract:**
- MUST return the newest non-terminal binding for `(router_session_id, agent_url, remote_skill)` after tenant/user filtering.
- MUST NOT return bindings where `router_session_id` or `remote_skill` is null.
- MUST NOT return `is_terminal=True` bindings.
- MUST isolate tenant/user scope. If either the lookup or stored binding has `tenant_id`/`user_id`, values must match.
- SHOULD order by updated/saved time descending, with a stable tie-breaker.
- MUST return the stored `context_id`; `A2AAgentToolset` uses it for normal continuity and omits `task_id` unless the binding metadata indicates an unfinished input/auth-required remote task.
- SHOULD preserve `metadata` flags written by A2A tooling, especially `awaiting_remote_input`, `awaiting_remote_auth`, `remote_task_state`, and `remote_task_terminal`.

**Tenant/user matching rule:**
```python
def scope_matches(row: RemoteBinding, tenant_id: str | None, user_id: str | None) -> bool:
    if (tenant_id is not None or row.tenant_id is not None) and row.tenant_id != tenant_id:
        return False
    return not ((user_id is not None or row.user_id is not None) and row.user_id != user_id)
```

### 5. `list_bindings(router_session_id=...) -> Sequence[RemoteBinding]`

**Purpose:** Debugging, inspection, and router/session tooling. The library itself does not call it.

**Contract:**
- Return all bindings for the router session.
- Include terminal and non-terminal rows.
- Return an empty sequence for unknown sessions.

### 6. `mark_binding_terminal(...) -> None`

**Purpose:** Prevent reuse of a remote binding after terminal/resume-incompatible states.

**Signature:**
```python
async def mark_binding_terminal(
    self,
    *,
    trace_id: str,
    context_id: str | None,
    task_id: str,
) -> None:
    ...
```

**Contract:**
- Mark the exact `(trace_id, context_id, task_id)` binding as terminal.
- Be idempotent; missing rows should be a no-op.
- Remove/invalidate any active secondary index entry for that binding.

**Current call-site note:** the library never calls `mark_binding_terminal`. Bindings are scoped to the router conversation: `A2AAgentToolset` always saves them with `is_terminal=False` and decides from the stored metadata whether to continue the remote task or only the remote context. Call it from your own code when a conversation should stop reusing a specialist context.

### 7. `save_planner_state(token: str, payload: dict) -> None`

**Purpose:** Persist planner pause state for distributed resume (OAuth/HITL).

**When Called:** When `ReactPlanner` pauses execution (for example a tool calls `ctx.pause(...)`, or a ToolNode waits for an OAuth callback). Pausing requires `pause_enabled=True`, the default.

**Contract:**
- MUST be idempotent (token is unique per pause)
- SHOULD set expiration (recommended: 1 hour)
- MUST serialize `payload` as JSON

**Parameters:**
| Parameter | Type | Description |
|-----------|------|-------------|
| `token` | `str` | Unique pause token: 32 hex characters (`uuid4().hex`) |
| `payload` | `dict` | Serialized pause state |

**Payload Structure:**
```python
{
    "trajectory": {...},           # Trajectory.serialise() payload (includes steps + context)
    "reason": "await_input",       # Pause reason string
    "payload": {...},              # Caller-provided pause payload (OAuth/HITL metadata)
    "constraints": {...} | None,   # Constraint tracker snapshot (optional)
    "tool_context": {...} | None,  # tool_context snapshot, or None if it is not JSON-serialisable
}
```

> **Callout (gold standard):** Treat the pause payload as a *wire format*, not an internal object dump.
> The runtime currently persists `Trajectory.serialise()` plus minimal metadata (see `penguiflow/planner/pause_management.py`).
> Downstream stores should store and return the payload **losslessly** (no lossy JSON coercion beyond ensuring JSON-serialisability).

The pausing planner also keeps the record in memory (`planner.pause_records`). A `resume()` on that same instance uses the in-memory copy and never calls `load_planner_state`, so the stored record is consumed only when another planner instance resumes; otherwise it lives until it expires. A failing `save_planner_state` is logged (`state_store_save_failed`) and the pause still succeeds, but then only the pausing instance can resume it.

**Example Implementation:**
```python
async def save_planner_state(self, token: str, payload: dict) -> None:
    await self._pool.execute(
        """
        INSERT INTO planner_pauses (token, payload, expires_at)
        VALUES ($1, $2, NOW() + INTERVAL '1 hour')
        ON CONFLICT (token) DO UPDATE
        SET payload = EXCLUDED.payload,
            expires_at = NOW() + INTERVAL '1 hour'
        """,
        token,
        json.dumps(payload),
    )
```

### 8. `load_planner_state(token: str) -> dict | None`

**Purpose:** Load planner pause state for resume.

**When Called:** When `ReactPlanner.resume(token)` is called and the token is not in that planner instance's memory.

**Contract:**
- SHOULD return `None` if token not found or expired
- MUST NOT raise exceptions for missing tokens
- SHOULD delete/expire token after successful load (one-time use)

**Parameters:**
| Parameter | Type | Description |
|-----------|------|-------------|
| `token` | `str` | The pause token to load |

**Returns:** `dict | None` - The stored payload, or `None` if not found

> **Callout (missing token semantics):** `ReactPlanner` treats `None` as the clean "not found/expired" signal and raises `KeyError(token)` from `resume()`.
> Returning `{}` for missing tokens tends to produce confusing downstream errors (`KeyError: 'trajectory'`) instead of a
> clear missing-token path. Prefer returning `None`. Any exception your method raises is logged (`state_store_load_failed`) and re-raised as `KeyError`.

**Example Implementation:**
```python
async def load_planner_state(self, token: str) -> dict | None:
    row = await self._pool.fetchrow(
        """
        DELETE FROM planner_pauses
        WHERE token = $1 AND expires_at > NOW()
        RETURNING payload
        """,
        token,
    )
    if not row:
        return None
    return json.loads(row["payload"])
```

### 9. `save_memory_state(key: str, state: dict[str, Any]) -> None`

**Purpose:** Persist short-term memory state for session continuity.

**When Called:** After a `run()` or `resume()` that ends with `PlannerFinish` (not on a pause), when `short_term_memory` uses a strategy other than `"none"` and a memory key resolves. Failures are logged as `memory_persist_failed`.

**Contract:**
- MUST be idempotent (same key can be saved multiple times)
- SHOULD use upsert semantics
- Key format: `"{tenant_id}:{user_id}:{session_id}"` (`MemoryKey.composite()`)

The key comes from the explicit `memory_key=` argument of `run()` / `resume()`, or from `tool_context` at the `MemoryIsolation` key paths (defaults `tenant_id`, `user_id`, `session_id`). A missing tenant or user becomes `default` / `anonymous`; a missing session id means no memory for that run (with the default `require_explicit_key=True`). Put `tenant_id`, not `tenant`, in `tool_context`, or two tenants that share a session id share memory.

**Parameters:**
| Parameter | Type | Description |
|-----------|------|-------------|
| `key` | `str` | Composite memory key |
| `state` | `dict[str, Any]` | Memory state to persist |

**State Structure:**
```python
{
    "version": 1,                  # Schema version
    "health": "healthy",           # Memory health state
    "summary": "...",              # Rolling summary (if using rolling_summary strategy)
    "turns": [                     # Recent conversation turns
        {
            "user_message": "...",
            "assistant_response": "...",
            "trajectory_digest": {...},  # or None
            "artifacts_shown": {...},
            "artifacts_hidden_refs": [...],
            "ts": 1702857600.0
        }
    ],
    "pending": [...],              # Turns awaiting summarization
    "backlog": [...],              # Recovery buffer during degradation
    "config_snapshot": {...}       # Configuration at save time
}
```

**Example Implementation:**
```python
async def save_memory_state(self, key: str, state: dict[str, Any]) -> None:
    await self._pool.execute(
        """
        INSERT INTO memory_states (key, state, updated_at)
        VALUES ($1, $2, NOW())
        ON CONFLICT (key) DO UPDATE
        SET state = EXCLUDED.state,
            updated_at = NOW()
        """,
        key,
        json.dumps(state),
    )
```

### 10. `load_memory_state(key: str) -> dict[str, Any] | None`

**Purpose:** Load persisted memory state for session hydration.

**When Called:** At the start of every `run()` and `resume()` that resolves a memory key, when `short_term_memory` is configured. A non-`None` result replaces the planner's in-memory state for that key. Failures are logged as `memory_hydrate_failed`.

**Contract:**
- MUST return `None` if key not found (NOT raise exception)
- SHOULD return the exact state that was saved (the loader rejects any `version` other than `1`)

**Parameters:**
| Parameter | Type | Description |
|-----------|------|-------------|
| `key` | `str` | Composite memory key |

**Returns:** `dict[str, Any] | None` - The stored state, or `None` if not found

**Example Implementation:**
```python
async def load_memory_state(self, key: str) -> dict[str, Any] | None:
    row = await self._pool.fetchrow(
        "SELECT state FROM memory_states WHERE key = $1",
        key,
    )
    if not row:
        return None
    return json.loads(row["state"])
```

---

### 11. Task Persistence (`save_task`, `list_tasks`, `save_update`, `list_updates`)

**Purpose:** Persist background/foreground task lifecycle state and streaming updates.

**Location (integration):** `penguiflow/sessions/session.py`

**Contracts:**
- `save_task()` MUST be idempotent or upsert-safe (same task is updated many times).
- `list_tasks()` MUST return all tasks for the session (order not important).
- `save_update()` SHOULD be append-only, idempotent via `update_id` if possible.
- `list_updates()` SHOULD return updates in stable order and treat `since_id` as an *exclusive* cursor.
- Reads MUST return an empty sequence on "not found" (do not raise).

**Signature:**
```python
async def save_task(self, state: TaskState) -> None: ...
async def list_tasks(self, session_id: str) -> Sequence[TaskState]: ...
async def save_update(self, update: StateUpdate) -> None: ...
async def list_updates(
    self,
    session_id: str,
    *,
    task_id: str | None = None,
    since_id: str | None = None,
    limit: int = 500,
) -> Sequence[StateUpdate]: ...
```

**Notes:**
- `StateUpdate.update_id` is generated client-side; use it as the natural idempotency key.
- If `since_id` is unknown, treat it as "no cursor".
- To preserve cursor semantics, avoid filtering after applying limit.
- The reference stores apply the cursor and the `task_id` filter first, then return the **newest** `limit` matches in chronological order.
- A store with `save_task` is used directly as the session store, so it must also implement `save_update`, `list_updates`, `save_steering` and `list_steering` (see [StreamingSession / SessionManager](#4-streamingsession--sessionmanager)).

### 12. Steering Persistence (`save_steering`, `list_steering`)

**Purpose:** Persist bidirectional steering/intervention events (USER_MESSAGE, CANCEL, APPROVE, etc.).

**Location (integration):** `penguiflow/sessions/session.py`

**Contracts:**
- Steering payloads are untrusted; persist the sanitized form (see [Steering Payload Sanitization](#steering-payload-sanitization)).
- `list_steering()` SHOULD return events in stable order and treat `since_id` as an *exclusive* cursor.

**Signature:**
```python
async def save_steering(self, event: SteeringEvent) -> None: ...
async def list_steering(
    self,
    session_id: str,
    *,
    task_id: str | None = None,
    since_id: str | None = None,
    limit: int = 500,
) -> Sequence[SteeringEvent]: ...
```

### 13. Trajectory Persistence (`save_trajectory`, `get_trajectory`, `list_traces`)

**Purpose:** Persist planner trajectories for replay, debugging, and UI inspection.

**Location (integration):** `penguiflow/planner/react_runtime.py`

> **Note:** `ReactPlanner` persists the trajectory after `run()` / `resume()` returns (`PlannerFinish` or `PlannerPause`), as a fire-and-forget background task. It only does so when `tool_context` carries both `trace_id` and `session_id`; failures are logged as warnings.

**Contracts:**
- `get_trajectory()` MUST return `None` when not found or when session_id does not match.
- `list_traces()` MUST return trace IDs for the session, most recent first.

**Signature:**
```python
async def save_trajectory(self, trace_id: str, session_id: str, trajectory: Trajectory) -> None: ...
async def get_trajectory(self, trace_id: str, session_id: str) -> Trajectory | None: ...
async def list_traces(self, session_id: str, limit: int = 50) -> list[str]: ...
```

### 14. Planner Event Persistence (`save_planner_event`, `list_planner_events`)

**Purpose:** Persist structured planner/tool execution events for UI streaming and auditing.

**Location (integration):** `penguiflow/planner/react_runtime.py`

> **Note:** Planner events are persisted by the `ReactPlanner` itself. Events are buffered during execution and flushed as a fire-and-forget background task on every exit path (finish, pause, error, cancellation). Events are only flushed when `tool_context` carries a `trace_id`; without one the buffer is discarded. Failures are logged as warnings.

**Contract:**
- Planner events are append-only.
- If your backend supports it, store an insertion sequence to preserve stable ordering.

**Signature:**
```python
async def save_planner_event(self, trace_id: str, event: PlannerEvent) -> None: ...
async def list_planner_events(self, trace_id: str) -> list[PlannerEvent]: ...
```

---

## Context Versioning & Divergence Detection

Background tasks receive a **snapshot** of the session context at spawn time. During execution, the foreground may modify the context. PenguiFlow detects this divergence when merging results.

### SessionContext Fields

```python
@dataclass(slots=True)
class SessionContext:
    llm_context: dict[str, Any] = field(default_factory=dict)
    tool_context: dict[str, Any] = field(default_factory=dict)
    memory: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    background_results: dict[str, BackgroundTaskResult] = field(default_factory=dict)  # by task_id
    version: int = 0                # Incremented on every update
    context_hash: str | None = None # SHA256 of llm_context
```

### Context Hash Computation

```python
import hashlib
import json
from typing import Any


def _hash_context(payload: dict[str, Any]) -> str | None:
    try:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
```

### ContextPatch for Background Task Results

When a background task completes, it creates a `ContextPatch` (`penguiflow.sessions.ContextPatch`):

```python
class ContextPatch(BaseModel):
    task_id: str
    spawned_from_event_id: str | None = None
    source_context_version: int | None = None   # Version at spawn time
    source_context_hash: str | None = None      # Hash at spawn time
    context_diverged: bool = False              # Set if divergence detected
    completed_at: datetime = Field(default_factory=_utc_now)

    # Patch content
    digest: list[str] = Field(default_factory=list)          # Summary text
    facts: dict[str, Any] = Field(default_factory=dict)      # Extracted facts
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    sources: list[dict[str, Any]] = Field(default_factory=list)
    recommended_next_steps: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
```

### Divergence Detection Logic

`StreamingSession.apply_context_patch(patch=..., strategy=...)` compares the patch with the session's current context:

```python
def detect_divergence(patch: ContextPatch, context: SessionContext) -> ContextPatch:
    diverged = (
        patch.source_context_version is not None
        and patch.source_context_version != context.version
    ) or bool(
        patch.source_context_hash
        and patch.source_context_hash != context.context_hash
    )
    if diverged and not patch.context_diverged:
        patch = patch.model_copy(update={"context_diverged": True})
    return patch
```

For `APPEND` / `REPLACE`, a diverged patch also publishes a `NOTIFICATION` update ("Context changed"). For `HUMAN_GATED`, the flag travels in the approval checkpoint as `"diverged"`.

### Implications for StateStore

When persisting `TaskState`, ensure:
- `TaskContextSnapshot.context_version` and `context_hash` are preserved
- The copies of `llm_context`, `tool_context`, `memory`, `artifacts` are stored
- These fields enable divergence detection on resume/hydration

---

## Steering Payload Sanitization

Steering payloads are **untrusted user input**. PenguiFlow sanitizes them before processing and persistence.

### Size Limits

```python
MAX_STEERING_PAYLOAD_BYTES = 16_384  # 16 KB (SessionLimits.max_steering_payload_bytes)
MAX_STEERING_DEPTH = 6               # Nesting depth
MAX_STEERING_KEYS = 64               # Dict keys per level
MAX_STEERING_LIST_ITEMS = 50         # List items
MAX_STEERING_STRING = 4_096          # String character length
```

### Sanitization Process

1. **Clamp recursively**: nesting beyond the depth limit becomes `"<truncated>"`; dicts keep 64 keys (marked `__truncated_keys__`) with key names cut to 64 characters; lists keep 50 items (plus a `"<truncated>"` marker); strings are cut to 4,096 characters; other values become strings
2. **Check JSON serializability**: a payload that still cannot be encoded becomes `{"value": ..., "truncated": True}`
3. **Enforce the byte limit**: a payload over the limit becomes `{"truncated": True, "summary": ...}`

### Event-Specific Payload Validation

| Event Type | Required Fields | Optional Fields |
|------------|-----------------|-----------------|
| `INJECT_CONTEXT` | `text` (non-empty string) | `scope` ("foreground"/"task_only"), `severity` ("note"/"correction") |
| `REDIRECT` | `instruction\|goal\|query` (non-empty string) | `constraints` (dict) |
| `CANCEL` | - | `reason` (string), `hard` (bool) |
| `PRIORITIZE` | `priority` (int) | - |
| `APPROVE/REJECT` | `resume_token\|patch_id\|event_id` | `decision` (string) |
| `PAUSE/RESUME` | - | `reason` (string) |
| `USER_MESSAGE` | `text` (non-empty string) | `active_tasks` (list of task_ids) |

### StateStore Implications

- **Always persist sanitized payloads** - never raw user input
- `StreamingSession.steer()` sanitizes, validates and deduplicates an event before calling `save_steering()`; an invalid event is dropped (`steer()` returns `False`) and never persisted
- `validate_steering_event()` raises `SteeringValidationError` if you validate events yourself
- The `InMemoryStateStore` reference implementation sanitizes again in `save_steering()`

---

## Merge Strategies

When background tasks complete, their results can be merged into the session context using different strategies.

### MergeStrategy Enum

```python
class MergeStrategy(str, Enum):
    APPEND = "append"           # Record in SessionContext.background_results
    REPLACE = "replace"         # Same as APPEND today: keyed by task_id
    HUMAN_GATED = "human_gated" # Queue for user approval
```

### APPEND and REPLACE Strategies

Both record the patch as a `BackgroundTaskResult` in `SessionContext.background_results`, keyed by task id, so a later result for the same task replaces the earlier one:

```python
result = _background_result_from_patch(patch, payload=payload, group_id=group_id)
self._context.background_results[result.task_id] = result
```

Results are not written into `llm_context`. Legacy `llm_context["background_results"]` / `["background_result"]` entries are migrated into this mapping, and new task snapshots render the mapping back into the snapshot's `llm_context["background_results"]` as a list of payloads.

### HUMAN_GATED Strategy

Results are queued for user approval via a `CHECKPOINT` update:

```python
patch_id = secrets.token_hex(8)
self._pending_patches[patch_id] = PendingContextPatch(
    patch_id=patch_id,
    task_id=patch.task_id,
    patch=patch,
    strategy=strategy,
)  # created_at defaults to time.time()

self.publish(
    StateUpdate(
        session_id=self.session_id,
        task_id=patch.task_id,
        update_type=UpdateType.CHECKPOINT,
        content={
            "kind": "context_patch",
            "patch_id": patch_id,
            "task_id": patch.task_id,
            "digest": patch.digest,
            "diverged": patch.context_diverged,
            "prompt": "Apply background results to the conversation?",
            "options": ["approve", "reject"],
        },
    )
)
```

At most `SessionLimits.max_pending_patches` (default 32) patches wait at once; one more raises `RuntimeError("pending_patch_limit_exceeded")`. Pending patches live in the session's memory only: the checkpoint update is persisted through `save_update`, the patch itself is not.

### Handling Approval/Rejection

- `await session.apply_pending_patch(patch_id=..., strategy=None)` applies a pending patch (default strategy `APPEND`) and publishes a "Context updated" notification; it returns `False` for an unknown id.
- A steering `APPROVE` / `REJECT` event with `payload={"patch_id": ...}` applies or drops the patch. Rejection publishes a "Context patch rejected" notification.

---

## Memory Health States & Recovery

The short-term memory system tracks health and supports graceful degradation.

### MemoryHealth Enum

```python
class MemoryHealth(str, Enum):
    HEALTHY = "healthy"       # Summarization working normally
    RETRY = "retry"           # Attempting recovery with backoff
    DEGRADED = "degraded"     # Fallback to truncation only
    RECOVERING = "recovering" # Recovering from backlog
```

### Health Transitions

```
HEALTHY → RETRY (on summarizer failure)
RETRY → DEGRADED (after max retries exceeded)
DEGRADED → RECOVERING (on periodic recovery attempt)
RECOVERING → HEALTHY (on successful backlog summarization)
RECOVERING → DEGRADED (on recovery failure)
```

### Memory Strategies

| Strategy | Description | When to Use |
|----------|-------------|-------------|
| `"none"` | No short-term memory (default) | Stateless agents |
| `"truncation"` | Keep N recent turns, discard older | Simple use cases |
| `"rolling_summary"` | Summarize old turns + keep recent | Long conversations |

### MemoryBudget Configuration

```python
@dataclass(slots=True)
class MemoryBudget:
    full_zone_turns: int = 5          # Recent turns kept in full
    summary_max_tokens: int = 1000    # Summary size limit
    total_max_tokens: int = 10000     # Total memory budget
    overflow_policy: Literal[
        "truncate_summary",           # Drop oldest from summary
        "truncate_oldest",            # Drop oldest turn
        "error"                       # Raise MemoryBudgetExceeded from add_turn
    ] = "truncate_oldest"
```

Under `ReactPlanner`, a `MemoryBudgetExceeded` from `"error"` does not reach the caller: it is caught and logged (`memory_add_turn_failed`), and that run's memory state is not persisted.

### MemoryIsolation (Multi-Tenant)

```python
@dataclass(slots=True)
class MemoryIsolation:
    tenant_key: str = "tenant_id"     # Dot-path in tool_context
    user_key: str = "user_id"
    session_key: str = "session_id"
    require_explicit_key: bool = True  # No session id -> no memory for the run
```

Memory key format: `{tenant_id}:{user_id}:{session_id}`. Only the session id is required: a missing tenant or user defaults to `default` / `anonymous`. With `require_explicit_key=False`, a run without a session id uses one ephemeral `default:anonymous:<random>` key per planner.

### StateStore Implications

When implementing `save_memory_state`/`load_memory_state`:
- Store the `health` field to track state across sessions
- Preserve `pending` and `backlog` lists for recovery
- Store `config_snapshot` for debugging

---

## Artifact Store

### ArtifactStore Protocol

```python
@runtime_checkable
class ArtifactStore(Protocol):
    async def put_bytes(
        self,
        data: bytes,
        *,
        mime_type: str | None = None,
        filename: str | None = None,
        namespace: str | None = None,
        scope: ArtifactScope | None = None,
        meta: dict[str, Any] | None = None,
    ) -> ArtifactRef: ...

    async def put_text(
        self,
        text: str,
        *,
        mime_type: str = "text/plain",
        filename: str | None = None,
        namespace: str | None = None,
        scope: ArtifactScope | None = None,
        meta: dict[str, Any] | None = None,
    ) -> ArtifactRef: ...

    async def get(self, artifact_id: str) -> bytes | None: ...
    async def get_ref(self, artifact_id: str) -> ArtifactRef | None: ...
    async def delete(self, artifact_id: str) -> bool: ...
    async def exists(self, artifact_id: str) -> bool: ...

    async def list(self, *, scope: ArtifactScope | None = None) -> list[ArtifactRef]:
        """List artifacts matching the given scope filter.
        None fields in scope = don't filter on that dimension.
        If scope is None, returns all artifacts.
        """
        ...
```

The protocol is `@runtime_checkable`, and discovery uses `isinstance(...)`: a store missing any of the seven methods (including `list`) is not discovered, and the planner silently falls back to `NoOpArtifactStore`.

### ArtifactRef (Compact Reference)

Only `ArtifactRef` objects (~100 bytes) are passed to LLMs, never raw binary data:

```python
class ArtifactRef(BaseModel):
    id: str                            # Typically "{namespace}_{sha256[:12]}", but not required
    mime_type: str | None = None       # Content type (best-effort)
    size_bytes: int | None = None      # Original content size (best-effort)
    filename: str | None = None        # Suggested download name
    sha256: str | None = None          # Full content hash (best-effort)
    scope: ArtifactScope | None = None # Access control metadata
    namespace: str | None = None       # Grouping, e.g. the tool or ToolNode name
    source: dict[str, Any] = Field(default_factory=dict)  # Tool name, warnings, preview
```

### ArtifactScope (Access Control)

```python
class ArtifactScope(BaseModel):
    tenant_id: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    trace_id: str | None = None
```

**Note:** Scope is stored by `ArtifactStore` and **enforced at two layers**: the `ScopedArtifacts` facade (application-level, for tool/agent developers via `ctx.artifacts`) and your HTTP layer (for API consumers; PenguiFlow does not ship one). See [ScopedArtifacts Facade](#scopedartifacts-facade) below.

### ScopedArtifacts Facade

Tool and agent developers interact with artifacts through `ctx.artifacts`, an instance of `ScopedArtifacts`. This is the **porcelain** API, and it is what ToolNode's extraction pipeline uses too (`ctx.artifacts.upload(...)`). Underneath, `ctx._artifacts` is the raw `ArtifactStore` (the **plumbing** API), reserved for framework internals. In a planner run it is a proxy that emits an `artifact_stored` planner event for every write.

The planner builds the facade's scope from `tool_context["tenant_id"]`, `["user_id"]`, `["session_id"]` and `["trace_id"]`.

```python
class ScopedArtifacts:
    """Scoped facade over ArtifactStore for tool developers.

    Automatically injects tenant_id/user_id/session_id/trace_id on writes
    and enforces scope on reads. Immutable after construction.
    """

    def __init__(
        self,
        store: ArtifactStore,
        *,
        tenant_id: str | None,
        user_id: str | None,
        session_id: str | None,
        trace_id: str | None,
    ) -> None: ...

    @property
    def scope(self) -> ArtifactScope: ...

    async def upload(self, data: bytes | str, *, mime_type=None, filename=None, namespace=None, meta=None) -> ArtifactRef: ...
    async def download(self, artifact_id: str) -> bytes | None: ...
    async def get_metadata(self, artifact_id: str) -> ArtifactRef | None: ...
    async def list(self) -> list[ArtifactRef]: ...
    async def exists(self, artifact_id: str) -> bool: ...
    async def delete(self, artifact_id: str) -> bool: ...
```

#### Scope semantics

| Operation | Scope behavior |
|---|---|
| `upload()` | Injects **full scope** (tenant + user + session + trace) into the stored artifact; `str` data goes to `put_text`, `bytes` to `put_bytes` |
| `download()` / `get_metadata()` / `exists()` / `delete()` | Checks **tenant + user + session only** (not trace) — so reads return artifacts across traces within the same session. An artifact without scope passes |
| `list()` | Delegates to `ArtifactStore.list(scope=...)` with **tenant + user + session** (not trace) |

#### Immutability

`ScopedArtifacts` is immutable after construction — `__setattr__` raises `AttributeError`. The `_store`, `_scope`, and `_read_scope` attributes are set via `object.__setattr__` in `__init__` only.

### ArtifactRetentionConfig

```python
class ArtifactRetentionConfig(BaseModel):
    ttl_seconds: int = 3600                          # 1-hour default expiration
    max_artifact_bytes: int = 50 * 1024 * 1024       # 50 MiB per artifact
    max_session_bytes: int = 500 * 1024 * 1024       # 500 MiB per session
    max_trace_bytes: int = 100 * 1024 * 1024         # 100 MiB per trace
    max_artifacts_per_trace: int = 100
    max_artifacts_per_session: int = 1000
    cleanup_strategy: Literal["lru", "fifo", "none"] = "lru"
```

The in-memory reference store enforces the TTL, the per-artifact size (oversized writes raise `ValueError`) and the per-session count and byte limits; it does not enforce the per-trace limits.

### StateStore Integration

The `artifact_store` property provides access to the underlying store:

```python
@property
def artifact_store(self) -> ArtifactStore | None:
    return self._artifact_store
```

Discovery helper:

```python
from penguiflow.artifacts import discover_artifact_store

state_store = ...  # your StateStore
store = discover_artifact_store(state_store)  # Returns ArtifactStore or None
```

`ReactPlanner` picks its artifact store in this order: the explicit `artifact_store=` argument, then `discover_artifact_store(state_store)` (an `artifact_store` attribute that passes `isinstance(..., ArtifactStore)`, or the state store itself), then `NoOpArtifactStore`.

---

## Integration Points

### 1. A2A server (`A2AService`)

**Location:** `penguiflow_a2a/core.py`

```python
from penguiflow_a2a import A2AConfig, A2AService

service = A2AService(
    planner,
    agent_card=card,
    config=A2AConfig(agent_url="https://agents.example.com/support"),
    state_store=your_store,  # optional; defaults to the planner's state_store
)
```

**Integration Behavior:**
- With `config.agent_url` set, each A2A task run (the first run and each resume) calls `save_remote_binding(RemoteBinding(trace_id=task_id, context_id=context_id, task_id=task_id, agent_url=agent_url))` before the planner starts.
- Binding persistence is best-effort: an exception from `save_remote_binding` is caught and logged (`a2a_remote_binding_save_failed`), and the task still runs.
- A2A paused tasks resume only in the same process: `A2AService` keeps the task-to-resume-token map in memory, so the follow-up message for an `input-required` task must reach the process that paused it, even when the planner's pause record is in the shared store.

### 2. A2A client (`A2AAgentToolset`)

**Location:** `penguiflow_a2a/planner_tools.py`

**Integration Behavior:**
- The store is `tool_context["state_store"]`, falling back to the planner's `planner.state_store`.
- Bindings need `tool_context["session_id"]` (the router session id); tenant comes from `tool_context["tenant_id"]` (or `"tenant"`), user from `tool_context["user_id"]`, and the binding's `trace_id` from `tool_context["trace_id"]` (or the tool call id).
- Before each call it calls `find_binding(...)`: the stored `context_id` is reused, and the stored `task_id` only when `metadata` has `awaiting_remote_input` or `awaiting_remote_auth`.
- After each call it saves a binding with `is_terminal=False` and `metadata={"source": "a2a_agent_toolset", ...}` (task-mode calls add `remote_task_state` and `remote_task_terminal`).
- Errors from `find_binding` or `save_remote_binding` propagate to the tool call.
- `A2ARouterToolset` delegates through an `A2AAgentToolset`, so it uses the same bindings.

### 3. ReactPlanner

**Location:** `penguiflow/planner/react.py`

The planner keeps the store as `planner.state_store`.

```python
from penguiflow.planner import ReactPlanner
from penguiflow.planner.memory import ShortTermMemoryConfig

planner = ReactPlanner(
    llm_client=client,
    catalog=tools,
    state_store=your_store,  # Enables pause/resume and memory
    pause_enabled=True,      # The default; required for OAuth/HITL
    short_term_memory=ShortTermMemoryConfig(strategy="truncation"),  # Enables memory persistence
)
```

**Integration Behavior:**
- `save_planner_state()` called when planner pauses
- `load_planner_state()` called during `planner.resume(token)` when the token is not in that instance's memory
- `save_memory_state()` called after each run that finishes (if memory configured)
- `load_memory_state()` called at start of each run and resume (if memory configured)
- `save_trajectory()` / `save_planner_event()` called in background tasks after each run and resume (they need `trace_id`, and trajectories also `session_id`, in `tool_context`)
- Methods detected via `getattr()` - missing methods are skipped (planner continues without that capability)
- Persistence failures on these paths are **logged**; pause records always have an in-memory fallback, but resume on another instance fails (`KeyError`) if the pause token cannot be loaded

### 4. StreamingSession / SessionManager

**Location:** `penguiflow/sessions/session.py`

**Store selection:**
- a store with `save_task` is used directly as the session store, so it must implement `save_task`, `save_update`, `save_steering`, `list_tasks`, `list_updates` and `list_steering`;
- otherwise a store with `save_event` and `load_history` is wrapped in `StateStoreSessionAdapter`, which writes `session.*` pseudo-events (see [Event Types](#event-types-kind-field));
- no store means `InMemorySessionStateStore`.

**Integration Behavior:**
- `save_task()` called on task lifecycle transitions (awaited)
- `save_update()` called for every published update (as a background task)
- `save_steering()` called for each accepted steering event and for each cascaded cancel (awaited)
- `list_tasks()` used to hydrate a session (`SessionManager.get_or_create` calls `hydrate()`)
- `list_updates()` used by `StreamingSession.list_updates(...)` for polling and by `subscribe(since_id=...)` to replay history before live updates
- `list_steering()` is not called by the session layer; it serves your own readers

### 5. Session Hydration Flow

When a session is created or resumed:

```python
async def hydrate(self) -> None:
    """Load persisted tasks from the state store into the registry."""
    if self._hydrated:
        return
    tasks = await self.state_store.list_tasks(self.session_id)
    if tasks:
        await self._registry.seed_tasks(list(tasks))
        foreground = [task for task in tasks if task.task_type == TaskType.FOREGROUND]
        if foreground:
            # The last foreground task returned by list_tasks, whatever its status
            self._foreground_task_id = foreground[-1].task_id
    self._hydrated = True
```

Hydration restores task records only; it does not restart running work.

### 6. Admin CLI

**Location:** `penguiflow/admin.py`

```bash
# View trace history
penguiflow-admin history --state-store mypackage.stores:create_store trace_abc123

# Replay trace events
penguiflow-admin replay --state-store mypackage.stores:create_store trace_abc123
```

**Factory Specification:**
- Format: `"module.path:callable_name"`
- Callable must return a StateStore instance (sync or async)
- Example: `"myapp.infrastructure.stores:create_postgres_store"`
- `load_history()` errors propagate to the CLI, which prints them and exits with status 1
- Session pseudo-events live under `session:<session_id>`, so `penguiflow-admin history session:<session_id>` shows a session's task history when it is persisted through `StateStoreSessionAdapter`

---

## Distributed Deployment

### Stateless Worker Pool (Recommended)

For horizontal scaling, build a planner per job from a factory and share one durable store:

```python
from __future__ import annotations

from penguiflow.planner import PlannerPause, ReactPlanner
from penguiflow.state import StateStore


async def worker_loop(job_queue: JobQueue, state_store: StateStore) -> None:
    while True:
        job = await job_queue.dequeue()
        planner = ReactPlanner(llm_client=client, catalog=tools, state_store=state_store)
        try:
            result = await planner.run(
                job.query,
                tool_context={
                    "tenant_id": job.tenant_id,
                    "user_id": job.user_id,
                    "session_id": job.session_id,
                    "trace_id": job.trace_id,
                },
            )
            if isinstance(result, PlannerPause):
                await job_queue.park(job.id, resume_token=result.resume_token)  # pause state is in the store
            else:
                await job_queue.ack(job.id, result=result.payload)
        except Exception as e:
            await job_queue.nack(job.id, error=str(e))
```

**Benefits:**
- Horizontal scalability (10+ workers)
- Easy failure isolation
- No shared state between jobs
- Paused runs resume on any worker

### Sessions-Backed Service (Advanced)

For live UIs, background tasks and steering, share a `SessionManager(state_store=...)`: sessions persist tasks, updates and steering through `save_task` / `save_update` / `save_steering` (or as `session.*` pseudo-events via `StateStoreSessionAdapter` when the store only implements the core protocol).

### Multi-Worker Consistency Model

| Data Type | Consistency | Notes |
|-----------|-------------|-------|
| Events (`save_event`) | Eventual | Events may arrive out of order; session pseudo-events are read back, so prefer read-your-writes |
| Remote bindings | Eventual | Upsert semantics handle conflicts |
| Planner state (`pause/resume`) | Strong | Must be consistent for distributed resume |
| Memory state | Eventual | Last-write-wins acceptable |
| Task state | Strong | For accurate UI status |
| Steering events | Strong | For real-time interventions |

### Trace-Based Causality

All distributed work is associated via `trace_id`:

```python
from penguiflow.planner import PlannerFinish, PlannerPause, ReactPlanner


async def worker_a(planner: ReactPlanner, query: str) -> PlannerFinish | PlannerPause:
    # Process the initial request
    return await planner.run(query, tool_context={"trace_id": "trace_abc123", "session_id": "s1"})


async def worker_b(planner: ReactPlanner, token: str) -> PlannerFinish | PlannerPause:
    # Resume after the OAuth callback. With no tool_context argument, resume
    # reuses the stored tool_context (and its trace_id). Passing tool_context=
    # replaces it, so include trace_id and session_id again.
    return await planner.resume(token, user_input="oauth_completed")
```

### Cascade Cancellation

Parent task cancellation propagates to children. The session walks the whole task tree below the cancelled task, skips children whose snapshot says `propagate_on_cancel="isolate"` and children that already finished, and for each remaining child:

```python
from penguiflow.state import SteeringEvent, SteeringEventType
from penguiflow.steering import sanitize_steering_event


async def cancel_child(self, child_id: str, parent_task_id: str, reason: str) -> None:
    propagated = sanitize_steering_event(
        SteeringEvent(
            session_id=self.session_id,
            task_id=child_id,
            event_type=SteeringEventType.CANCEL,
            payload={
                "reason": reason,
                "confirmed": True,
                "propagated_from": parent_task_id,
            },
            source="system",
        ),
        max_payload_bytes=self._limits.max_steering_payload_bytes,
    )
    await self.state_store.save_steering(propagated)
```

It then pushes the event to the child's steering inbox, marks the child `CANCELLED` (persisted through `save_task`), and cancels its asyncio task.

---

## Method Contracts & Semantics

### Idempotency Requirements

| Method | Idempotent? | Reason |
|--------|-------------|--------|
| `save_event` | YES | Retries can emit duplicate events |
| `load_history` | YES | Read-only operation |
| `save_remote_binding` | YES | Same binding is saved after every call/run |
| `find_binding` | YES | Read-only operation |
| `list_bindings` | YES | Read-only operation |
| `mark_binding_terminal` | YES | Marking twice (or a missing row) is a no-op |
| `save_planner_state` | YES | Pause may be saved redundantly |
| `load_planner_state` | NO | Should consume/delete token on read |
| `save_memory_state` | YES | Same key updated repeatedly |
| `load_memory_state` | YES | Read-only operation |
| `save_task` | YES | Task state is updated repeatedly |
| `list_tasks` | YES | Read-only operation |
| `save_update` | YES | Retries can re-emit the same update_id |
| `list_updates` | YES | Read-only operation |
| `save_steering` | YES | Steering events can be retried |
| `list_steering` | YES | Read-only operation |
| `save_trajectory` | YES | Trace can be saved multiple times |
| `get_trajectory` | YES | Read-only operation |
| `list_traces` | YES | Read-only operation |
| `save_planner_event` | YES | Retries can duplicate planner events |
| `list_planner_events` | YES | Read-only operation |
| `artifact_store` | N/A | Property (capability discovery) |

### Consistency Model

- **Eventual consistency is acceptable** for event storage
- **Strong consistency required** for planner state (pause/resume)
- **Eventual consistency acceptable** for memory state (last-write-wins)
- **Strong consistency recommended** for task/steering reads in interactive UIs

### Ordering Guarantees

- `load_history()` MUST return events in `ts` ascending order
- For same-`ts` events, secondary ordering by insertion ID is recommended
- No ordering guarantees for `list_bindings()`; `find_binding()` returns the newest matching binding
- `list_updates()` / `list_steering()` SHOULD use stable ordering and support cursor semantics via `since_id`
- `list_planner_events()` SHOULD preserve emission order

---

## Reference Implementations

PenguiFlow ships a complete in-memory reference, `penguiflow.state.in_memory.InMemoryStateStore`, which implements every capability in this document (and the legacy `save_task_update` / `list_task_updates` aliases). Use it in tests and as the behavioural reference.

### Minimal In-Memory (Testing)

```python
from collections import defaultdict

from penguiflow.state import RemoteBinding, StoredEvent


class MinimalStateStore:
    def __init__(self) -> None:
        self._events: dict[str, list[StoredEvent]] = defaultdict(list)
        self._bindings: list[RemoteBinding] = []
        self._planner_state: dict[str, dict] = {}
        self._memory_state: dict[str, dict] = {}

    async def save_event(self, event: StoredEvent) -> None:
        key = event.trace_id or "__global__"
        self._events[key].append(event)

    async def load_history(self, trace_id: str) -> list[StoredEvent]:
        return sorted(self._events.get(trace_id, []), key=lambda e: e.ts)

    async def save_remote_binding(self, binding: RemoteBinding) -> None:
        self._bindings.append(binding)

    # Optional methods
    async def save_planner_state(self, token: str, payload: dict) -> None:
        self._planner_state[token] = payload

    async def load_planner_state(self, token: str) -> dict | None:
        return self._planner_state.pop(token, None)

    async def save_memory_state(self, key: str, state: dict) -> None:
        self._memory_state[key] = state

    async def load_memory_state(self, key: str) -> dict | None:
        return self._memory_state.get(key)
```

### PostgreSQL (Production)

See the full implementation in the **StateStore Implementation Guide for Production** (`docs/tools/statestore-guide.md` in the PenguiFlow repo).

Key considerations:
- Use connection pooling (`asyncpg.Pool`)
- Add indexes on `trace_id` and `ts`
- Set TTL on pause records
- Consider partitioning for high-volume event tables

### Redis + PostgreSQL (Hybrid)

```python
import json
from collections.abc import Sequence

import asyncpg
from redis.asyncio import Redis

from penguiflow.state import RemoteBinding, StoredEvent


class HybridStateStore:
    """Redis for hot paths (pause/resume), PostgreSQL for durability."""

    def __init__(self, redis: Redis, pg_pool: asyncpg.Pool) -> None:
        self._redis = redis
        self._pg = pg_pool

    # Events and bindings go to PostgreSQL (durability)
    async def save_event(self, event: StoredEvent) -> None:
        ...  # same as the PostgreSQL example

    async def load_history(self, trace_id: str) -> Sequence[StoredEvent]:
        ...  # same as the PostgreSQL example

    async def save_remote_binding(self, binding: RemoteBinding) -> None:
        ...  # same as the PostgreSQL example

    # Pause/resume uses Redis (speed)
    async def save_planner_state(self, token: str, payload: dict) -> None:
        await self._redis.setex(f"pause:{token}", 3600, json.dumps(payload))

    async def load_planner_state(self, token: str) -> dict | None:
        data = await self._redis.getdel(f"pause:{token}")
        return json.loads(data) if data else None

    # Memory can use either (preference: Redis for speed)
    async def save_memory_state(self, key: str, state: dict) -> None:
        await self._redis.set(f"memory:{key}", json.dumps(state))

    async def load_memory_state(self, key: str) -> dict | None:
        data = await self._redis.get(f"memory:{key}")
        return json.loads(data) if data else None
```

---

## Compatibility Adapters

PenguiFlow provides compatibility helpers that bridge legacy method names during migration. The library itself calls the new names directly; these helpers are for your own code.

### Legacy Method Mappings

| New Method | Legacy Alternative | Helper |
|------------|-------------------|---------|
| `save_update()` | `save_task_update()` | `save_update_compat` |
| `list_updates()` | `list_task_updates()` | `list_updates_compat` |

The session layer calls `save_update` / `list_updates` directly, so a session store must implement the new names.

### Using Compatibility Functions

```python
from penguiflow.planner import PlannerEvent
from penguiflow.state import RemoteBinding, StateStore, StateUpdate
from penguiflow.state.adapters import (
    list_planner_events_compat,
    list_updates_compat,
    maybe_save_remote_binding,
    save_planner_event_compat,
    save_update_compat,
)


async def use_compat(
    store: StateStore,
    update: StateUpdate,
    event: PlannerEvent,
    binding: RemoteBinding,
    cursor: str | None,
) -> None:
    # These try new method name first, fall back to legacy (TypeError if neither exists)
    await save_update_compat(store, update)
    updates = await list_updates_compat(store, update.session_id, since_id=cursor)

    # Planner events have no legacy name; these raise TypeError when the method is missing
    await save_planner_event_compat(store, "trace-1", event)
    events = await list_planner_events_compat(store, "trace-1")

    # Safe None-guard for optional stores
    await maybe_save_remote_binding(store, binding)  # No-op if store is None
    print(len(updates), len(events))
```

### Implementing Both Interfaces

For gradual migration, implement both method names:

```python
class MyStateStore:
    # New interface
    async def save_update(self, update: StateUpdate) -> None:
        await self._persist_update(update)

    # Legacy interface (deprecated)
    async def save_task_update(self, update: StateUpdate) -> None:
        await self.save_update(update)  # Delegate to new method
```

---

## Telemetry Integration

### TaskTelemetrySink Protocol

```python
class TaskTelemetrySink(Protocol):
    async def emit(self, event: TaskTelemetryEvent) -> None: ...
```

### TaskTelemetryEvent

```python
class TaskTelemetryEvent(BaseModel):
    event_type: Literal[
        "task_spawned",
        "task_completed",
        "task_failed",
        "task_cancelled",
        "task_group_completed",
        "task_group_failed",
    ]
    outcome: Literal["spawned", "completed", "failed", "cancelled"]
    session_id: str
    task_id: str
    parent_task_id: str | None = None
    trace_id: str | None = None
    task_type: TaskType
    status: TaskStatus
    mode: Literal["foreground", "subagent", "job"] | None = None
    spawn_reason: str | None = None
    duration_ms: float | None = None
    created_at_s: float = Field(default_factory=time.time)
    extra: dict[str, Any] = Field(default_factory=dict)
```

### Built-in Implementations

```python
from penguiflow.sessions.telemetry import (
    LoggingTaskTelemetrySink,  # Routes to Python logger
    NoOpTaskTelemetrySink,  # Silent (default)
)
```

### Custom Telemetry Sink

```python
from prometheus_client import CollectorRegistry, Counter

from penguiflow.sessions import TaskTelemetryEvent


class PrometheusTaskSink:
    def __init__(self, registry: CollectorRegistry) -> None:
        self._counter = Counter(
            "penguiflow_tasks_total",
            "Total tasks by outcome",
            ["outcome", "task_type"],
            registry=registry,
        )

    async def emit(self, event: TaskTelemetryEvent) -> None:
        self._counter.labels(
            outcome=event.outcome,
            task_type=event.task_type.value,
        ).inc()
```

### Integration with Session

```python
from penguiflow.sessions import SessionManager, StreamingSession

session = StreamingSession(
    "session-123",
    state_store=your_store,
    telemetry_sink=PrometheusTaskSink(registry),
)

# Or once for every session a manager creates
manager = SessionManager(state_store=your_store, telemetry_sink=PrometheusTaskSink(registry))
```

### Telemetry vs. StateStore

| Aspect | TaskTelemetrySink | StateStore |
|--------|-------------------|------------|
| Purpose | Observability/monitoring | Durability/auditability |
| Emission | Async fire-and-forget | Persistent storage |
| Data | High-level event semantics | Full task/update payloads |
| Scope | Task lifecycle only | Complete execution history |
| Use Case | Metrics, alerts, dashboards | Recovery, replay, audit |

---

## Database Schema Recommendations

### PostgreSQL Schema

```sql
-- Events table (required)
CREATE TABLE flow_events (
    id BIGSERIAL PRIMARY KEY,
    -- Recommended: normalise trace_id=None ("global events") to a sentinel like '__global__'
    -- so this column can be NOT NULL and participate in uniqueness constraints.
    trace_id VARCHAR(128) NOT NULL,
    ts DOUBLE PRECISION NOT NULL,
    kind VARCHAR(64) NOT NULL,
    node_name VARCHAR(128),
    node_id VARCHAR(128),
    -- sha256 of canonical JSON of {trace_id, ts, kind, node_name, node_id, payload}
    -- Used to make save_event() idempotent without requiring a first-class event_id.
    event_fp VARCHAR(64) NOT NULL,
    payload JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (trace_id, event_fp)
);

CREATE INDEX idx_events_trace_ts ON flow_events(trace_id, ts, id);
CREATE INDEX idx_events_kind ON flow_events(kind);

-- Remote bindings (A2A server tasks and A2AAgentToolset continuity)
CREATE TABLE remote_bindings (
    id BIGSERIAL PRIMARY KEY,
    trace_id VARCHAR(128) NOT NULL,
    context_id VARCHAR(128),
    task_id VARCHAR(128) NOT NULL,
    agent_url TEXT NOT NULL,
    router_session_id VARCHAR(128),
    remote_skill VARCHAR(128),
    tenant_id VARCHAR(128),
    user_id VARCHAR(128),
    last_remote_task_id VARCHAR(128),
    is_terminal BOOLEAN NOT NULL DEFAULT FALSE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_remote_bindings_identity
ON remote_bindings (trace_id, context_id, task_id) NULLS NOT DISTINCT;

-- If your database does not support NULLS NOT DISTINCT, use a generated
-- context_id_key = COALESCE(context_id, '') column for the unique index instead.

CREATE INDEX idx_remote_bindings_active_conversation
ON remote_bindings (router_session_id, agent_url, remote_skill, tenant_id, user_id, updated_at DESC)
WHERE router_session_id IS NOT NULL
  AND remote_skill IS NOT NULL
  AND is_terminal = FALSE;

-- Planner pause state
CREATE TABLE planner_pauses (
    token VARCHAR(128) PRIMARY KEY,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_pauses_expires ON planner_pauses(expires_at);

-- Memory state
CREATE TABLE memory_states (
    key VARCHAR(256) PRIMARY KEY,
    state JSONB NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Task state
CREATE TABLE task_states (
    task_id VARCHAR(64) PRIMARY KEY,
    session_id VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    task_type VARCHAR(32) NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    context_snapshot JSONB NOT NULL,
    trace_id VARCHAR(64),
    result JSONB,
    error TEXT,
    description TEXT,
    progress JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_tasks_session ON task_states(session_id);
CREATE INDEX idx_tasks_status ON task_states(session_id, status);

-- State updates
CREATE TABLE state_updates (
    id BIGSERIAL PRIMARY KEY,
    session_id VARCHAR(64) NOT NULL,
    task_id VARCHAR(64) NOT NULL,
    trace_id VARCHAR(64),
    update_id VARCHAR(64) NOT NULL UNIQUE,
    update_type VARCHAR(32) NOT NULL,
    content JSONB,
    step_index INTEGER,
    total_steps INTEGER,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_updates_session ON state_updates(session_id);
CREATE INDEX idx_updates_task ON state_updates(session_id, task_id);
CREATE INDEX idx_updates_cursor ON state_updates(session_id, id);

-- Steering events
CREATE TABLE steering_events (
    id BIGSERIAL PRIMARY KEY,
    session_id VARCHAR(64) NOT NULL,
    task_id VARCHAR(64) NOT NULL,
    event_id VARCHAR(64) NOT NULL UNIQUE,
    event_type VARCHAR(32) NOT NULL,
    payload JSONB,
    trace_id VARCHAR(64),
    source VARCHAR(32) DEFAULT 'user',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_steering_session ON steering_events(session_id);
CREATE INDEX idx_steering_task ON steering_events(session_id, task_id);

-- Trajectories
CREATE TABLE trajectories (
    trace_id VARCHAR(64) NOT NULL,
    session_id VARCHAR(64) NOT NULL,
    trajectory JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (trace_id, session_id)
);

CREATE INDEX idx_trajectories_session ON trajectories(session_id, created_at DESC);

-- Planner events
CREATE TABLE planner_events (
    id BIGSERIAL PRIMARY KEY,
    trace_id VARCHAR(64) NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    ts DOUBLE PRECISION NOT NULL,
    trajectory_step INTEGER,
    thought TEXT,
    node_name VARCHAR(128),
    latency_ms DOUBLE PRECISION,
    token_estimate INTEGER,
    error TEXT,
    extra JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_planner_events_trace ON planner_events(trace_id, id);

-- Artifacts
CREATE TABLE artifacts (
    artifact_id VARCHAR(128) PRIMARY KEY,
    tenant_id VARCHAR(128),
    user_id VARCHAR(128),
    session_id VARCHAR(64),
    trace_id VARCHAR(64),
    namespace VARCHAR(128),
    mime_type VARCHAR(128),
    size_bytes BIGINT NOT NULL,
    filename VARCHAR(256),
    sha256 VARCHAR(64),
    source JSONB NOT NULL DEFAULT '{}'::jsonb,
    data BYTEA NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ
);

-- Supports list(scope=...) filters on tenant/user/session
CREATE INDEX idx_artifacts_scope ON artifacts(tenant_id, user_id, session_id);
CREATE INDEX idx_artifacts_expires ON artifacts(expires_at);

-- Cleanup jobs (run periodically)
-- DELETE FROM planner_pauses WHERE expires_at < NOW();
-- DELETE FROM artifacts WHERE expires_at < NOW();
```

### Redis Key Patterns

```
# Planner pause state
pause:{token}           -> JSON payload (TTL: 1 hour)

# Memory state
memory:{tenant_id}:{user_id}:{session_id}  -> JSON state (no TTL or long TTL)

# Event streams (optional, for real-time)
events:{trace_id}       -> Redis Stream

# Task state (optional, for fast lookups)
task:{session_id}:{task_id}  -> JSON TaskState

# Steering queue (optional, for pub/sub)
steering:{session_id}   -> Redis Stream
```

---

## Testing Your Implementation

### Required Test Cases

```python
import pytest
from your_module import YourStateStore

from penguiflow.state import RemoteBinding, StoredEvent


@pytest.fixture
async def store():
    store = YourStateStore()
    yield store
    await store.cleanup()  # If applicable

# 1. Event persistence
async def test_save_and_load_events(store):
    event = StoredEvent(
        trace_id="test-trace",
        ts=1702857600.0,
        kind="session.task",
        node_name=None,
        node_id=None,
        payload={"task_id": "task-1", "status": "RUNNING"},
    )
    await store.save_event(event)
    history = await store.load_history("test-trace")

    assert len(history) == 1
    assert history[0].trace_id == "test-trace"
    assert history[0].kind == "session.task"

# 2. Empty history for unknown trace
async def test_load_history_unknown_trace(store):
    history = await store.load_history("nonexistent")
    assert list(history) == []

# 3. Event ordering
async def test_events_ordered_by_timestamp(store):
    for i, ts in enumerate([3.0, 1.0, 2.0]):
        await store.save_event(StoredEvent(
            trace_id="order-test",
            ts=ts,
            kind="test",
            node_name=f"node_{i}",
            node_id=None,
            payload={},
        ))

    history = await store.load_history("order-test")
    timestamps = [e.ts for e in history]
    assert timestamps == [1.0, 2.0, 3.0]

# 4. Idempotent save
async def test_save_event_idempotent(store):
    event = StoredEvent(
        trace_id="idem-test",
        ts=1.0,
        kind="test",
        node_name="node",
        node_id="id",
        payload={},
    )
    await store.save_event(event)
    await store.save_event(event)  # Should not fail

    history = await store.load_history("idem-test")
    assert len(history) >= 1  # May be 1 or 2 depending on idempotency impl

# 5. Remote binding
async def test_save_remote_binding(store):
    binding = RemoteBinding(
        trace_id="binding-test",
        context_id="ctx",
        task_id="task-123",
        agent_url="http://worker:8080",
    )
    await store.save_remote_binding(binding)
    await store.save_remote_binding(binding)  # Upsert: should not fail

# 6. Conversation bindings (if implemented)
async def test_find_binding_scoped_and_terminal(store):
    if not hasattr(store, "find_binding"):
        pytest.skip("Conversation bindings not implemented")

    binding = RemoteBinding(
        trace_id="trace-1",
        context_id="ctx-1",
        task_id="task-1",
        agent_url="http://specialist:8080",
        router_session_id="router-1",
        remote_skill="research",
        tenant_id="acme",
        user_id="u1",
    )
    await store.save_remote_binding(binding)

    found = await store.find_binding(
        router_session_id="router-1",
        agent_url="http://specialist:8080",
        remote_skill="research",
        tenant_id="acme",
        user_id="u1",
    )
    assert found is not None
    assert found.context_id == "ctx-1"

    other_tenant = await store.find_binding(
        router_session_id="router-1",
        agent_url="http://specialist:8080",
        remote_skill="research",
        tenant_id="globex",
        user_id="u1",
    )
    assert other_tenant is None

    await store.mark_binding_terminal(trace_id="trace-1", context_id="ctx-1", task_id="task-1")
    await store.mark_binding_terminal(trace_id="trace-1", context_id="ctx-1", task_id="task-1")
    assert await store.find_binding(
        router_session_id="router-1",
        agent_url="http://specialist:8080",
        remote_skill="research",
        tenant_id="acme",
        user_id="u1",
    ) is None
    assert len(await store.list_bindings(router_session_id="router-1")) == 1

# 7. Planner state (if implemented)
async def test_planner_state_save_load(store):
    if not hasattr(store, "save_planner_state"):
        pytest.skip("Planner state not implemented")

    token = "test-token-123"
    payload = {
        "trajectory": {"version": 1, "steps": []},
        "reason": "await_input",
        "payload": {"example": True},
        "constraints": None,
        "tool_context": {"tenant_id": "test", "user_id": "test"},
    }

    await store.save_planner_state(token, payload)
    loaded = await store.load_planner_state(token)

    assert loaded == payload

# 8. Planner state consumed on load
async def test_planner_state_consumed(store):
    if not hasattr(store, "save_planner_state"):
        pytest.skip("Planner state not implemented")

    token = "consume-test"
    await store.save_planner_state(token, {"data": "value"})

    first_load = await store.load_planner_state(token)
    second_load = await store.load_planner_state(token)

    assert first_load == {"data": "value"}
    assert second_load is None  # Consumed / missing

# 9. Memory state (if implemented)
async def test_memory_state_roundtrip(store):
    if not hasattr(store, "save_memory_state"):
        pytest.skip("Memory state not implemented")

    key = "tenant:user:session"
    state = {"turn_history": [{"role": "user", "content": "hello"}]}

    await store.save_memory_state(key, state)
    loaded = await store.load_memory_state(key)

    assert loaded == state

# 10. Memory state returns None for unknown key
async def test_memory_state_unknown_key(store):
    if not hasattr(store, "load_memory_state"):
        pytest.skip("Memory state not implemented")

    result = await store.load_memory_state("nonexistent:key")
    assert result is None

# 11. Task state (if implemented)
async def test_task_state_roundtrip(store):
    if not hasattr(store, "save_task"):
        pytest.skip("Task persistence not implemented")

    from penguiflow.state import TaskContextSnapshot, TaskState, TaskStatus, TaskType

    snapshot = TaskContextSnapshot(
        session_id="test-session",
        task_id="test-task",
        context_version=1,
        context_hash="abc123",
    )
    task = TaskState(
        task_id="test-task",
        session_id="test-session",
        status=TaskStatus.PENDING,
        task_type=TaskType.BACKGROUND,
        priority=5,
        context_snapshot=snapshot,
        description="Test task",
    )

    await store.save_task(task)
    tasks = await store.list_tasks("test-session")

    assert len(tasks) == 1
    assert tasks[0].task_id == "test-task"
    assert tasks[0].priority == 5

# 12. State update with cursor (if implemented)
async def test_state_update_cursor(store):
    if not hasattr(store, "save_update"):
        pytest.skip("Update persistence not implemented")

    from penguiflow.state import StateUpdate, UpdateType

    updates = [
        StateUpdate(
            session_id="test-session",
            task_id="test-task",
            update_id=f"update-{i}",
            update_type=UpdateType.PROGRESS,
            content={"step": i},
        )
        for i in range(5)
    ]

    for update in updates:
        await store.save_update(update)

    # Get all updates
    all_updates = await store.list_updates("test-session")
    assert len(all_updates) == 5

    # Get updates after cursor
    partial = await store.list_updates("test-session", since_id="update-2")
    assert len(partial) == 2  # update-3, update-4

# 13. Steering event sanitization (if implemented)
async def test_steering_sanitized(store):
    if not hasattr(store, "save_steering"):
        pytest.skip("Steering persistence not implemented")

    from penguiflow.state import SteeringEvent, SteeringEventType

    event = SteeringEvent(
        session_id="test-session",
        task_id="test-task",
        event_type=SteeringEventType.USER_MESSAGE,
        payload={"text": "Hello"},
        source="user",
    )

    await store.save_steering(event)
    events = await store.list_steering("test-session")

    assert len(events) == 1
    assert events[0].source == "user"
```

### Integration Test with the Planner

Pause in one planner instance and resume from another that shares the store. `scripted_client()` stands for a deterministic `JSONLLMClient`, and `catalog_with_approval_tool` for a catalog with a tool that calls `ctx.pause(...)`:

```python
from penguiflow.planner import PlannerPause, ReactPlanner


async def test_statestore_pause_resume_across_planners():
    store = YourStateStore()
    first = ReactPlanner(llm_client=scripted_client(), catalog=catalog_with_approval_tool, state_store=store)
    paused = await first.run("needs approval", tool_context={"session_id": "s1"})
    assert isinstance(paused, PlannerPause)

    # A fresh planner (as on another worker) resumes from the store
    second = ReactPlanner(llm_client=scripted_client(), catalog=catalog_with_approval_tool, state_store=store)
    result = await second.resume(paused.resume_token, user_input="approved", tool_context={"session_id": "s1"})
    assert result.reason == "answer_complete"
```

---

## Error Handling

### What Is Best-Effort vs. What Can Propagate

PenguiFlow does **not** treat every StateStore call uniformly. In 7.0.0:

- **Session writes propagate.** `StreamingSession` awaits `save_task` and `save_steering` inline, so their errors reach the caller (`spawn_task`, `steer`, ...). With a core-only store, `StateStoreSessionAdapter` turns those writes into `save_event` calls, so `save_event` errors propagate the same way. `save_update` runs as a background task: its errors never reach the caller and only show up as asyncio "Task exception was never retrieved" logs.
- **A2A server bindings are best-effort**: `A2AService` catches and logs `save_remote_binding` failures (`a2a_remote_binding_save_failed`) and the task still runs.
- **A2A client bindings propagate**: `find_binding` / `save_remote_binding` errors from `A2AAgentToolset` fail the tool call.
- **Planner pause/resume**: a failing `save_planner_state` is logged (`state_store_save_failed`) and the in-memory record still works in the same planner instance; a failing `load_planner_state` is logged (`state_store_load_failed`) and raised as `KeyError`.
- **Memory, trajectories and planner events** are best-effort: failures are logged as warnings (`memory_hydrate_failed`, `memory_persist_failed`, background persistence warnings).
- **Explicit read APIs** (like `load_history(trace_id)` from `penguiflow-admin`) may propagate storage errors to the caller.

> **Gold standard rule:** For production-grade deployments, treat every StateStore method as **must not throw**.
> Degrade with safe defaults (`[]`/`None`), apply timeouts, and log/telemetry rather than raising.

### Recommended Error Handling in Your Implementation

```python
async def save_event(self, event: StoredEvent) -> None:
    try:
        await self._pool.execute(...)
    except asyncpg.PostgresConnectionError:
        # Gold standard: degrade gracefully.
        # - log/telemetry
        # - optionally enqueue to a dead-letter queue / local buffer
        # - return without raising (avoid breaking sessions/planner callers)
        logger.warning("statestore_unavailable", extra={"method": "save_event", "trace_id": event.trace_id})
        return
    except asyncpg.UniqueViolationError:
        # Idempotent - this is expected for duplicates
        return
    except Exception:
        logger.exception("statestore_save_failed", extra={"method": "save_event", "trace_id": event.trace_id})
        return
```

### Logging Recommendations

Use structured logging with these fields:
- `event`: `"statestore_save_failed"`, `"statestore_load_failed"`
- `trace_id`: The affected trace
- `method`: `"save_event"`, `"load_history"`, etc.
- `exception`: The exception repr

---

## Performance Considerations

### Connection Pooling

Always use connection pools for database backends:

```python
import asyncpg
import redis.asyncio as redis


async def make_pools(dsn: str, url: str) -> tuple[asyncpg.Pool, redis.ConnectionPool]:
    pg_pool = await asyncpg.create_pool(
        dsn,
        min_size=5,
        max_size=20,
        command_timeout=5.0,
    )
    redis_pool = redis.ConnectionPool.from_url(url, max_connections=20)
    return pg_pool, redis_pool
```

### Batching (High-Volume Scenarios)

For high-throughput deployments, consider batching event writes. `StateStoreSessionAdapter` reads session events back through `load_history`, so flush before those reads (or keep session traces out of the batch):

```python
import asyncio

from penguiflow.state import StateStore, StoredEvent


class BatchingStateStore:
    """Wraps a store; load_history and save_remote_binding delegate unchanged."""

    def __init__(self, delegate: StateStore, batch_size: int = 100) -> None:
        self._delegate = delegate
        self._batch: list[StoredEvent] = []
        self._batch_size = batch_size
        self._lock = asyncio.Lock()

    async def save_event(self, event: StoredEvent) -> None:
        async with self._lock:
            self._batch.append(event)
            if len(self._batch) >= self._batch_size:
                await self._flush()

    async def _flush(self) -> None:
        if not self._batch:
            return
        batch, self._batch = self._batch, []
        # Bulk insert (out-of-protocol extension).
        # If you add a bulk method, feature-detect it via hasattr() and fall back to per-event writes.
        bulk = getattr(self._delegate, "save_events_bulk", None)
        if bulk is not None:
            await bulk(batch)
        else:
            for item in batch:
                await self._delegate.save_event(item)
```

### Indexing Strategy

Essential indexes:
- `(trace_id, ts)` - for `load_history()`
- `(session_id)` - for `list_tasks()`, `list_updates()`
- `(session_id, id)` - for cursor-based pagination
- `(router_session_id, agent_url, remote_skill, ...)` partial index on active bindings - for `find_binding()`
- `(expires_at)` - for pause/artifact cleanup

Avoid:
- Indexes on `payload` JSONB (expensive to maintain)
- Too many indexes on write-heavy tables

### Timeout Configuration

Set reasonable timeouts to prevent blocking:

```python
async def save_event(self, event: StoredEvent) -> None:
    async with asyncio.timeout(5.0):  # 5 second timeout
        await self._pool.execute(...)
```

---

## Implementation Checklist

Use this checklist to verify your implementation is complete:

### Required Methods

- [ ] `save_event(event: StoredEvent) -> None` implemented
- [ ] `load_history(trace_id: str) -> Sequence[StoredEvent]` implemented
- [ ] `save_remote_binding(binding: RemoteBinding) -> None` implemented

### Method Contracts

- [ ] `save_event` is idempotent
- [ ] `load_history` returns events ordered by timestamp
- [ ] `load_history` returns empty list for unknown trace (not exception)
- [ ] `save_remote_binding` supports upsert semantics on `(trace_id, context_id, task_id)`
- [ ] `save_remote_binding` preserves additive A2A router-continuity fields when present

### Optional Methods (if applicable)

- [ ] `find_binding(router_session_id, agent_url, remote_skill, tenant_id=None, user_id=None)` implemented for A2A router continuity
- [ ] `find_binding` returns only non-terminal bindings with non-null `router_session_id` and `remote_skill`
- [ ] `find_binding` scopes by tenant/user without leakage
- [ ] `list_bindings(router_session_id=...)` implemented for A2A inspection/debugging
- [ ] `mark_binding_terminal(trace_id, context_id, task_id)` implemented and idempotent
- [ ] `save_planner_state(token, payload)` implemented
- [ ] `load_planner_state(token)` implemented
- [ ] `load_planner_state` returns `None` for missing/expired tokens
- [ ] `load_planner_state` consumes token (one-time use)
- [ ] `save_memory_state(key, state)` implemented
- [ ] `load_memory_state(key)` implemented
- [ ] `load_memory_state` returns `None` for missing keys

### Task & Session Methods (if applicable)

- [ ] `save_task(state: TaskState)` implemented
- [ ] `list_tasks(session_id)` implemented
- [ ] `save_update(update: StateUpdate)` implemented
- [ ] `list_updates(session_id, task_id, since_id, limit)` implemented
- [ ] `save_steering(event: SteeringEvent)` implemented
- [ ] `list_steering(session_id, task_id, since_id, limit)` implemented

### Trajectory & Planner Events (if applicable)

- [ ] `save_trajectory(trace_id, session_id, trajectory)` implemented
- [ ] `get_trajectory(trace_id, session_id)` implemented
- [ ] `list_traces(session_id, limit)` implemented
- [ ] `save_planner_event(trace_id, event)` implemented
- [ ] `list_planner_events(trace_id)` implemented

### Artifact Store (if applicable)

- [ ] `artifact_store` property returns `ArtifactStore` or `None`
- [ ] All seven `ArtifactStore` methods implemented, including `list(scope=...)` (otherwise discovery fails)
- [ ] Retention config honored (TTL, max sizes)
- [ ] Session-scoped access patterns supported

### Production Readiness

- [ ] Connection pooling configured
- [ ] Timeouts set on all database operations
- [ ] Proper error handling (log, don't crash)
- [ ] TTL/expiration on pause records
- [ ] Cleanup job for expired data
- [ ] Indexes on frequently queried columns
- [ ] Steering payloads sanitized before storage
- [ ] Unit tests passing
- [ ] Integration test with the planner (pause on one instance, resume on another) passing

### Distributed Deployment

- [ ] Idempotency verified for all write methods
- [ ] Cursor semantics work correctly for list methods
- [ ] Context versioning preserved in TaskContextSnapshot
- [ ] Session hydration tested
- [ ] Every run carries `tenant_id`, `user_id`, `session_id` and `trace_id` in `tool_context`

### Documentation

- [ ] Factory function documented for admin CLI
- [ ] Environment variables documented
- [ ] Schema migrations documented

---

## See Also

- **StateStore Implementation Guide for Production** (`docs/tools/statestore-guide.md`) - PostgreSQL/Redis patterns and pause/resume
- **Observability & Monitoring** (`docs/architecture/infrastructure/observability_monitoring.md`) - Logging/metrics patterns
- `penguiflow/state/in_memory.py` - Reference implementation of every capability
- `penguiflow/planner/pause_management.py` - Canonical pause/resume wire format and loader semantics
