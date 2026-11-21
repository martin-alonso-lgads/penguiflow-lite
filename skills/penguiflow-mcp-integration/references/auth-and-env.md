# Credentials, Auth Modes and `${VAR}` Substitution

## Where credentials go, by transport

| Source | How credentials reach it |
|---|---|
| MCP over stdio (`connection` is a command) | Only through `env=`. No HTTP headers exist, so the config accepts only `auth_type=NONE`. |
| MCP over a URL (`http(s)://`, `ws(s)://`) | Auth headers, sent when the MCP session opens (not per call). With `OAUTH2_USER`, each user gets their own session, opened with their token. |
| UTCP / HTTP / CLI | `env` and `auth_config` values are passed to the utcp client as variables, named by their keys, for the UTCP manual's call templates to reference. In `BASE_URL` mode, the `http` call template ToolNode builds also carries the static auth (`BEARER`, `API_KEY`, `COOKIE`) as UTCP `api_key` auth, in both `auth` (the discovery request) and `auth_tools` (tools generated from an OpenAPI spec); a `cli` template carries none. `OAUTH2_USER` is rejected. |

## Stdio servers: `env=`

```python
from penguiflow.tools import ExternalToolConfig, TransportType

ExternalToolConfig(
    name="postgres",
    transport=TransportType.MCP,
    connection="npx -y @modelcontextprotocol/server-postgres",
    env={"DATABASE_URL": "${DATABASE_URL}"},
)
```

The MCP SDK starts the subprocess with a minimal inherited environment (`PATH`, `HOME`, `USER`, `SHELL`, `TERM`, `LOGNAME` on POSIX) plus `env`. Variables set in the host process are **not** passed through unless you list them in `env`. An `auth_type` other than `NONE` on a stdio command fails validation when the config is built.

## `AuthType` matrix

### `AuthType.NONE`
No auth headers. The default.

### `AuthType.BEARER`
Sends `Authorization: Bearer <token>`.

```python
from penguiflow.tools import AuthType, ExternalToolConfig, TransportType

ExternalToolConfig(
    name="search",
    transport=TransportType.MCP,
    connection="https://mcp.example.com/mcp",
    auth_type=AuthType.BEARER,
    auth_config={"token": "${SEARCH_TOKEN}"},
)
```

### `AuthType.API_KEY`
Sends the key in a header, `X-API-Key` unless `header` names another.

```python
from penguiflow.tools import AuthType, ExternalToolConfig, TransportType

ExternalToolConfig(
    name="billing",
    transport=TransportType.HTTP,
    connection="https://billing.example.com/utcp",
    auth_type=AuthType.API_KEY,
    auth_config={
        "api_key": "${BILLING_API_KEY}",
        "header": "X-Custom-Key",   # optional
    },
)
```

### `AuthType.COOKIE`
Sends `Cookie: <cookie_name>=<cookie_value>`. Useful for session-cookie apps.

```python
from penguiflow.tools import AuthType, ExternalToolConfig, TransportType

ExternalToolConfig(
    name="internal_app",
    transport=TransportType.MCP,
    connection="https://apps.example.com/mcp",
    auth_type=AuthType.COOKIE,
    auth_config={
        "cookie_name": "session",
        "cookie_value": "${SESSION_COOKIE}",
    },
)
```

### `AuthType.OAUTH2_USER`
Per-user OAuth through an `OAuthManager` and a planner HITL pause, for MCP over a URL only. `auth_config` stays empty; providers are configured on the manager. It needs `tool_context["user_id"]` on every run and `connect(ctx)` instead of `connect()`. ToolNode opens one MCP session per user, with that user's token. See `oauth-and-hitl.md`.

The config validator requires `token` for `BEARER`, `api_key` for `API_KEY`, and both cookie keys for `COOKIE`. It rejects any `auth_type` but `NONE` on an MCP stdio command, and `OAUTH2_USER` on UTCP/HTTP/CLI. Each of these fails when the `ExternalToolConfig` is built.

## Auth resolution timing

ToolNode resolves auth at two moments:
1. **`connect(...)`**: builds the headers and opens the MCP session with them (or builds the utcp variables). For an MCP URL server these are the only headers that session will ever send.
2. **`ToolNode.call(...)`**: resolves the headers again (substituting `${VAR}` again, or looking up the calling user's OAuth token, pausing if it is missing). With `OAUTH2_USER` they pick the caller's MCP session, and a changed token opens a new session. Static headers don't change an open session, and UTCP calls send no headers.

So a rotated static token, for MCP URL and UTCP sources alike, takes effect only after a reconnect (`await node.force_reconnect()`, which is `close()` then `connect()`). A user's new OAuth token takes effect on that user's next call.

## `${VAR}` substitution

`${VAR}` is replaced from the process environment in:
- `env` values (stdio subprocess and utcp variables);
- `auth_config` values: the header values of the static modes, and every value passed to utcp as a variable.

It is **not** applied to `connection` or any other field. Several variables per string are fine (`"${ORG}-${TEAM}"`); `${...}` is the only syntax.

A missing variable raises `ToolAuthError("Missing required environment variable 'X' for ToolNode '<name>'")` when the value is used: at `connect()` and, for headers, on every call. For `env=` values the error arrives wrapped in `ToolConnectionError`. Missing credentials thus surface at startup, not halfway through a session.

## Secret hygiene

### Don't put secrets in `llm_context`
`llm_context` is fed to the LLM in every prompt. Tokens there leak into model providers and logs.

Instead:
- Put service secrets in env vars, referenced from `env` or `auth_config`.
- Put per-user identity in `tool_context["user_id"]` (an opaque ID, not a token).
- Use `OAUTH2_USER` for user-scoped tokens.

### Where secrets are safe
- Process env vars (referenced via `${VAR}`).
- `tool_context` (planner side, never sent to the LLM).
- The `TokenStore` behind your `OAuthManager`.

### Per-tenant tool secrets
For multi-tenant deployments, scope env vars per worker (one worker per tenant), or build one ToolNode per tenant with its own config. Don't encode `${TENANT_*_TOKEN}` patterns in shared config files; it's fragile and leaks tenant names into config.

## Common pitfalls

| Symptom | Cause | Fix |
|---|---|---|
| `ToolAuthError: Missing required environment variable 'X'` | Var set in your shell, not in the worker env | Set it in the process environment that runs the worker |
| `ValidationError: auth_type=... needs an MCP URL connection` | Header auth on a stdio command | Pass the credentials in `env=` with `auth_type=NONE`, or connect to the server's URL |
| Stdio server can't authenticate | Credentials expected from the host env | Pass them in `env=` |
| API key sent as `X-API-Key` | The custom header name is under another `auth_config` key | Use the `header` key |
| `${HOST}` left as text in the URL | `connection` is not substituted | Build the URL in Python before creating the config |
| Rotated token not used | MCP headers are sent only when a session opens; utcp variables are built at connect | `force_reconnect()` after rotating |
| Tokens leak into prompts | Token put in `llm_context` | Move it to env / `auth_config` / the token store; redact prompt logs |
| Cross-tenant token reuse | Token store keyed by `user_id` only | Use a tenant-scoped `user_id` (`tenant:user`) |
