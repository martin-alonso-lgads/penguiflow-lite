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
| `{ns}.resources_list` | none | `{"resources": [ResourceInfo dicts], "count": n}` from the list cached at connect |
| `{ns}.resources_read` | `uri: str` | `{"result": <read result>, "uri": uri}` (see shapes below) |
| `{ns}.resources_templates_list` | none | `{"templates": [ResourceTemplateInfo dicts], "count": n}` |

They appear alongside the server's tools in `tool_node.get_tools()`, and `tool_filter` does not apply to them.

## Programmatic API

```python
from penguiflow.planner import ToolContext
from penguiflow.tools import ToolNode


async def browse(node: ToolNode, ctx: ToolContext) -> None:
    resources = await node.list_resources(refresh=False)          # list[ResourceInfo]
    templates = await node.list_resource_templates(refresh=False)  # list[ResourceTemplateInfo]
    print([r.uri for r in resources], [t.uri_template for t in templates])

    result = await node.read_resource(resources[0].uri, ctx, use_cache=True)

    async def on_update(uri: str) -> None:
        print("changed:", uri)

    subscribed = await node.subscribe_resource(resources[0].uri, on_update)  # bool
    print(result, subscribed)
    await node.unsubscribe_resource(resources[0].uri)                       # bool
```

- `refresh=True` re-asks the server; otherwise you get the lists cached at connect. Both return the cached lists (possibly empty) when the node is not connected or the server has no resources.
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

ToolNode installs itself as the fastmcp client's message handler (fastmcp's default task-status handling still runs after it). `subscribe_resource(uri, callback=None)` sends `resources/subscribe` through the MCP session and records the callback. When the server sends `notifications/resources/updated` for a URI, `ToolNode.handle_mcp_message(message)` routes it to `handle_resource_updated(uri)`, which:
1. invalidates the read cache entry for that URI;
2. calls the callback set with `set_resource_updated_callback(callback)`, for every updated URI;
3. calls the `subscribe_resource` callback of that URI, with the URI. It may be sync or async, and runs as a background task.

Callback exceptions are logged, not raised. Other server messages are ignored by ToolNode.

Notes:
- `subscribe_resource` returns `False` when the node isn't connected, the server has no resources, or the server rejects the request; subscribing to a URI twice keeps the first callback. `unsubscribe_resource` returns `False` for a URI you didn't subscribe to.
- When the ToolNode's tools are in a `ReactPlanner` catalog, the planner owns `set_resource_updated_callback`: it installs its own callback, which emits a `resource_updated` planner event with `{"uri", "namespace"}`, and re-installs it when the catalog refreshes. Use `subscribe_resource` callbacks, or that event, for your own handling.
- Updates arrive only for URIs the server agreed to watch, and only while that MCP session is up. After `close()` or a reconnect the server stops sending them, but the ToolNode still lists the URI as subscribed, so a new `subscribe_resource(uri, ...)` returns `True` without asking the server again. To watch resources across reconnects, build a fresh ToolNode and subscribe on it.

## Cache behavior

`ResourceCache` is created on the first cached read:
- In-memory and per ToolNode; nothing survives a restart.
- Keyed by URI only, not by tenant or user.
- Inline text entries stay until invalidated by an update notification or evicted (at most 1000 entries, least recently used first). Entries don't expire by time.
- Artifact entries are re-fetched when the artifact is no longer visible through `ctx.artifacts` (deleted, expired, or out of the caller's scope).

Because inline text is shared across callers, share one ToolNode across tenants only when its resources aren't tenant-specific. For multi-worker cache coherence, rely on subscriptions or `use_cache=False`, or build a shared cache layer around the ToolNode.

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
| Stale content | Cache hit on a changed resource | `subscribe_resource(uri, ...)`, or `read_resource(..., use_cache=False)` |
| `subscribe_resource` returns `False` | Not connected, no resources support, or the server rejected it | Check server capabilities; subscribe after `connect()` |
| Subscription callback stops firing | The node reconnected or was closed, which ends the server-side subscription | Subscribe on a fresh ToolNode |
| `download()` returns `None` | `NoOpArtifactStore`, expired artifact, or another scope | Configure an artifact store; check `tool_context` scope keys |
| Artifact storage unbounded | No retention policy on the store | Configure `ArtifactRetentionConfig` ([[penguiflow-statestore]]) |

## Observability

Track:
- `tool_call_*` events for `{ns}.resources_*` tools (latency, error rate).
- `artifact_stored` events when extraction fires (size distribution).
- `resource_updated` events (update rate per URI).
- Cache hit rate (application-level; `ResourceCache` has no metrics, only `size`).

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
