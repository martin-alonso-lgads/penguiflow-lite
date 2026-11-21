# `ExternalToolConfig` reference

The full configuration surface for `ToolNode` (`penguiflow.tools.config`).

## Required fields

| Field | Type | Purpose |
|---|---|---|
| `name` | `str` | Namespace prefix. Tools surface as `{name}.{tool}`. |
| `transport` | `TransportType` | `MCP`, `HTTP`, `UTCP` or `CLI` |
| `connection` | `str` | MCP launch command or URL; HTTP/UTCP manual or base URL; CLI command. Used as given (no `${VAR}`). |

## Identity and transport fields

| Field | Default | Purpose |
|---|---|---|
| `description` | `""` | Human-readable description of the source. |
| `utcp_mode` | `UtcpMode.AUTO` | HTTP/UTCP/CLI only (see below). |
| `mcp_transport_mode` | `McpTransportMode.AUTO` | MCP URL connections: `AUTO`, `SSE` or `STREAMABLE_HTTP`. |
| `env` | `{}` | Environment for an MCP stdio subprocess, and utcp variables. Values may use `${VAR}`. |

## Auth fields

| Field | Default | Purpose |
|---|---|---|
| `auth_type` | `AuthType.NONE` | `NONE`, `API_KEY`, `BEARER`, `COOKIE` or `OAUTH2_USER` |
| `auth_config` | `{}` | `token` (BEARER), `api_key` + optional `header` (API_KEY), `cookie_name` + `cookie_value` (COOKIE). Values may use `${VAR}`. See `auth-and-env.md`. |

## Resilience fields

| Field | Default | Purpose |
|---|---|---|
| `timeout_s` | `30.0` (1–300) | Timeout of one call attempt, in seconds. |
| `max_concurrency` | `10` (1–100) | Semaphore size for parallel calls in this ToolNode, shared by all users. |
| `retry_policy` | `RetryPolicy()` | `max_attempts=3` (1–10), `wait_exponential_min_s=0.1`, `wait_exponential_max_s=5.0`. |
| `session_pool` | `SessionPoolConfig()` | Per-user MCP sessions (`OAUTH2_USER` over a URL only): `max_sessions=64` (past it, the least recently used idle session is closed) and `idle_timeout_s=900.0` (close a session unused that long; `0` = never). A session with calls in flight is never closed. |

Retries use exponential backoff between those bounds. An error that carries an HTTP status is retried only when the status is in `RetryPolicy.retry_on_status` (default `[429, 500, 502, 503, 504]`; `[]` disables status retries). Errors without a status are retried by category: timeouts (`ToolTimeoutError`) and connection errors (`ToolConnectionError`) are retried, MCP tool errors (`ToolClientError`) and auth errors are not. Each timed-out attempt waits the full `timeout_s`, so the worst case is about `timeout_s × max_attempts`.

Operational guidance:
- External SaaS: `max_concurrency=3..5`.
- Internal services: start at `10` and tune from observed throughput.
- Set `timeout_s` explicitly for each source.

## Filtering

`tool_filter: list[str] | None`, a regex allowlist applied with `re.match(pattern, tool_name)` to the **bare** server tool name (without the `{name}.` prefix). `re.match` anchors at the start only.

- `None` (default) or `[]`: expose every discovered tool.
- Read-only: `["get_", "list_", "search_"]`.
- Specific tools: `["create_issue$", "list_repositories$"]`.

The filter applies to server tools only, not to the generated `{name}.resources_*` and `{name}.prompts_*` tools. For per-request or per-tenant filtering, use `ToolVisibilityPolicy` at the planner layer instead of a `tool_filter` per tenant.

## Other fields

| Field | Default | Purpose |
|---|---|---|
| `arg_validation` | `{"emit_suspect": True}` | Planner arg-validation policy copied into each tool's `NodeSpec.extra`. |
| `prompts` | `PromptsConfig()` | `enabled` (discover MCP prompts at connect) and `generate_tools` (add `{name}.prompts_list` / `{name}.prompts_get`). |
| `apps` | `AppsConfig()` | MCP Apps: `enabled`, `fetch_html_on_call`, `default_sandbox`. |
| `artifact_extraction` | `ArtifactExtractionConfig()` | See below. |
| `output_transformer` | `None` | `(namespaced_tool_name, result, ctx) -> result` (sync or async); when set, replaces the whole extraction pipeline. Not serialized. |

## Artifact extraction

`artifact_extraction: ArtifactExtractionConfig` controls how large or binary tool outputs are moved into the artifact store (through `ctx.artifacts.upload(...)`). There is no master switch; turn layers off one by one. Layers, in the order they run:

| Layer | Knob | Behavior |
|---|---|---|
| Per-tool fields | `tool_fields: dict[str, list[ArtifactFieldConfig]]`, keyed by the namespaced tool name | Each `ArtifactFieldConfig(field_path, content_type, mime_type=None, summary_template=...)` extracts one dot-path field longer than 100 chars (base64-decoded for `pdf`/`image`/`binary`). |
| Resource links | `resources.enabled` (default `True`) | MCP `resource_link` blocks become `{"type": "artifact_stub", "resource_uri": ...}` stubs. |
| Typed MCP content | `handle_mcp_typed_content` (default `True`) | Embedded resources and `blob` fields are decoded and stored. |
| Binary detection | `binary_detection: BinaryDetectionConfig` | Strings of at least `min_size_for_detection` (1000) chars that start with a known base64 signature (PDF, PNG, JPEG, GIF, ZIP, ...) and pass the magic-byte check are stored. |
| Size safety net | `max_inline_size` (default `10_000` chars), `auto_artifact_large_content` (default `True`) | Longer strings are stored as text artifacts. |

`resources.inline_text_if_under_chars` (default `10_000`) is the inline threshold for **resource reads** (see `resources-and-artifacts.md`). `resources.cache_reads_to_artifacts` (default `True`) turns the read cache on; `False` reads from the server every time. `resources.cache_ttl_seconds` (default `3600`) is how long a cached read stays valid (`0` = until an update notification or the session closes). `default_binary_summary` and `default_text_summary` format the summaries the LLM sees.

Shapes that replace extracted values:
- Large text: `{"artifact": <ArtifactRef dict>, "summary": "...", "preview": "<first 500 chars>..."}`.
- Binary: `{"artifact": <ArtifactRef dict>, "summary": "..."}`.

Presets: `get_artifact_preset("github" | "filesystem" | "google-drive" | "tableau")` returns a ready `ArtifactExtractionConfig`; `list_artifact_presets()` lists them.

## Transport semantics

### `TransportType.MCP`
`connection` can be:
- A launch command (`npx -y @modelcontextprotocol/server-github`): split with `shlex`, then run as a stdio subprocess with `env`.
- A URL starting with `http://`, `https://`, `ws://` or `wss://`: connects to a running MCP service.

`ExternalToolConfig.is_url_connection` tells which one `connection` is.

`mcp_transport_mode` (`McpTransportMode`, imported from `penguiflow.tools.config`) for URLs:
- `AUTO` (default): with no auth headers, fastmcp infers the transport from the URL. With auth headers, ToolNode picks SSE when the URL contains `/sse`, else Streamable HTTP.
- `SSE` or `STREAMABLE_HTTP`: force one. Modern servers use Streamable HTTP.

Production recommendation: run MCP servers as managed services (Docker/k8s) and connect via URL. Avoid `npx -y ...` in long-lived workers: process lifecycle, version drift and node_modules churn cause flakiness.

### `TransportType.HTTP`
`connection` is a URL called through utcp's HTTP call template. Use for REST APIs without an MCP wrapper.

### `TransportType.UTCP`
`connection` points to a UTCP manual (a JSON manifest describing the tools) or a base URL.

### `TransportType.CLI`
`connection` is used for a utcp CLI call template.

### `utcp_mode` (HTTP / UTCP / CLI)

`utcp_mode: UtcpMode` controls how `connection` is interpreted:
- `AUTO` (default): `MANUAL_URL` when `connection` ends with `.json`, `/utcp` or `/.well-known/utcp`, else `BASE_URL`.
- `MANUAL_URL`: `connection` is a UTCP manual (recommended for full discovery).
- `BASE_URL`: `connection` is a plain endpoint; ToolNode builds one `POST` call template (`cli` type for `CLI`, `http` otherwise), with limited discovery. An `http` template carries the static auth (`BEARER`, `API_KEY` or `COOKIE`) as UTCP `api_key` auth, for the discovery request (`auth`) and the tools generated from an OpenAPI spec (`auth_tools`).

**Hard rule**: a `utcp_mode` other than `AUTO` with `transport=TransportType.MCP` raises `ValueError("utcp_mode is only valid for HTTP/UTCP transports")` when the config is built.

### Auth validation

The `validate_config` model validator enforces:
- `auth_type=BEARER` → `auth_config["token"]` must be present.
- `auth_type=API_KEY` → `auth_config["api_key"]` must be present.
- `auth_type=COOKIE` → both `auth_config["cookie_name"]` and `auth_config["cookie_value"]` must be present.
- An MCP stdio command (not a URL) → `auth_type` must be `NONE`. A subprocess gets no HTTP headers; pass credentials with `env={...}`, or connect to the server's URL.
- `auth_type=OAUTH2_USER` → only with `transport=MCP` over a URL; `UTCP`, `HTTP` and `CLI` sources can't send per-user tokens.

Violations fail at construction time (a pydantic `ValidationError`), not at first call.

## Namespacing

Tool names are `{name}.{tool}`. Example: `name="github"`, server tool `create_issue` → planner tool name `github.create_issue`.

Rules:
- Pick stable namespaces (`github`, `slides`, `slack`); they leak into prompts and logs.
- Give each ToolNode in a planner its own `name`, so tool names stay unique.
- For one vendor with several personas (`github_readonly`, `github_admin`), use distinct namespaces.

## Lifecycle

```python
from penguiflow import ModelRegistry
from penguiflow.planner import ToolContext
from penguiflow.tools import ExternalToolConfig, ToolNode


async def run_once(cfg: ExternalToolConfig, ctx: ToolContext) -> object:
    tool_node = ToolNode(config=cfg, registry=ModelRegistry())
    await tool_node.connect()             # discovers tools (and resources, prompts); resolves auth
    specs = tool_node.get_tools()         # list[NodeSpec] for the planner catalog
    print([spec.name for spec in specs])
    result = await tool_node.call("github.list_issues", {"repo": "acme/app"}, ctx)  # {"result": ...}
    await tool_node.close()               # closes every MCP session, clears discovered tools
    return result
```

Pass `specs` to your planner's catalog; the planner skill ([[penguiflow-reactplanner-config]]) handles catalog composition. `call()` accepts the namespaced or the bare tool name and returns `{"result": <output after extraction>}`. `connect()` is idempotent, and `call()` connects with its own `ctx` when the node isn't connected or was connected on another event loop. With `OAUTH2_USER`, connect with `connect(ctx)` (see `oauth-and-hitl.md`). `force_reconnect(ctx=None)` is `close()` then `connect(ctx)`. `mcp_session_count` is the number of open MCP sessions.

Presets: `POPULAR_MCP_SERVERS` and `get_preset(name)` give ready `ExternalToolConfig`s (`github`, `filesystem`, `postgres`, `slack`, `duckduckgo`, `brave-search`, `redis`), all `npx`/`uvx` stdio commands with `auth_type=NONE` and credentials as `${VAR}` placeholders in `env` (`github`: `GITHUB_PERSONAL_ACCESS_TOKEN`; `slack`: `SLACK_BOT_TOKEN`, `SLACK_TEAM_ID`; `brave-search`: `BRAVE_API_KEY`). The github, slack and brave-search presets run `@modelcontextprotocol` reference servers that npm marks as deprecated. `get_preset` returns the shared instance; customize a copy with `get_preset("postgres").model_copy(update={...})`. `ExternalToolConfig.model_copy` validates the copy like a new config, so an invalid update (for example `auth_type=BEARER` on a stdio preset) raises `ValidationError`.

## Observability hooks

ToolNode tools run as planner tools, so each call emits the usual planner events:
- `tool_call_start` and `tool_call_end`, both emitted before the tool runs (`tool_call_id`, `tool_name`, `action_seq`; `args_json` on start);
- `tool_call_result` after it finishes, with `result_json`. A failure's `result_json` holds `{"error": ..., "failure": ...}`; there is no `tool_call_error` event;
- `artifact_stored` for each upload through `ctx.artifacts`;
- `resource_updated` (`{"uri", "namespace"}`) for resource update notifications. With `OAUTH2_USER`, an update from a user's session is emitted only in a run of that user.

Events carry `ts`, so compute latency from `tool_call_start` to `tool_call_result`. Track per namespace:
- Connect duration and discovered tool count.
- Call latency p50/p95/p99.
- Error rate by tool.

For the full event schema see [[penguiflow-observability]].

## Anti-patterns
- Leaving `timeout_s` at a default you haven't checked against the source's latency.
- Unbounded `max_concurrency`: external SaaS rate-limit storms.
- No `tool_filter` in production: the planner gets every dangerous tool the server exposes.
- Sharing one ToolNode across tenants without a visibility policy: cross-tenant tool leak.
- `npx -y ...` in serverless or autoscaling workers: cold-start flakiness.
