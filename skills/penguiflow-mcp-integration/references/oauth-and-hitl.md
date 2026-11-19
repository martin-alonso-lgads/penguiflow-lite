# OAuth via HITL Pause/Resume

This reference covers the ToolNode side of per-user OAuth (`AuthType.OAUTH2_USER`). The planner-side pause/resume mechanics are in [[penguiflow-hitl-pause-resume]].

## End-to-end flow

```
planner.run(query, tool_context={"user_id": "acme:u-123", "trace_id": "t-1", ...})
  -> tool call -> ToolNode.call(name, args, ctx)
  -> OAuthManager.get_token("acme:u-123", "<config name>") returns None
  -> OAuthManager.get_auth_request(provider="<config name>", user_id=..., trace_id="t-1")
  -> ctx.pause("external_event", {"pause_type": "oauth", "provider": ..., "display_name": ...,
                                  "auth_url": ..., "scopes": [...], "state": ...})
  -> planner returns PlannerPause(reason="external_event", payload=..., resume_token=...)
  -> host stores resume_token under trace_id and sends the user to payload["auth_url"]
  -> provider redirects to redirect_uri?code=...&state=...
  -> host callback: user_id, trace_id = await manager.handle_callback(code, state)  (stores the token)
  -> host calls planner.resume(resume_token, user_input="oauth_completed", ...)
  -> the run continues; the next call of the tool finds the token
```

## Required setup

### 1. Providers and token store

```python
import os

from penguiflow.tools import InMemoryTokenStore, OAuthManager, OAuthProviderConfig

manager = OAuthManager(
    providers={
        "github": OAuthProviderConfig(       # key = ExternalToolConfig.name
            name="github",
            display_name="GitHub",
            auth_url="https://github.com/login/oauth/authorize",
            token_url="https://github.com/login/oauth/access_token",
            client_id=os.environ["GH_CLIENT_ID"],
            client_secret=os.environ["GH_CLIENT_SECRET"],
            redirect_uri="https://app.example.com/oauth/callback",
            scopes=["repo"],
        ),
    },
    token_store=InMemoryTokenStore(),        # the default; use a durable, shared store in production
)
```

ToolNode looks providers up by its config `name`, so the `providers` key must match it; an unknown provider raises `ValueError` when a token is needed. `OAuthProviderConfig` values are used as given (no `${VAR}` substitution), so read secrets yourself.

A token store implements the `TokenStore` protocol, keyed by `(user_id, provider)`:

```python
class DurableTokenStore:
    async def store(self, user_id: str, provider: str, token: str, expires_at: float | None) -> None:
        ...

    async def get(self, user_id: str, provider: str) -> str | None:
        ...  # return None when absent or expired

    async def delete(self, user_id: str, provider: str) -> None:
        ...
```

`expires_at` is a Unix timestamp computed from the provider's `expires_in`, or `None`. Refresh tokens are not stored or used.

### 2. ToolNode config and connection

```python
from penguiflow import ModelRegistry
from penguiflow.tools import AuthType, ExternalToolConfig, OAuthManager, ToolNode, TransportType


def build_github_node(manager: OAuthManager, registry: ModelRegistry) -> ToolNode:
    config = ExternalToolConfig(
        name="github",
        transport=TransportType.MCP,
        connection="https://mcp.example.com/github",
        auth_type=AuthType.OAUTH2_USER,
    )
    return ToolNode(config=config, registry=registry, auth_manager=manager)
```

- `connect()` needs a `ToolContext` for `OAUTH2_USER`: `await node.connect(ctx)`, with `ctx.tool_context["user_id"]` set. A bare `connect()` raises `ToolAuthError`, and so does a `call()` that has to reconnect (it reconnects without a context), so connect before the planner uses the tools.
- If that user has no token, `connect(ctx)` calls `ctx.pause(...)` with the payload below, and raises `ToolAuthError` if there is still no token afterwards.
- Without `auth_manager`, any auth attempt raises `ToolAuthError`.

**Per call.** `call()` looks up the calling user's token and pauses the run when it is missing. UTCP/HTTP sources send it as `Authorization: Bearer <token>` on that call. MCP sources send headers only when connecting, so an MCP connection keeps using the token of the user it was connected with. For per-user MCP access, use one ToolNode per user (each with its own `connect(ctx)`).

### 3. `tool_context` keys
On every `planner.run(...)` and `planner.resume(...)`:
- `tool_context["user_id"]`: **required**. Identifies the user owning the token; missing raises `ToolAuthError("user_id required in tool_context for OAuth")`.
- `tool_context["trace_id"]`: optional. Stored with the pending request and returned by `handle_callback`, so the callback can find the paused run.

In multi-tenant systems, make `user_id` tenant-scoped (`acme:user-123`); the token store only sees `user_id` and the provider.

### 4. Callback handler (in your host app)
PenguiFlow ships the token exchange (`OAuthManager.handle_callback`), not the HTTP endpoint. Your endpoint receives the provider redirect, then:

```python
from penguiflow.planner import PlannerFinish, PlannerPause, ReactPlanner
from penguiflow.tools import OAuthManager


async def oauth_callback(
    code: str,
    state: str,
    *,
    manager: OAuthManager,
    planner: ReactPlanner,
    paused: dict[str, str],  # trace_id -> resume_token, saved when the run paused
) -> PlannerFinish | PlannerPause:
    user_id, trace_id = await manager.handle_callback(code, state)
    return await planner.resume(
        paused.pop(trace_id),
        user_input="oauth_completed",
        tool_context={"user_id": user_id, "trace_id": trace_id},  # replaces the stored tool_context
    )
```

`handle_callback(code, state)`:
- consumes the pending `state` (each one works once), and rejects unknown states and states older than 10 minutes with `ValueError`;
- POSTs the code to `token_url` (needs `aiohttp`, from `penguiflow-lite[planner]`), and raises `ValueError` when the provider answers with an `error`;
- stores the access token in `token_store` and returns `(user_id, trace_id)`.

Pending states live in that `OAuthManager` instance's memory. The callback must reach the same process (and manager) that created the auth request.

`planner.resume(..., tool_context=...)` replaces the run's stored `tool_context`, so pass everything the run needs (session id, tenant, ...), or omit it to reuse the stored one.

### 5. Durable planner pause state
For multi-worker deployments, the planner pause must survive restarts. Give the planner a `StateStore` implementing:
- `save_planner_state(token, payload)`;
- `load_planner_state(token)`; resume tokens are one-time (TTL recommended).

Without it, a worker restart between pause and resume drops the pending run.

## Pause payload schema

```python
{
    "pause_type": "oauth",
    "provider": "github",                # ExternalToolConfig.name
    "display_name": "GitHub",            # OAuthProviderConfig.display_name
    "auth_url": "https://github.com/login/oauth/authorize?client_id=...&redirect_uri=...&scope=repo&state=...&response_type=code",
    "scopes": ["repo"],
    "state": "<random>",                 # OAuth state param (treat as secret)
}
```

The pause reason is `"external_event"`. The host UI uses this payload to render the "Connect your GitHub account" prompt. The `auth_url` query values are not URL-encoded, so keep `redirect_uri` and scope names URL-safe.

## Operational defaults

- **Durable, shared token store**: DB or Redis. Tokens outlive process restarts and span workers.
- **Sticky callbacks**: route the OAuth callback to the process that issued the auth request, or replace `OAuthManager` with your own manager that keeps pending states in shared storage.
- **HTTPS-only callbacks** in production.
- **Short pending window**: states expire after 10 minutes; re-prompting is safer than long-lived states.
- **Tenant-scoped `user_id`** (`tenant:user_id`) to avoid cross-tenant token reuse.
- **No tokens in `llm_context`**, ever.

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `ToolAuthError: ... requires HITL OAuth but no ToolContext provided` | `connect()` without a context, or a `call()` that had to reconnect | `await node.connect(ctx)` before the run |
| `ToolAuthError: user_id required in tool_context for OAuth` | `tool_context` missing `user_id` | Set it on every `run(...)` / `resume(...)` |
| `ValueError: Unknown OAuth provider` | `providers` key differs from the config `name` | Key providers by the ToolNode name |
| `ValueError: Invalid or expired OAuth state` | Callback hit another process, the state was already used, or it is older than 10 minutes | Sticky callbacks; restart the auth flow |
| Pause fires but resume raises `KeyError` | Pause state was in memory and the worker restarted | Implement `save_planner_state`/`load_planner_state` durably |
| OAuth completed but the tool still pauses | Token stored under another `(user_id, provider)`, or the store isn't shared | Use the same keys; share the store |
| MCP calls act as the wrong user | MCP sends headers only at connect | One ToolNode per user, or a UTCP/HTTP source |
| Tokens visible in prompts | Stored in `llm_context` | Keep them in the token store; audit prompt logs |

## Observability

Track:
- Pause count by provider (`pause_type="oauth"`).
- Time-to-resume p50/p95/p99.
- Callback success and failure (invalid or expired state, provider errors).
- Token store hit rate (how often `get` returns a token without prompting).

A high `pause_type="oauth"` rate for users who already authenticated means the token store is not durable or the key is wrong.

## Testing

Use an `OAuthManager` with an `InMemoryTokenStore`. Leave it empty to assert the pause payload and the resume contract; pre-store a token (`await store.store("u-1", "github", "token", None)`) to exercise the authenticated path without a provider.
