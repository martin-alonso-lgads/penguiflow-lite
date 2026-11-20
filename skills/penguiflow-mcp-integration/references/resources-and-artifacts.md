# MCP Resources and Artifact Extraction

## What resources are

Many MCP servers expose **resources** in addition to tools. Resources are:
- Addressable by URI (`file://...`, custom schemes).
- Often read-heavy and cacheable.
- Useful for "browse then act" patterns: list → read → summarize → decide.

At connect, ToolNode calls `resources/list` and `resources/templates/list`. If the list call succeeds, it exposes resources as planner tools, caches reads, and stores large or binary content in the artifact store.

## Auto-generated tools

If the server answered `resources/list`, ToolNode adds:

| Tool | Args | Returns |
|---|---|---|
| `{ns}.resources_list` | none | `{"resources": [ResourceInfo dicts], "count": n}` from the list cached at connect (with `OAUTH2_USER`, the calling user's list, read from their session the first time) |
| `{ns}.resources_read` | `uri: str` | `{"result": <read result>, "uri": uri}` (see shapes below) |
| `{ns}.resources_templates_list` | none | `{"templates": [ResourceTemplateInfo dicts], "count": n}` |

They appear alongside the server's tools in `tool_node.get_tools()`, and `tool_filter` does not apply to them.

## Programmatic API

```python
from penguiflow.planner import ToolContext
from penguiflow.tools import ToolNode


async def browse(node: ToolNode, ctx: ToolContext) -> None:
    resources = await node.list_resources(refresh=False, ctx=ctx)          # list[ResourceInfo]
    templates = await node.list_resource_templates(refresh=False, ctx=ctx)  # list[ResourceTemplateInfo]
    print([r.uri for r in resources], [t.uri_template for t in templates])

    result = await node.read_resource(resources[0].uri, ctx, use_cache=True)

    async def on_update(uri: str) -> None:
        print("changed:", uri)

    subscribed = await node.subscribe_resource(resources[0].uri, on_update, ctx=ctx)  # bool
    print(result, subscribed)
    await node.unsubscribe_resource(resources[0].uri, ctx=ctx)                       # bool
```

- `refresh=True` re-asks the server; otherwise you get the lists cached at connect. Both return the cached lists (possibly empty) when the node is not connected or the server has no resources.
- With `OAUTH2_USER` each user has their own MCP session, so pass `ctx=` to `list_resources`, `list_resource_templates`, `subscribe_resource` and `unsubscribe_resource` (`read_resource` already takes `ctx`; with a shared session `ctx=` changes nothing). Without it, `list_resources` returns an empty list and the (un)subscribe calls return `False`. The resource list is per user; templates are discovered once, on the first session (`ctx=` is needed only to refresh them).
- `use_cache=False` reads from the server directly, bypassing (and not filling) the read cache.
- `ResourceInfo` fields: `uri`, `name`, `description`, `mime_type`, `size_bytes`, `annotations`. `ResourceTemplateInfo`: `uri_template`, `name`, `description`, `mime_type`.

## Read result shapes

`read_resource(...)` returns one of:

```python
{"text": "..."}                    # text up to the inline threshold
{"artifact": {"id": "...", "mime_type": "...", "size_bytes": ...}}  # ArtifactRef dict: binary, or longer text
{"error": "..."}                   # not connected, no resources support, or empty content
```

A server error during the read raises from `read_resource` (and fails the `resources_read` tool call); only with `use_cache=False` is it returned as `{"error": ...}`.

The inline threshold is `ExternalToolConfig.artifact_extraction.resources.inline_text_if_under_chars` (default `10_000` characters). Binary (`blob`) content is always stored as an artifact, under the namespace `{ns}.resource`.

For the artifact path, fetch the bytes with `await ctx.artifacts.download(result["artifact"]["id"])` (returns `bytes | None`).

## Subscriptions

Each MCP session's fastmcp client gets a message handler that routes that server's messages to the session (fastmcp's default task-status handling still runs after it). `subscribe_resource(uri, callback=None, ctx=None)` sends `resources/subscribe` through the caller's MCP session and records the callback on that session. When the server sends `notifications/resources/updated` for a URI, ToolNode:
1. invalidates that URI in that session's read cache;
2. calls the callback set with `set_resource_updated_callback(callback)`, for every updated URI;
3. calls that session's `subscribe_resource` callback of that URI, with the URI. It may be sync or async, and runs as a background task.

`ToolNode.handle_resource_updated(uri)` (and `handle_mcp_message(message)` for an update notification) does steps 1 and 3 for every session and step 2 once. Callback exceptions are logged, not raised. Other server messages are ignored by ToolNode.

Notes:
- `subscribe_resource` returns `False` when the node isn't connected, the server has no resources, or the server rejects the request; subscribing to a URI twice keeps the first callback. `unsubscribe_resource` returns `False` for a URI you didn't subscribe to.
- When the ToolNode's tools are in a `ReactPlanner` catalog, the planner owns `set_resource_updated_callback`: it installs its own callback, which emits a `resource_updated` planner event with `{"uri", "namespace"}`, and re-installs it when the catalog refreshes. Use `subscribe_resource` callbacks, or that event, for your own handling.
- A subscription belongs to its MCP session. It is dropped (and logged) when that session closes: on `close()`, on a reconnect (`force_reconnect()` or an event loop change), and when a per-user session is replaced (new token, `401`) or evicted (`session_pool`). Subscribe again afterwards; the new `subscribe_resource` call asks the server again.

## Cache behavior

Each MCP session has its own `ResourceCache`, configured from `artifact_extraction.resources`:
- In-memory and per session; it goes away when the session closes, and nothing survives a restart.
- Keyed by URI within the session. With `OAUTH2_USER` every user has their own session, so one user's inline text is never served to another. Every other auth type shares one session, and so one cache, across callers.
- Entries expire after `cache_ttl_seconds` (default `3600`; `0` = no expiry), and are invalidated earlier by an update notification. At most 1000 entries are kept, least recently used evicted first.
- Artifact entries are re-fetched when the artifact is no longer visible through `ctx.artifacts` (deleted, expired, or out of the caller's scope).
- `cache_reads_to_artifacts=False` disables the read cache: every read goes to the server.

With a shared session, inline text is shared across callers, so share one ToolNode across tenants only when its resources aren't tenant-specific. For multi-worker cache coherence, rely on subscriptions, a short `cache_ttl_seconds` or `use_cache=False`, or build a shared cache layer around the ToolNode.

## Artifact extraction pipeline

The same idea applies to tool **outputs**. Every `call()` result runs through `ExternalToolConfig.artifact_extraction`: per-tool fields, resource links, typed MCP content, binary detection, then the size safety net (`max_inline_size`, default `10_000` chars, with `auto_artifact_large_content=True`). Extracted values are replaced by `{"artifact": <ArtifactRef dict>, "summary": ..., "preview": ...}`. The knobs are listed in `external-tool-config.md`.

### `ctx.artifacts`

ToolNode stores everything through `ctx.artifacts`, a `ScopedArtifacts` facade (`penguiflow.artifacts`) that stamps `tenant_id`, `user_id`, `session_id` and `trace_id` from `tool_context` on writes and checks them on reads. Tool authors use the same facade:

```python
from penguiflow.artifacts import ArtifactRef
from penguiflow.planner import ToolContext


async def save_report(ctx: ToolContext, pdf: bytes) -> ArtifactRef:
    ref = await ctx.artifacts.upload(
        pdf,
        mime_type="application/pdf",
        filename="report.pdf",
        namespace="reports",
        meta={"source": "save_report"},
    )
    data = await ctx.artifacts.download(ref.id)  # bytes | None
    print(len(data or b""))
    return ref
```

| Method | Returns |
|---|---|
| `upload(data, *, mime_type=None, filename=None, namespace=None, meta=None)` | `ArtifactRef` (`str` data defaults to `text/plain`) |
| `download(artifact_id)` | `bytes \| None` |
| `get_metadata(artifact_id)` | `ArtifactRef \| None` |
| `list()` | `list[ArtifactRef]` in this tenant/user/session |
| `exists(artifact_id)`, `delete(artifact_id)` | `bool` |

`ctx._artifacts` is the raw `ArtifactStore`; don't use it from tool code.

The store behind the facade is the planner's: `ReactPlanner(artifact_store=...)`, or one discovered on its `state_store`. With neither, the planner uses `NoOpArtifactStore`: uploads return refs but keep no content (text keeps a 500-character preview in the ref), and a warning is logged once.

## Operational defaults

- Use a **durable `ArtifactStore`** in production; otherwise large reads and outputs are lost.
- Keep `use_cache=True` (the default) unless you have strict freshness needs; subscribe to the resources you cache.
- Prefer explicit reads over inlining: list resources, then read the specific URI.
- Set `inline_text_if_under_chars` to a size that fits comfortably in the LLM context (e.g. 4–8 KB).
- Treat resource URIs as sensitive if they encode identifiers, paths or tenant info.

## Security and multi-tenancy

- Don't let an LLM read arbitrary URIs across tenants. Use:
  - tenant-scoped MCP servers (one per tenant), or
  - tool visibility policies that filter `resources_*` per request.
- Set `tool_context["tenant_id"]`, `["user_id"]` and `["session_id"]` so artifacts are scoped.
- Make your artifact download endpoint enforce scope checks (tenant/user/session); an artifact id alone is not authorization.

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `resources_*` tools not generated | The server's `resources/list` failed at connect | Use the server's tools directly, or a resources-capable server |
| Read returns `{"error": ...}` or raises | Disconnected, bad URI, server failure, or empty content | Check the connection and auth; re-list resources for fresh URIs |
| Reads always return artifacts | Content over the inline threshold, or binary | Raise `inline_text_if_under_chars` if the size profile allows |
| Stale content | Cache hit on a changed resource | `subscribe_resource(uri, ...)`, a shorter `cache_ttl_seconds`, or `read_resource(..., use_cache=False)` |
| `subscribe_resource` returns `False` | Not connected, no resources support, the server rejected it, or `OAUTH2_USER` without `ctx=` | Check server capabilities; subscribe after `connect()`, passing `ctx=` for per-user auth |
| Subscription callback stops firing | Its MCP session closed (close, reconnect, a replaced or evicted per-user session) | Subscribe again |
| `download()` returns `None` | `NoOpArtifactStore`, expired artifact, or another scope | Configure an artifact store; check `tool_context` scope keys |
| Artifact storage unbounded | No retention policy on the store | Configure `ArtifactRetentionConfig` ([[penguiflow-statestore]]) |

## Observability

Track:
- `tool_call_*` events for `{ns}.resources_*` tools (latency, error rate).
- `artifact_stored` events when extraction fires (size distribution).
- `resource_updated` events (update rate per URI).
- Cache hit rate (application-level; `ResourceCache` has no metrics, only `size`). `node.resource_cache` is the shared session's cache (read-only; `None` before connect and with `OAUTH2_USER`).

## Worked example

```python
from penguiflow import ModelRegistry
from penguiflow.planner import ToolContext
from penguiflow.tools import ExternalToolConfig, ToolNode, TransportType


async def preview_first_file(ctx: ToolContext) -> None:
    node = ToolNode(
        config=ExternalToolConfig(
            name="filesystem",
            transport=TransportType.MCP,
            connection="npx -y @modelcontextprotocol/server-filesystem /data",
        ),
        registry=ModelRegistry(),
    )
    await node.connect()
    try:
        resources = await node.list_resources()
        for r in resources[:5]:
            print(r.uri, r.name)

        result = await node.read_resource(resources[0].uri, ctx)
        if "text" in result:
            print(result["text"][:200])
        elif "artifact" in result:
            blob = await ctx.artifacts.download(result["artifact"]["id"])
            print(len(blob or b""))
    finally:
        await node.close()
```
