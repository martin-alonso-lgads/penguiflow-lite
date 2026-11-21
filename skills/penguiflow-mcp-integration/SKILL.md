---
name: penguiflow-mcp-integration
description: Wire external tools (MCP servers, UTCP, HTTP, CLI) into a PenguiFlow ReactPlanner via `ToolNode` and `ExternalToolConfig` — pick a transport (stdio command or URL for MCP), pass credentials with `env=` for stdio servers or an auth mode (API key/bearer/cookie for URL and UTCP sources, per-user OAuth2 with one MCP session per user for URL MCP servers), substitute `${VAR}`, set timeouts/retries/concurrency, allowlist tools with `tool_filter`, use MCP resources (`{ns}.resources_list`/`resources_read`, `subscribe_resource` update callbacks), extract large content into artifacts, and gate per-user OAuth through `OAuthManager` and HITL pause/resume. Use when a user says "wire MCP tools", "connect to an MCP server", "external tools", "ToolNode", "OAuth for tools", "tool filter", "MCP resources", "UTCP", or names `ExternalToolConfig`, `TransportType`, `AuthType`, `OAuthManager`.
---

# PenguiFlow MCP / External Tool Integration

## When to use
- Connecting an MCP server (`@modelcontextprotocol/server-*`) as planner tools.
- Wiring a UTCP/HTTP/CLI external tool catalog.
- Enabling OAuth-scoped per-user tool access.
- Tightening tool exposure (allowlists, read-only filters).
- Using MCP resources (URI discovery, reads, update subscriptions).

## When NOT to use
- Wiring planner-internal tools (Python functions decorated with `@tool`) → that's just [[penguiflow-reactplanner-config]] catalog wiring.
- Calling a remote A2A agent → use [[penguiflow-a2a-integration]] (`A2AAgentToolset`).
- The OAuth pause-resume lifecycle on the planner side → use [[penguiflow-hitl-pause-resume]].
- The artifact store backend used for large content → use [[penguiflow-statestore]] (`ArtifactStore`).

## Hard boundaries
This skill stops at "ToolNode exposes the right tools with safe defaults". Pause/resume state, the durable artifact store, and planner catalog wiring belong to sibling skills.

## Workflow

### 1) Pick a transport
`TransportType` values (ToolNode needs `penguiflow-lite[planner]`, which brings fastmcp, utcp and tenacity):
- `MCP`: `connection` is either a launch command (split with `shlex`, run over stdio, e.g. `npx -y @modelcontextprotocol/server-github`) or a URL starting with `http://`, `https://`, `ws://` or `wss://`.
- `HTTP`: `connection` is a base URL; tools are called through utcp's HTTP call template.
- `UTCP`: `connection` points to a UTCP manual or base URL.
- `CLI`: `connection` is a command for utcp's CLI call template.

Pick based on what the tool source actually speaks. MCP is the dominant choice for ecosystem tools.

### 2) Build an `ExternalToolConfig`
A stdio server gets no HTTP headers, so pass its credentials as environment variables:

```python
from penguiflow.tools import ExternalToolConfig, TransportType

github = ExternalToolConfig(
    name="github",                               # namespace; tools surface as github.<tool>
    transport=TransportType.MCP,
    connection="npx -y @modelcontextprotocol/server-github",
    env={"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"},  # ${VAR} from the host env
    tool_filter=["get_", "list_", "search_"],    # regex allowlist (re.match on the bare name)
    max_concurrency=3,                           # per-ToolNode semaphore
    timeout_s=30.0,                              # per attempt
)
```

A URL server gets the auth headers when connecting:

```python
from penguiflow.tools import AuthType, ExternalToolConfig, TransportType

search = ExternalToolConfig(
    name="search",
    transport=TransportType.MCP,
    connection="https://mcp.example.com/mcp",
    auth_type=AuthType.BEARER,
    auth_config={"token": "${SEARCH_TOKEN}"},
)
```

Required fields: `name`, `transport`, `connection`. Everything else has defaults; `references/external-tool-config.md` lists every field.

### 3) Choose an auth mode
| `AuthType` | Header sent | `auth_config` keys |
|---|---|---|
| `NONE` | — | — |
| `API_KEY` | `<header>: <api_key>` | `api_key` (required), `header` (default `X-API-Key`) |
| `BEARER` | `Authorization: Bearer <token>` | `token` (required) |
| `COOKIE` | `Cookie: <cookie_name>=<cookie_value>` | `cookie_name`, `cookie_value` (both required) |
| `OAUTH2_USER` | `Authorization: Bearer <user token>` | none; providers live in `ToolNode(auth_manager=OAuthManager(...))` (Step 6) |

Where credentials go: URL MCP servers get the headers when a session opens. Stdio servers never get headers, so the config rejects any `auth_type` but `NONE` on a stdio command (use `env=`). UTCP/HTTP/CLI sources get no headers either: `env` and `auth_config` values reach the utcp client as variables, and `OAUTH2_USER` is rejected there. A missing required key, or one of these combinations, raises a validation error when the config is built. See `references/auth-and-env.md`.

### 4) Substitute env vars with `${VAR}`
`${VAR}` is substituted in `env` values and in `auth_config` values, and nowhere else (not in `connection`). Several per string are fine. A missing variable raises `ToolAuthError` ("Missing required environment variable ...") when the value is used: at `connect()` for headers and stdio env, and again on each call for headers. For `env=` values it arrives wrapped in `ToolConnectionError`.

Don't put secrets in `llm_context`; keep them in env vars and `auth_config`.

### 5) Allowlist with `tool_filter`
`tool_filter` is a list of regexes matched with `re.match(...)` against the server's bare tool names, so `"get_"` matches `get_issue` (anchor with `$` for exact names). `None` or `[]` exposes every tool; the default is `None`. Examples: `["get_", "list_", "search_"]` (read-only), `["create_issue$", "list_repositories$"]` (specific). The generated `{ns}.resources_*` and `{ns}.prompts_*` tools are not filtered. Use an explicit allowlist in production.

### 6) (Optional) Per-user OAuth via HITL
Set `auth_type=AuthType.OAUTH2_USER` on an MCP URL connection and pass `ToolNode(config=cfg, registry=registry, auth_manager=OAuthManager(providers={cfg.name: OAuthProviderConfig(...)}))`. The provider key must equal the config `name`. `connect()` needs a `ToolContext` (`await node.connect(ctx)`; a bare `connect()` raises `ToolAuthError`), and every run needs `tool_context["user_id"]`. One ToolNode serves every user: it keeps one MCP session per `(tool_context["tenant_id"], tool_context["user_id"])`, opened with that user's token. `connect(ctx)` opens the first session and discovers the tools on it; other users' sessions open on their first call, and `session_pool` closes idle ones. When the user has no token, ToolNode pauses the planner with `reason="external_event"` and payload `{"pause_type": "oauth", "provider": ..., "display_name": ..., "auth_url": ..., "scopes": [...], "state": ...}`. Your callback endpoint calls `await manager.handle_callback(code, state)`, which stores the token and returns `(user_id, trace_id)`; then resume the run. See `references/oauth-and-hitl.md`.

### 7) (Optional) Use MCP resources
If the server answers `resources/list` at connect, ToolNode adds `{ns}.resources_list`, `{ns}.resources_read` and `{ns}.resources_templates_list` tools. Programmatic API: `list_resources()`, `read_resource(uri, ctx)`, `list_resource_templates()`, `subscribe_resource(uri, callback)`, `unsubscribe_resource(uri)`; with `OAUTH2_USER`, pass `ctx=` so the call uses that user's session. Each MCP session has its own read cache and subscriptions, and a subscription ends when its session closes (subscribe again after a reconnect). A server's `notifications/resources/updated` invalidates that session's read cache, calls the `set_resource_updated_callback` callback with `(uri, principal)` (`principal` is the session's `(tenant_id, user_id)`, `None` for the shared session; a planner holding the tools uses it to emit `resource_updated` events, only in a run of that user), then your `subscribe_resource` callback with the URI. Reads over the inline threshold, and binary reads, go to the artifact store. See `references/resources-and-artifacts.md`.

### 8) Connect, inspect, close
```python
from penguiflow import ModelRegistry
from penguiflow.tools import ToolNode

registry = ModelRegistry()
tool_node = ToolNode(config=github, registry=registry)
await tool_node.connect()
for spec in tool_node.get_tools():
    print(spec.name)
...  # run the planner
await tool_node.close()
```

Pass `tool_node.get_tools()` (a list of `NodeSpec`) into your planner catalog, e.g. `ReactPlanner(catalog=[*tool_node.get_tools(), ...])`. The planner skill handles catalog composition. `close()` closes every MCP session and clears the discovered tools.

## Troubleshooting (fast checks)
- **`ToolAuthError: Missing required environment variable ...`**: a `${VAR}` is unset in the worker's environment (not just your shell).
- **Stdio server reports missing credentials**: stdio servers get no auth headers, and inherit only a minimal environment (`PATH`, `HOME`, `USER`, ...). Pass what the server reads in `env=`.
- **`ValidationError: auth_type=... needs an MCP URL connection`**: header auth on a stdio command. Pass the credentials in `env=` with `auth_type=NONE`, or connect to the server's URL.
- **Too many tools exposed**: add a stricter `tool_filter`. `[]` exposes everything, like `None`.
- **`429`/rate-limit storms**: lower `max_concurrency` (shared by all users) and reduce planner parallelism. Errors whose status is in `retry_policy.retry_on_status` (default `[429, 500, 502, 503, 504]`) are retried (3 attempts, 0.1–5 s exponential backoff).
- **`ToolTimeoutError`**: the call exceeded `timeout_s` (default 30 s, allowed 1–300) on every attempt. Timeouts and connection errors are retried too, so the worst case is about `timeout_s × max_attempts`.
- **OAuth pauses but never completes**: the callback didn't call `handle_callback`, or reached another process (pending `state`s live in that manager's memory for 10 minutes), or the token store isn't shared, or there's no durable planner state for the pause ([[penguiflow-hitl-pause-resume]]).
- **`ToolAuthError` with `OAUTH2_USER` on a call**: `tool_context["user_id"]` is missing, there's no `auth_manager`, or the user didn't finish OAuth. A `401` from the server closes that user's session and deletes the token it was opened with, so the next call pauses for OAuth consent again.
- **Every user sees the first user's tools**: tools are discovered once, on the first session, so a server that filters tools per user exposes that user's list. Connect as a user who has every tool, and hide tools per user with a `ToolVisibilityPolicy`.
- **Resource tools not generated**: the server's `resources/list` failed at connect (no resources support).
- **Reads return artifacts**: the text is over `artifact_extraction.resources.inline_text_if_under_chars` (default 10 000 chars), or the content is binary. Download with `ctx.artifacts.download(result["artifact"]["id"])`.
- **Artifacts download as `None`**: the planner has no artifact store (`ReactPlanner(artifact_store=...)`), so `NoOpArtifactStore` kept only a preview.
- **`npx -y` crashes under load**: run MCP servers as managed services and connect via URL.
- **Cross-tenant token reuse**: tokens are keyed by `(tool_context["user_id"], provider)`. Make `user_id` tenant-scoped (`tenant:user`).

## Worked examples
- `examples/toolnode_presets/flow.py` prints the built-in MCP presets (`POPULAR_MCP_SERVERS`, `get_preset`). It connects to nothing.

## References (load only as needed)
- `references/external-tool-config.md`: every `ExternalToolConfig` field, transport semantics, resilience knobs, artifact extraction, namespacing, lifecycle.
- `references/auth-and-env.md`: where credentials go per transport, auth modes, `${VAR}` substitution, secret hygiene.
- `references/oauth-and-hitl.md`: `OAuthManager`, `OAuthProviderConfig`, token stores, the OAuth pause payload, callback and resume flow.
- `references/resources-and-artifacts.md`: MCP resources, subscriptions, `ResourceCache`, artifact extraction, `ctx.artifacts`.
