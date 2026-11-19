# StateStore Implementation Guide for Production

This guide explains how to implement a production-ready `StateStore` for PenguiFlow, enabling durable audit/replay, A2A router continuity, and distributed pause/resume flows (OAuth/HITL).

> [!NOTE]
> Canonical pages (in the PenguiFlow docs):
> - **State store** (`docs/tools/statestore.md`)
> - **OAuth & HITL** (`docs/tools/oauth-hitl.md`)
>
> The full method contracts are in the **StateStore Implementation Specification** (`docs/spec/STATESTORE_IMPLEMENTATION_SPEC.md`).

## Overview

In production deployments with multiple workers, you need a persistent `StateStore` to:

1. **Persist audit events** for audit/replay
2. **Persist A2A remote bindings** for remote task tracking
3. **Reuse A2A specialist context** across router turns when `A2AAgentToolset` is used
4. **Save planner state** when pausing for OAuth or HITL
5. **Load planner state** when resuming after the OAuth callback or the human's answer

When using `ToolNode` with user-level OAuth (`AuthType.OAUTH2_USER`), the planner pauses execution to wait for user authentication. A separate `TokenStore` (also shown below) keeps the OAuth tokens themselves.

## StateStore Protocol

Penguiflow's `StateStore` protocol (`penguiflow/state/protocol.py`, import path `penguiflow.state`) defines three required methods:

```python
from collections.abc import Sequence
from typing import Protocol

from penguiflow.state import RemoteBinding, StoredEvent


class StateStore(Protocol):
    async def save_event(self, event: StoredEvent) -> None:
        """Persist an audit event for replay/audit."""
        ...

    async def load_history(self, trace_id: str) -> Sequence[StoredEvent]:
        """Load all events for a trace."""
        ...

    async def save_remote_binding(self, binding: RemoteBinding) -> None:
        """Save A2A remote binding."""
        ...
```

For pause/resume support (required for OAuth/HITL flows), add these **duck-typed** methods:

```python
async def save_planner_state(self, token: str, payload: dict) -> None:
    """Save pause record for distributed resume."""
    ...

async def load_planner_state(self, token: str) -> dict | None:
    """Load pause record for resume (return None if missing/expired)."""
    ...
```

For A2A router-to-specialist continuity, add these **duck-typed** methods. `A2AAgentToolset` uses the store from `tool_context["state_store"]`, or else the planner's `planner.state_store`:

```python
from collections.abc import Sequence

from penguiflow.state import RemoteBinding


async def find_binding(
    self,
    *,
    router_session_id: str,
    agent_url: str,
    remote_skill: str,
    tenant_id: str | None = None,
    user_id: str | None = None,
) -> RemoteBinding | None:
    """Return the newest non-terminal specialist binding for this router scope."""
    ...

async def list_bindings(self, *, router_session_id: str) -> Sequence[RemoteBinding]:
    """List bindings for debugging/inspection."""
    ...

async def mark_binding_terminal(self, *, trace_id: str, context_id: str | None, task_id: str) -> None:
    """Prevent an exact remote binding from being reused."""
    ...
```

`ReactPlanner` and A2A tooling check optional methods via `hasattr()` / `getattr()` at runtime. If these methods are missing, A2A calls still work but remote `contextId` reuse is disabled.

Normal continuity reuses the stored remote `context_id` and omits `task_id`. `task_id` is only reused when binding metadata marks an unfinished input/auth-required remote task with `awaiting_remote_input` or `awaiting_remote_auth`. Bindings belong to the router conversation (`tool_context["session_id"]`): the toolset always saves them with `is_terminal=False` and never calls `mark_binding_terminal`, so call it yourself when a conversation should stop reusing a specialist context.

---

## PostgreSQL Implementation

### Database Schema

```sql
-- Events table (required)
--
-- NOTE: StoredEvent.trace_id is nullable. Normalise None to '__global__'
-- so trace_id can be NOT NULL and participate in uniqueness constraints.
CREATE TABLE flow_events (
    id BIGSERIAL PRIMARY KEY,
    trace_id VARCHAR(128) NOT NULL,
    ts DOUBLE PRECISION NOT NULL,
    kind VARCHAR(64) NOT NULL,
    node_name VARCHAR(128),
    node_id VARCHAR(128),
    event_fp VARCHAR(64) NOT NULL,
    payload JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (trace_id, event_fp)
);

CREATE INDEX idx_events_trace_ts ON flow_events(trace_id, ts, id);

-- Planner pauses (for OAuth flows)
CREATE TABLE planner_pauses (
    token VARCHAR(128) PRIMARY KEY,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ DEFAULT (NOW() + INTERVAL '1 hour')
);

-- OAuth tokens (for ToolNode auth)
CREATE TABLE oauth_tokens (
    user_id VARCHAR(64) NOT NULL,
    provider VARCHAR(64) NOT NULL,
    access_token TEXT NOT NULL,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, provider)
);

-- Remote bindings (for A2A server tasks and A2AAgentToolset continuity)
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

-- Cleanup expired pauses
CREATE INDEX idx_pauses_expires ON planner_pauses(expires_at);
```

### Python Implementation

```python
import hashlib
import json
from collections.abc import Sequence

import asyncpg

from penguiflow.state import RemoteBinding, StoredEvent


class PostgresStateStore:
    """Production StateStore backed by PostgreSQL."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # ─── Required Methods ────────────────────────────────────────────────────────

    async def save_event(self, event: StoredEvent) -> None:
        """Persist an audit event."""
        trace_id = event.trace_id or "__global__"
        event_fp = hashlib.sha256(
            json.dumps(
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
            ).encode("utf-8")
        ).hexdigest()
        await self._pool.execute(
            """
            INSERT INTO flow_events (trace_id, ts, kind, node_name, node_id, event_fp, payload)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (trace_id, event_fp) DO NOTHING
            """,
            trace_id,
            event.ts,
            event.kind,
            event.node_name,
            event.node_id,
            event_fp,
            json.dumps(dict(event.payload)) if event.payload else None,
        )

    async def load_history(self, trace_id: str) -> Sequence[StoredEvent]:
        """Load all events for a trace."""
        rows = await self._pool.fetch(
            """
            SELECT trace_id, ts, kind, node_name, node_id, payload
            FROM flow_events
            WHERE trace_id = $1
            ORDER BY ts ASC, id ASC
            """,
            trace_id,
        )
        return [
            StoredEvent(
                trace_id=row["trace_id"],
                ts=float(row["ts"]),
                kind=row["kind"],
                node_name=row["node_name"],
                node_id=row["node_id"],
                payload=json.loads(row["payload"]) if row["payload"] else {},
            )
            for row in rows
        ]

    async def save_remote_binding(self, binding: RemoteBinding) -> None:
        """Save A2A remote binding."""
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

    async def find_binding(
        self,
        *,
        router_session_id: str,
        agent_url: str,
        remote_skill: str,
        tenant_id: str | None = None,
        user_id: str | None = None,
    ) -> RemoteBinding | None:
        """Return the newest active A2A specialist binding for this router scope."""
        rows = await self._pool.fetch(
            """
            SELECT * FROM remote_bindings
            WHERE router_session_id = $1
              AND agent_url = $2
              AND remote_skill = $3
              AND is_terminal = FALSE
            ORDER BY updated_at DESC, id DESC
            """,
            router_session_id,
            agent_url,
            remote_skill,
        )
        for row in rows:
            if (tenant_id is not None or row["tenant_id"] is not None) and row["tenant_id"] != tenant_id:
                continue
            if (user_id is not None or row["user_id"] is not None) and row["user_id"] != user_id:
                continue
            return self._parse_remote_binding(row)
        return None

    async def list_bindings(self, *, router_session_id: str) -> list[RemoteBinding]:
        rows = await self._pool.fetch(
            "SELECT * FROM remote_bindings WHERE router_session_id = $1 ORDER BY updated_at DESC, id DESC",
            router_session_id,
        )
        return [self._parse_remote_binding(row) for row in rows]

    async def mark_binding_terminal(self, *, trace_id: str, context_id: str | None, task_id: str) -> None:
        await self._pool.execute(
            """
            UPDATE remote_bindings
            SET is_terminal = TRUE, updated_at = NOW()
            WHERE trace_id = $1
              AND context_id IS NOT DISTINCT FROM $2
              AND task_id = $3
            """,
            trace_id,
            context_id,
            task_id,
        )

    def _parse_remote_binding(self, row: asyncpg.Record) -> RemoteBinding:
        metadata = row["metadata"]
        return RemoteBinding(
            trace_id=row["trace_id"],
            context_id=row["context_id"],
            task_id=row["task_id"],
            agent_url=row["agent_url"],
            router_session_id=row["router_session_id"],
            remote_skill=row["remote_skill"],
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            last_remote_task_id=row["last_remote_task_id"],
            is_terminal=bool(row["is_terminal"]),
            metadata=json.loads(metadata) if isinstance(metadata, str) else dict(metadata or {}),
        )

    # ─── Pause/Resume Support (duck-typed) ───────────────────────────────────────

    async def save_planner_state(self, token: str, payload: dict) -> None:
        """Save pause record for distributed resume.

        Called when planner pauses for OAuth or HITL approval.
        """
        await self._pool.execute(
            """
            INSERT INTO planner_pauses (token, payload)
            VALUES ($1, $2)
            ON CONFLICT (token) DO UPDATE SET payload = EXCLUDED.payload
            """,
            token,
            json.dumps(payload),
        )

    async def load_planner_state(self, token: str) -> dict | None:
        """Load pause record for resume.

        Called when a planner instance that did not pause the run resumes it.
        """
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

    # ─── Cleanup ─────────────────────────────────────────────────────────────────

    async def cleanup_expired_pauses(self) -> int:
        """Remove expired pause records. Run periodically."""
        result = await self._pool.execute(
            "DELETE FROM planner_pauses WHERE expires_at < NOW()"
        )
        return int(result.split()[-1])  # Returns "DELETE N"
```

The planner that paused a run keeps the pause record in memory too, and resumes from that copy without calling `load_planner_state`. The stored row is consumed only when another planner instance resumes; otherwise `cleanup_expired_pauses()` removes it.

### Usage with ToolNode

An `OAUTH2_USER` ToolNode authenticates tool discovery as a user, so it needs `connect(ctx)`: a `ToolContext` whose `tool_context` carries that user's `user_id`. `connect()` without a context raises `ToolAuthError`. So does `call()` on a node that is not connected on the current event loop, because `call()` reconnects without a context; connect on the loop that serves the calls. If the user has no token yet, `connect(ctx)` pauses the run through `ctx.pause(...)` with the OAuth payload, so connect from code that runs inside a planner run, such as a tool.

```python
import os
from datetime import UTC, datetime

import asyncpg

from penguiflow.planner import ReactPlanner, ToolContext
from penguiflow.registry import ModelRegistry
from penguiflow.tools import (
    AuthType,
    ExternalToolConfig,
    OAuthManager,
    OAuthProviderConfig,
    ToolNode,
    TransportType,
)


# TokenStore that uses the same database
class PostgresTokenStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def store(self, user_id: str, provider: str, token: str, expires_at: float | None) -> None:
        expires_dt = datetime.fromtimestamp(expires_at, tz=UTC) if expires_at else None
        await self._pool.execute(
            """
            INSERT INTO oauth_tokens (user_id, provider, access_token, expires_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id, provider) DO UPDATE
            SET access_token = EXCLUDED.access_token,
                expires_at = EXCLUDED.expires_at
            """,
            user_id, provider, token, expires_dt,
        )

    async def get(self, user_id: str, provider: str) -> str | None:
        row = await self._pool.fetchrow(
            """
            SELECT access_token FROM oauth_tokens
            WHERE user_id = $1 AND provider = $2
              AND (expires_at IS NULL OR expires_at > NOW())
            """,
            user_id, provider,
        )
        return row["access_token"] if row else None

    async def delete(self, user_id: str, provider: str) -> None:
        await self._pool.execute(
            "DELETE FROM oauth_tokens WHERE user_id = $1 AND provider = $2",
            user_id, provider,
        )


def build_github_node(pool: asyncpg.Pool) -> tuple[ToolNode, OAuthManager]:
    """Create the user-scoped GitHub ToolNode and its OAuth manager."""
    oauth_manager = OAuthManager(
        providers={
            "github": OAuthProviderConfig(
                name="github",
                display_name="GitHub",
                auth_url="https://github.com/login/oauth/authorize",
                token_url="https://github.com/login/oauth/access_token",
                client_id=os.environ["GITHUB_CLIENT_ID"],
                client_secret=os.environ["GITHUB_CLIENT_SECRET"],
                redirect_uri="https://myapp.com/oauth/callback",
                scopes=["repo", "user"],
            ),
        },
        token_store=PostgresTokenStore(pool),
    )
    github = ToolNode(
        config=ExternalToolConfig(
            name="github",
            transport=TransportType.MCP,
            connection="npx -y @modelcontextprotocol/server-github",
            auth_type=AuthType.OAUTH2_USER,
        ),
        registry=ModelRegistry(),
        auth_manager=oauth_manager,
    )
    return github, oauth_manager


async def connect_github(github: ToolNode, ctx: ToolContext) -> None:
    """Connect as the user in ctx.tool_context["user_id"].

    Without a stored token this pauses the run for OAuth, like call() does.
    """
    await github.connect(ctx)


def build_planner(github: ToolNode, state_store: PostgresStateStore) -> ReactPlanner:
    """Build a planner over the tools of a connected ToolNode."""
    return ReactPlanner(
        catalog=github.get_tools(),  # empty until connect(ctx) has run
        llm="gpt-4o",
        state_store=state_store,  # Enables distributed pause/resume and A2AAgentToolset continuity
    )
```

---

## Redis Implementation

For high-throughput scenarios, use Redis for pause/resume state:

```python
import json
import time
from collections.abc import Sequence

import asyncpg
import redis.asyncio as redis

from penguiflow.state import RemoteBinding, StoredEvent


class RedisStateStore:
    """StateStore with Redis for pause/resume, PostgreSQL for events."""

    def __init__(self, redis_client: redis.Redis, pg_pool: asyncpg.Pool) -> None:
        self._redis = redis_client
        self._pool = pg_pool
        self._pause_ttl = 3600  # 1 hour

    # Events and bindings still go to PostgreSQL (for audit/replay)
    async def save_event(self, event: StoredEvent) -> None:
        ...  # same as PostgresStateStore

    async def load_history(self, trace_id: str) -> Sequence[StoredEvent]:
        ...  # same as PostgresStateStore

    async def save_remote_binding(self, binding: RemoteBinding) -> None:
        ...  # same as PostgresStateStore

    # Pause/resume uses Redis for speed
    async def save_planner_state(self, token: str, payload: dict) -> None:
        await self._redis.setex(
            f"planner:pause:{token}",
            self._pause_ttl,
            json.dumps(payload),
        )

    async def load_planner_state(self, token: str) -> dict | None:
        # One-time use: consume/delete the pause record when loading.
        data = await self._redis.getdel(f"planner:pause:{token}")
        if not data:
            return None
        return json.loads(data)


class RedisTokenStore:
    """TokenStore backed by Redis (create the client with decode_responses=True)."""

    def __init__(self, redis_client: redis.Redis) -> None:
        self._redis = redis_client

    async def store(self, user_id: str, provider: str, token: str, expires_at: float | None) -> None:
        key = f"oauth:token:{user_id}:{provider}"
        if expires_at:
            ttl = int(expires_at - time.time())
            if ttl > 0:
                await self._redis.setex(key, ttl, token)
        else:
            await self._redis.set(key, token)

    async def get(self, user_id: str, provider: str) -> str | None:
        return await self._redis.get(f"oauth:token:{user_id}:{provider}")

    async def delete(self, user_id: str, provider: str) -> None:
        await self._redis.delete(f"oauth:token:{user_id}:{provider}")
```

---

## OAuth Callback Handler

When the user completes OAuth, the provider redirects the browser to your `redirect_uri` with `code` and `state` query parameters. Your callback endpoint should:

1. Exchange the code for a token and store it (`OAuthManager.handle_callback`)
2. Find the paused run for the returned `trace_id`
3. Resume the planner

`handle_callback(code, state)` returns `(user_id, trace_id)`. It raises `ValueError` for an unknown, already used or expired `state` (pending states expire after 10 minutes) and for an error in the provider's token response; treat those as a bad request. Other exceptions, such as `RuntimeError` when `aiohttp` is not installed or a network error while calling the provider, are server errors.

```python
from fastapi import FastAPI, HTTPException

app = FastAPI()
oauth_manager = ...  # the OAuthManager from build_github_node(...)


@app.get("/oauth/callback")
async def oauth_callback(code: str, state: str) -> dict[str, object]:
    """Handle the OAuth redirect from the provider."""
    try:
        # Exchange code for token and store it in the TokenStore
        user_id, trace_id = await oauth_manager.handle_callback(code, state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Resume the run that paused for this trace. Your app kept the
    # PlannerPause.resume_token, keyed by trace_id, when the planner paused.
    # With distributed workers, publish a resume job instead:
    # result = await planner.resume(resume_token, user_input="oauth_completed")

    return {"ok": True, "user_id": user_id, "trace_id": trace_id}
```

`OAuthManager` keeps pending `state` values in process memory (`pending_states`). Route the callback to the worker that built the auth URL (sticky sessions), or give ToolNode your own auth manager that keeps pending states in shared storage; otherwise `handle_callback` raises `ValueError("Invalid or expired OAuth state")` on the other workers.

`planner.resume(token, ...)` without `tool_context` reuses the `tool_context` stored with the pause. Passing `tool_context=` replaces it, so include `user_id`, `session_id` and `trace_id` again.

---

## Distributed Worker Architecture

For production deployments with multiple workers:

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Worker 1  │     │   Worker 2  │     │   Worker 3  │
│  (planner)  │     │  (planner)  │     │  (planner)  │
└──────┬──────┘     └──────┬──────┘     └──────┬──────┘
       │                   │                   │
       └───────────────────┼───────────────────┘
                           │
                    ┌──────▼──────┐
                    │   Redis     │ ← Pause state (fast)
                    │   Cluster   │
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │  PostgreSQL │ ← Events, tokens (durable)
                    └─────────────┘
```

**Flow:**
1. Worker 1 receives request, starts planner
2. Planner pauses for OAuth, saves state to Redis
3. User completes OAuth in browser
4. Callback (on the worker that issued the auth URL) stores token in PostgreSQL, publishes resume event
5. Worker 2 picks up resume, loads state from Redis
6. Planner continues execution

An `A2AService` is the exception: it maps paused A2A tasks to their resume tokens in process memory, so a paused A2A task resumes only in the process that paused it.

---

## Best Practices

### Token Security

1. **Encrypt tokens at rest** - Use database-level encryption or application-level encryption for `access_token` column
2. **Use short TTLs** - Set `expires_at` based on provider's token lifetime
3. **Rotate refresh tokens** - If provider supports refresh tokens, implement rotation logic

### State Cleanup

1. **Expire old pauses** - Run `cleanup_expired_pauses()` periodically (cron job or scheduled task)
2. **Monitor pause table size** - Alert if pauses accumulate (indicates callback failures)
3. **Log pause/resume events** - Track OAuth flow completion rates

### High Availability

1. **PostgreSQL replication** - Use read replicas for `load_history()` calls
2. **Redis Cluster** - Use Redis Cluster for pause state distribution
3. **Connection pooling** - Always use connection pools (`asyncpg.Pool`, `redis.ConnectionPool`)

---

## See Also

- **Concurrency Configuration Guide** (`docs/tools/concurrency-guide.md`)
- **ToolNode Configuration Guide** (`docs/tools/configuration-guide.md`)
- **Artifacts & resources** ([artifacts-and-resources.md](artifacts-and-resources.md))
