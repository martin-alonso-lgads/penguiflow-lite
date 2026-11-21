"""ToolNode: External tool integration via MCP and UTCP protocols.

ToolNode provides a unified interface for connecting to external tool sources
(MCP servers, UTCP endpoints) and exposing their capabilities as typed
NodeSpecs compatible with PenguiFlow's planner and registry system.

Key capabilities:
- **Multi-protocol support**: MCP (stdio/SSE/streamable-http) and UTCP
  transports
- **Artifact extraction**: Multi-layer pipeline for handling binary content
  (L4: field extraction, L1: resource links, L2: MCP content blocks,
   L3: heuristic binary detection, L0: size safety net)
- **MCP Resources**: Discovery, caching, subscriptions, and generated tools
- **OAuth integration**: HITL flows for user-scoped authentication
- **Automatic retries**: Configurable retry policies with backoff
- **Tool namespacing**: Collision-free multi-source tool registration

See RFC: docs/RFC_MCP_BINARY_CONTENT_HANDLING.md for artifact handling details.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import functools
import inspect
import json
import logging
import re
import time
import warnings
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from types import GenericAlias
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, assert_never, cast

from pydantic import BaseModel, create_model

from penguiflow.catalog import NodeSpec
from penguiflow.node import Node

from .adapters import adapt_exception
from .apps import (
    UI_COMPAT_EXTENSION_IDS,
    UI_MIME_TYPE,
    AppMetadata,
    extract_app_metadata,
)
from .config import (
    ArtifactFieldConfig,
    AuthType,
    ExternalToolConfig,
    McpTransportMode,
    RetryPolicy,
    TransportType,
    UtcpMode,
)
from .errors import ToolAuthError, ToolConnectionError, ToolNodeError
from .mcp_pool import (
    SHARED_PRINCIPAL,
    McpSession,
    McpSessionPool,
    Principal,
    auth_fingerprint,
    principal_from_tool_context,
)
from .prompts import (
    PromptArgumentInfo,
    PromptInfo,
    serialize_prompt_messages,
)
from .resources import (
    ResourceCache,
    ResourceCacheConfig,
    ResourceInfo,
    ResourceSubscriptionManager,
    ResourceTemplateInfo,
)

if TYPE_CHECKING:
    from mcp.client.session import ClientSession, MessageHandlerFnT
    from mcp.shared.session import RequestResponder
    from mcp.types import (
        ClientResult,
        InitializeResult,
        ServerNotification,
        ServerRequest,
    )

    from penguiflow.planner.context import ToolContext
    from penguiflow.registry import ModelRegistry

    _McpMessage = (
        RequestResponder[ServerRequest, ClientResult]
        | ServerNotification
        | Exception
    )

logger = logging.getLogger(__name__)


def _attr_or_key(obj: object, key: str, default: object = None) -> object:
    """Read ``key`` as an attribute, or as a dict key for dict objects."""
    if hasattr(obj, key):
        return getattr(obj, key)
    if isinstance(obj, dict):
        return obj.get(key, default)
    return default


def _optional_text(value: object) -> str | None:
    """Return ``value`` as text, keeping ``None``."""
    return None if value is None else str(value)


def _container_get(
    parent: dict[str, Any] | list[Any], key: str | int
) -> object:
    """Return ``parent[key]`` when the key fits the container, else None."""
    if isinstance(parent, dict) and isinstance(key, str):
        return parent[key]
    if isinstance(parent, list) and isinstance(key, int):
        return parent[key]
    return None


def _container_set(
    parent: dict[str, Any] | list[Any], key: str | int, value: object
) -> None:
    """Set ``parent[key]`` when the key fits the container."""
    if isinstance(parent, dict):
        if isinstance(key, str):
            parent[key] = value
    elif isinstance(key, int):
        parent[key] = value


def resource_updated_uri(message: object) -> str | None:
    """Return the URI of a ``notifications/resources/updated`` message.

    Args:
        message: A message from an MCP session: a server notification, a
            server request or an exception.

    Returns:
        The updated resource's URI, or ``None`` for any other message.
    """
    import mcp.types as mcp_types

    root = getattr(message, "root", None)
    if isinstance(root, mcp_types.ResourceUpdatedNotification):
        return str(root.params.uri)
    return None


class McpMessageRouter:
    """MCP ``message_handler`` that lets a ToolNode see server messages.

    Resource update notifications go to ``on_resource_updated`` (the
    session's own cache and subscribers). Every message then goes to
    ``fallback``: the client's default handler, which fastmcp uses to
    route task status notifications.
    """

    def __init__(self, on_resource_updated: Callable[[str], None]) -> None:
        """Route resource updates to ``on_resource_updated``."""
        self._on_resource_updated = on_resource_updated
        self.fallback: MessageHandlerFnT | None = None

    async def __call__(self, message: _McpMessage) -> None:
        """Handle one server message."""
        uri = resource_updated_uri(message)
        if uri is not None:
            self._on_resource_updated(uri)
        if self.fallback is not None:
            await self.fallback(message)


def _import_fastmcp() -> tuple[Any, Any, Any, Any, Any]:
    """Return fastmcp's client, task handler and transport classes."""
    try:
        from fastmcp import Client as MCPClient
        from fastmcp.client.tasks import TaskNotificationHandler
        from fastmcp.client.transports import (
            SSETransport,
            StdioTransport,
            StreamableHttpTransport,
        )
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency hint
        msg = (
            "fastmcp is required for MCP ToolNode. Install "
            "penguiflow-lite[planner]."
        )
        raise ToolConnectionError(msg) from exc
    return (
        MCPClient,
        TaskNotificationHandler,
        SSETransport,
        StdioTransport,
        StreamableHttpTransport,
    )


def _mcp_resource_method(
    client: object, name: str, alias: str
) -> Callable[[str], Awaitable[object]] | None:
    """Find ``client``'s resource (un)subscribe method.

    ``fastmcp.Client`` has no subscribe methods, so after any method on
    the client itself this falls back to the underlying
    ``mcp.ClientSession``, which takes the URI as an ``AnyUrl``.
    """
    for attr in (name, alias):
        method = getattr(client, attr, None)
        if callable(method):
            return _resource_call(method, any_url=False)
    session_method = getattr(getattr(client, "session", None), name, None)
    if callable(session_method):
        return _resource_call(session_method, any_url=True)
    return None


def _parse_resource_info(r: object) -> ResourceInfo:
    """Parse one ``resources/list`` entry."""
    return ResourceInfo(
        uri=str(_attr_or_key(r, "uri", "")),
        name=_optional_text(_attr_or_key(r, "name")),
        description=_optional_text(_attr_or_key(r, "description")),
        mime_type=_optional_text(_attr_or_key(r, "mimeType")),
    )


def _parse_template_info(t: object) -> ResourceTemplateInfo:
    """Parse one ``resources/templates/list`` entry."""
    return ResourceTemplateInfo(
        uri_template=str(_attr_or_key(t, "uriTemplate", "")),
        name=_optional_text(_attr_or_key(t, "name")),
        description=_optional_text(_attr_or_key(t, "description")),
        mime_type=_optional_text(_attr_or_key(t, "mimeType")),
    )


def _as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _bearer_headers(token: str) -> dict[str, str]:
    """Return the headers that send an OAuth access ``token``."""
    return {"Authorization": f"Bearer {token}"}


def _is_unauthorized(exc: BaseException | None) -> bool:
    """Whether ``exc`` is an MCP server's HTTP 401 rejection."""
    if not isinstance(exc, Exception):
        return False
    adapted = adapt_exception(exc, "mcp")
    return isinstance(adapted, ToolAuthError) and adapted.status_code == 401


def _resource_call(
    method: Callable[..., object], *, any_url: bool
) -> Callable[[str], Awaitable[object]]:
    """Adapt a client's resource (un)subscribe method to take a URI.

    ``mcp.ClientSession`` methods take the URI as an ``AnyUrl``
    (``any_url=True``); other clients take a plain string.
    """

    async def call(uri: str) -> object:
        arg: object = uri
        if any_url:
            from pydantic import AnyUrl

            arg = AnyUrl(uri)
        result = method(arg)
        if inspect.isawaitable(result):
            return await result
        return result

    return call


# Private ``mcp.ClientSession`` field that ``ClientSession.initialize`` sets.
# ToolNode replaces that call to advertise the MCP Apps capability (the SDK
# has no public hook for extra client capabilities), so it sets it too.
_SESSION_SERVER_CAPABILITIES = "_server_capabilities"


class _McpClient(Protocol):
    """The parts of ``fastmcp.Client`` that MCP initialization uses."""

    @property
    def session(self) -> ClientSession:
        """The underlying MCP client session."""
        ...

    @property
    def initialize_result(self) -> InitializeResult | None:
        """The server's initialize result, once initialized."""
        ...

    async def initialize(self) -> InitializeResult:
        """Run the standard MCP initialize handshake."""
        ...


_JSON_SCALAR_TYPES: dict[str, type[Any]] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
}


def _first_typed_branch(prop_schema: dict[str, Any]) -> dict[str, Any] | None:
    """Return the first anyOf/oneOf branch with a non-null ``type``."""
    for union_key in ("anyOf", "oneOf"):
        for branch in prop_schema.get(union_key, []) or []:
            if isinstance(branch, dict) and branch.get("type") not in (
                None,
                "null",
            ):
                return branch
    return None


def _json_array_type(prop_schema: dict[str, Any]) -> type[Any] | GenericAlias:
    """Map an array schema to ``list`` of its scalar item type, if any."""
    items_type = prop_schema.get("items", {}).get("type")
    if isinstance(items_type, list):
        items_type = next((t for t in items_type if t != "null"), None)
    if items_type in _JSON_SCALAR_TYPES:
        return GenericAlias(list, (_JSON_SCALAR_TYPES[items_type],))
    return list


def _content_block_value(item: object) -> object:
    """Return a content block's text, its dump, or its string form."""
    if hasattr(item, "text"):
        return item.text
    if hasattr(item, "model_dump"):
        return item.model_dump()
    return str(item)


def _parse_json_like(value: object) -> object:
    """Parse ``value`` as JSON when it is a string that looks like JSON."""
    if isinstance(value, str) and value.strip().startswith(("{", "[")):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _item_text(item: object) -> str | None:
    """Return an item's ``text`` attribute or key as a string."""
    text = getattr(item, "text", None)
    if text is None and isinstance(item, dict):
        text = item.get("text")
    return None if text is None else str(text)


_AsyncFn = TypeVar("_AsyncFn", bound=Callable[..., Awaitable[object]])


def _retry_predicate(
    policy: RetryPolicy,
) -> Callable[[BaseException], bool]:
    """Return the retry test for ``policy``.

    A ToolNode error with a status code is retried when the code is in
    ``policy.retry_on_status``. Other ToolNode errors are retried by
    category. Raw timeouts and connection errors are always retried.
    """
    retry_statuses = frozenset(policy.retry_on_status)

    def should_retry(exc: BaseException) -> bool:
        if isinstance(exc, asyncio.CancelledError):
            return False
        if isinstance(exc, ToolNodeError):
            if exc.status_code is not None:
                return exc.status_code in retry_statuses
            return exc.is_retryable
        return isinstance(exc, (TimeoutError, ConnectionError, OSError))

    return should_retry


def _retrying(policy: RetryPolicy) -> Callable[[_AsyncFn], _AsyncFn]:
    """Return a tenacity decorator applying ``policy`` to retryable errors.

    tenacity is imported lazily to keep the dependency optional.
    """
    try:
        from tenacity import (
            retry,
            retry_if_exception,
            stop_after_attempt,
            wait_exponential,
        )
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
        msg = (
            "tenacity is required for ToolNode retries. Install "
            "penguiflow-lite[planner]."
        )
        raise RuntimeError(msg) from exc
    return retry(
        stop=stop_after_attempt(policy.max_attempts),
        wait=wait_exponential(
            min=policy.wait_exponential_min_s,
            max=policy.wait_exponential_max_s,
        ),
        retry=retry_if_exception(_retry_predicate(policy)),
        reraise=True,
    )


def _serialize_mcp_result(result: object) -> object:
    """Convert MCP CallToolResult to JSON-serializable format."""
    # If it's already a dict or primitive (except string), return as-is
    if isinstance(result, (dict, int, float, bool, type(None), list)):
        return result

    # Handle CallToolResult from fastmcp/mcp
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured

    if hasattr(result, "content"):
        # Extract text from content blocks; a single block is parsed as
        # JSON when it looks like JSON.
        texts = [_content_block_value(item) for item in result.content]
        return _parse_json_like(texts[0]) if len(texts) == 1 else texts

    if isinstance(result, str):
        return _parse_json_like(result)

    # Fallback: try model_dump for pydantic models
    if hasattr(result, "model_dump"):
        return result.model_dump()

    # Last resort: convert to string
    return str(result)


def _create_result_model(name: str) -> type[BaseModel]:
    """Create Pydantic model for tool results (permissive)."""
    return create_model(f"{name.replace('.', '_')}Result", result=(Any, None))


def _validate_magic_bytes(data: bytes, extension: str) -> bool:
    """Validate binary magic bytes match expected format.

    Args:
        data: Decoded binary data
        extension: Expected file extension

    Returns:
        True if magic bytes match, False otherwise
    """
    magic_bytes = {
        "pdf": b"%PDF",
        "png": b"\x89PNG",
        "jpeg": b"\xff\xd8\xff",
        "gif": b"GIF8",
        "zip": b"PK\x03\x04",
    }

    expected = magic_bytes.get(extension)
    if expected is None:
        return True  # No validation for unknown types

    return data.startswith(expected)


def _extract_text_from_resource(contents: object) -> str | None:
    """Extract text content from resource read result."""
    if isinstance(contents, list):
        return next(
            (
                text
                for item in contents
                if (text := _item_text(item)) is not None
            ),
            None,
        )
    text = _item_text(contents)
    if text is not None:
        return text
    # Try nested contents
    nested = (
        contents.get("contents", []) if isinstance(contents, dict) else None
    )
    if isinstance(nested, list):
        for item in nested:
            if isinstance(item, dict) and "text" in item:
                return str(item["text"])
    return None


@dataclass
class ToolNode:
    """Unified external tool integration for Penguiflow (MCP only for Phase 1).

    ToolNode connects to a single external tool source (an MCP server, or a
    UTCP/HTTP/CLI endpoint) and converts whatever tools/resources/prompts it
    exposes into Penguiflow ``NodeSpec`` objects that the planner and registry
    can consume. Callers typically construct one ``ToolNode`` per configured
    source, ``await connect()`` it, register ``get_tools()`` with the planner,
    invoke tools through ``call()``, and ``await close()`` it during shutdown.

    Attributes:
        config: Static configuration for the tool source (transport, auth,
            retry policy, artifact-extraction rules, etc.). See
            :class:`~penguiflow.tools.config.ExternalToolConfig`.
        registry: The shared :class:`~penguiflow.registry.ModelRegistry` that
            discovered tool argument/result models are registered into.
        auth_manager: Optional OAuth manager used to resolve and refresh user
            tokens for ``AuthType.OAUTH2_USER`` sources (e.g. an
            :class:`~penguiflow.tools.auth.OAuthManager`). Required whenever
            ``config.auth_type`` is ``OAUTH2_USER``; otherwise unused.
        time_source: Clock for session idle times and resource cache ages.

    For an MCP source with ``OAUTH2_USER`` auth, the node opens one MCP
    session per user (``tool_context["tenant_id"]`` and ``["user_id"]``),
    with that user's token, resource cache and subscriptions. Tools are
    discovered once, on the first session. Every other source uses one
    shared session.

    Example:
        >>> node = ToolNode(config=cfg, registry=registry)
        >>> await node.connect()
        >>> specs = node.get_tools()
        >>> await node.close()
    """

    config: ExternalToolConfig
    registry: ModelRegistry
    auth_manager: Any | None = None
    time_source: Callable[[], float] = field(
        default=time.monotonic, repr=False
    )

    utcp_client: Any | None = field(default=None, repr=False)
    tools: list[NodeSpec] = field(default_factory=list, repr=False)
    tool_name_map: dict[str, str] = field(
        default_factory=dict, repr=False
    )  # namespaced -> original
    _semaphore: asyncio.Semaphore = field(init=False, repr=False)
    connected: bool = field(default=False, repr=False)
    _connect_lock: asyncio.Lock = field(init=False, repr=False)
    connected_loop: Any | None = field(
        default=None, repr=False
    )  # Track event loop for reconnection

    _pool: McpSessionPool = field(init=False, repr=False)

    # MCP Resources support (Phase 2)
    _resources: list[ResourceInfo] = field(default_factory=list, repr=False)
    _resource_templates: list[ResourceTemplateInfo] = field(
        default_factory=list, repr=False
    )
    _resources_supported: bool = field(default=False, repr=False)
    _resource_update_callback: (
        Callable[[str, Principal | None], None] | None
    ) = field(default=None, repr=False)
    _background_tasks: set[asyncio.Task[None]] = field(
        default_factory=set, init=False, repr=False
    )

    # MCP Prompts support (Phase 3)
    _prompts: list[PromptInfo] = field(default_factory=list, repr=False)
    _prompts_supported: bool = field(default=False, repr=False)
    prompts_stale: bool = field(default=False, repr=False)

    # MCP Apps support
    # namespaced tool name -> AppMetadata
    app_metadata: dict[str, AppMetadata] = field(
        default_factory=dict, repr=False
    )

    def __post_init__(self) -> None:
        """Create the concurrency limits, locks and the session pool."""
        self._semaphore = asyncio.Semaphore(self.config.max_concurrency)
        self._connect_lock = asyncio.Lock()
        limits = self.config.session_pool
        self._pool = McpSessionPool(
            max_sessions=limits.max_sessions,
            idle_timeout_s=limits.idle_timeout_s,
            time_source=self.time_source,
        )

    # ─── MCP Sessions ────────────────────────────────────────────────────────

    @property
    def per_user_sessions(self) -> bool:
        """Whether each user gets their own MCP session (``OAUTH2_USER``)."""
        return (
            self.config.transport == TransportType.MCP
            and self.config.auth_type == AuthType.OAUTH2_USER
        )

    @property
    def mcp_client(self) -> object | None:
        """The shared MCP client, or ``None`` (always with per-user auth)."""
        session = self._pool.get(SHARED_PRINCIPAL)
        return None if session is None else session.client

    @mcp_client.setter
    def mcp_client(self, client: object | None) -> None:
        """Install ``client`` as the shared session, or drop it with None.

        Raises:
            ToolNodeError: With per-user auth, where no session is shared.
        """
        if self.per_user_sessions:
            msg = (
                f"ToolNode '{self.config.name}' uses per-user MCP "
                "sessions; there is no shared mcp_client"
            )
            raise ToolNodeError(msg)
        if client is None:
            self._pool.discard(SHARED_PRINCIPAL)
            return
        session = self._new_session(SHARED_PRINCIPAL, client)
        session.loop = _running_loop()
        self._pool.install(session)

    @property
    def resource_cache(self) -> ResourceCache | None:
        """The shared session's resource cache, or ``None``."""
        session = self._pool.get(SHARED_PRINCIPAL)
        return None if session is None else session.cache

    @property
    def mcp_session_count(self) -> int:
        """Number of open MCP sessions (shared and per-user)."""
        return len(self._pool)

    def _new_resource_cache(self) -> ResourceCache:
        resources = self.config.artifact_extraction.resources
        return ResourceCache(
            namespace=self.config.name,
            config=ResourceCacheConfig(
                enabled=resources.cache_reads_to_artifacts,
                ttl_seconds=resources.cache_ttl_seconds,
                inline_text_if_under_chars=(
                    resources.inline_text_if_under_chars
                ),
            ),
            time_source=self.time_source,
        )

    def _new_session(self, principal: Principal, client: object) -> McpSession:
        return McpSession(
            principal=principal,
            client=client,
            cache=self._new_resource_cache(),
            subscriptions=ResourceSubscriptionManager(self.config.name),
        )

    def _principal_for(self, ctx: ToolContext | None) -> Principal:
        """Return the session key for a call made with ``ctx``.

        Raises:
            ToolAuthError: With per-user auth, when ``ctx`` is missing or
                has no ``user_id``.
        """
        if not self.per_user_sessions:
            return SHARED_PRINCIPAL
        if ctx is None:
            msg = (
                f"ToolNode '{self.config.name}' requires HITL "
                "OAuth but no ToolContext provided. "
                "Call connect(ctx) with a ToolContext containing user_id."
            )
            raise ToolAuthError(msg)
        principal = principal_from_tool_context(ctx.tool_context)
        if principal is None:
            msg = "user_id required in tool_context for OAuth"
            raise ToolAuthError(msg)
        return principal

    async def _session_for(
        self,
        ctx: ToolContext | None,
        headers: dict[str, str] | None = None,
    ) -> McpSession:
        """Return the open session for ``ctx``, opening it when needed.

        With per-user auth this resolves the user's token first, which may
        pause the run for OAuth consent. A token change opens a new
        session.
        """
        principal = self._principal_for(ctx)
        if headers is None:
            headers = (
                await self._resolve_user_oauth(ctx)
                if ctx is not None and self.per_user_sessions
                else self._get_static_auth_headers()
            )
        fingerprint = auth_fingerprint(headers)
        try:
            return await self._pool.acquire(
                principal,
                fingerprint,
                functools.partial(self._open_session, principal, headers),
            )
        except ToolConnectionError as exc:
            if self.per_user_sessions and _is_unauthorized(exc.__cause__):
                await self._forget_token(principal, fingerprint)
            raise

    async def _forget_token(
        self, principal: Principal, fingerprint: str
    ) -> None:
        """Delete the user's stored token when the server rejected it.

        Only the token the rejected session was opened with is deleted, so
        a token stored since (after a new consent) is kept. The user's next
        call then asks for consent again instead of reusing the token.

        Args:
            principal: The user whose session was rejected.
            fingerprint: :func:`auth_fingerprint` of the rejected headers.
        """
        if self.auth_manager is None:
            return
        user_id = principal[1]
        token = await self.auth_manager.get_token(user_id, self.config.name)
        if token is None:
            return
        if auth_fingerprint(_bearer_headers(token)) != fingerprint:
            return
        await self.auth_manager.token_store.delete(user_id, self.config.name)

    async def _open_session(
        self, principal: Principal, headers: dict[str, str]
    ) -> McpSession:
        session = self._new_session(principal, None)
        router = McpMessageRouter(
            functools.partial(self._on_session_update, session)
        )
        session.client = await self.open_mcp_client(headers, router)
        return session

    @contextlib.asynccontextmanager
    async def _lease(
        self,
        ctx: ToolContext | None,
        headers: dict[str, str] | None = None,
    ) -> AsyncGenerator[McpSession, None]:
        """Use ``ctx``'s session for the block.

        With per-user auth, an auth failure closes the session. An HTTP 401
        also deletes the user's token, so the next call asks for consent
        again and opens a new session with the new token.
        """
        session = await self._session_for(ctx, headers)
        async with self._pool.lease(session):
            try:
                yield session
            except ToolAuthError as exc:
                if self.per_user_sessions and exc.status_code in (None, 401):
                    self._pool.retire(session)
                    if exc.status_code == 401:
                        await self._forget_token(
                            session.principal, session.fingerprint
                        )
                raise

    @contextlib.asynccontextmanager
    async def _optional_lease(
        self, ctx: ToolContext | None
    ) -> AsyncGenerator[McpSession | None, None]:
        """Use ``ctx``'s session, or yield ``None`` when there is none.

        The shared session is never opened here; a per-user session is
        opened on first use.
        """
        if self.per_user_sessions:
            if ctx is None or not ctx.tool_context.get("user_id"):
                yield None
                return
            async with self._lease(ctx) as session:
                yield session
            return
        shared = self._pool.get(SHARED_PRINCIPAL)
        if shared is None:
            yield None
            return
        async with self._pool.lease(shared):
            yield shared

    def _on_session_update(self, session: McpSession, uri: str) -> None:
        """Handle a resource update one session's server sent.

        The update callback gets the session's user, or ``None`` for the
        shared session.
        """
        self._invalidate_session(session, uri)
        principal = session.principal
        self._fire_resource_callback(
            uri, None if principal == SHARED_PRINCIPAL else principal
        )

    def _invalidate_session(self, session: McpSession, uri: str) -> None:
        """Drop ``uri`` from ``session``'s cache and notify its subscribers."""
        session.cache.invalidate(uri)
        loop = _running_loop()
        if loop is None:
            return
        task = loop.create_task(session.subscriptions.handle_update(uri))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _fire_resource_callback(
        self, uri: str, principal: Principal | None
    ) -> None:
        if self._resource_update_callback is None:
            return
        try:
            self._resource_update_callback(uri, principal)
        except Exception:
            logger.debug("resource_update_callback_failed", exc_info=True)

    async def open_mcp_client(
        self, auth_headers: dict[str, str], router: McpMessageRouter
    ) -> object:
        """Open and initialize one fastmcp client with ``auth_headers``.

        Override or patch this to supply a different MCP client.

        Args:
            auth_headers: HTTP headers for a URL connection (stdio
                connections take none).
            router: The client's message handler.

        Returns:
            The connected, initialized client.

        Raises:
            ToolConnectionError: If the client cannot connect.
        """
        client_cls, handler_cls, *transport_classes = _import_fastmcp()
        client: Any = None
        try:
            transport = self._build_mcp_transport(
                auth_headers, *transport_classes
            )
            client = client_cls(
                transport, auto_initialize=False, message_handler=router
            )
            # Keep fastmcp's default handler so task status still routes.
            router.fallback = handler_cls(client)
            await client.__aenter__()
            await self.initialize_mcp_client(client)
        except Exception as exc:
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.__aexit__(type(exc), exc, exc.__traceback__)
            msg = (
                "Failed to connect to MCP tool source "
                f"'{self.config.name}': {exc}"
            )
            raise ToolConnectionError(msg) from exc
        return client

    def _build_mcp_transport(
        self,
        auth_headers: dict[str, str],
        sse_transport_cls: type[Any],
        stdio_transport_cls: type[Any],
        streamable_http_transport_cls: type[Any],
    ) -> object:
        """Return the fastmcp transport for ``config.connection``."""
        if self.config.is_url_connection:
            return self._resolve_mcp_url_transport(
                self.config.connection,
                auth_headers,
                sse_transport_cls,
                streamable_http_transport_cls,
            )
        import shlex

        parts = shlex.split(self.config.connection)
        return stdio_transport_cls(
            command=parts[0], args=parts[1:], env=self._stdio_env()
        )

    def _stdio_env(self) -> dict[str, str] | None:
        """Return ``config.env`` with ``${VAR}`` placeholders filled in."""
        if not self.config.env:
            return None
        env = self.config.env
        return {key: self.substitute_env(val) for key, val in env.items()}

    def _is_connected_on(self, loop: asyncio.AbstractEventLoop) -> bool:
        """Return whether the client is connected on ``loop``."""
        return self.connected and self.connected_loop is loop

    async def connect(self, ctx: ToolContext | None = None) -> None:
        """Connect to tool source and discover available tools.

        Idempotent: if already connected on the current event loop this is a
        no-op. If the connection was established on a different event loop
        (e.g. the ToolNode was built before an async server started), this
        transparently reconnects on the current loop. On success, discovered
        tools (and, for MCP transports, resources and prompts) are populated
        and registered into ``self.registry``.

        With ``OAUTH2_USER`` auth this opens ``ctx``'s user's session; other
        users' sessions open on their first call.

        Args:
            ctx: Optional ToolContext for HITL OAuth during connection.
                 Required if auth_type is OAUTH2_USER.

        Raises:
            ToolAuthError: If ``auth_type`` is ``OAUTH2_USER`` and ``ctx`` is
                not provided, or the OAuth flow does not yield a token.
            ToolConnectionError: If the transport is unsupported or the
                underlying MCP/UTCP client fails to connect.
            ToolNodeError: If a discovered tool name collides with an
                already-registered name in ``self.registry``.
        """
        current_loop = asyncio.get_running_loop()
        if (
            self.connected
            and self.connected_loop is not None
            and self.connected_loop is not current_loop
        ):
            # Connected on a different event loop: its clients can't run
            # here, so drop them and connect again.
            self._detach_from_loop()

        if self.connected:
            return

        async with self._connect_lock:
            # Another task may have connected while this one awaited the lock.
            if self._is_connected_on(current_loop):
                return

            self._reset_discovery_state()
            if self.config.transport == TransportType.MCP:
                await self._connect_mcp(ctx)
            elif self.config.transport in {
                TransportType.HTTP,
                TransportType.UTCP,
                TransportType.CLI,
            }:
                await self._connect_utcp()
            else:
                msg = (
                    f"Transport '{self.config.transport.value}' not supported"
                )
                raise ToolConnectionError(msg)

            self.connected = True
            self.connected_loop = current_loop

    def _reset_discovery_state(self) -> None:
        """Forget discovered tools, resources, prompts and app metadata."""
        self.tools = []
        self.tool_name_map.clear()
        self.app_metadata.clear()
        self._resources = []
        self._resource_templates = []
        self._resources_supported = False
        self._prompts = []
        self._prompts_supported = False
        self.prompts_stale = False

    def _detach_from_loop(self) -> None:
        """Drop clients and loop-bound locks after an event loop change."""
        self._pool.detach()
        self.utcp_client = None
        self._connect_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(self.config.max_concurrency)
        self.connected = False
        self.connected_loop = None

    async def _connect_mcp(self, ctx: ToolContext | None) -> None:
        """Open ``ctx``'s MCP session and discover tools on it.

        Args:
            ctx: The caller's context; needed for per-user auth.
        """
        session = await self._session_for(ctx)
        try:
            mcp_tools = await session.client.list_tools()
        except Exception as exc:
            self._pool.retire(session)
            msg = (
                "Failed to connect to MCP tool source "
                f"'{self.config.name}': {exc}"
            )
            raise ToolConnectionError(msg) from exc

        self.tools = self.convert_mcp_tools(mcp_tools)
        await self.discover_mcp_resources(session)
        await self.discover_mcp_prompts(session)

    async def initialize_mcp_client(
        self, client: _McpClient
    ) -> InitializeResult:
        """Initialize an MCP client, advertising Apps support when enabled."""
        if not self.config.apps.enabled:
            return await client.initialize()

        import anyio
        import mcp.types as mcp_types
        from mcp.client.session import (
            _default_elicitation_callback,
            _default_list_roots_callback,
            _default_sampling_callback,
        )
        from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS

        existing = client.initialize_result
        if existing is not None:
            return existing

        session = client.session

        sampling = (
            (
                getattr(session, "_sampling_capabilities", None)
                or mcp_types.SamplingCapability()
            )
            if getattr(session, "_sampling_callback", None)
            is not _default_sampling_callback
            else None
        )
        elicitation = (
            mcp_types.ElicitationCapability(
                form=mcp_types.FormElicitationCapability(),
                url=mcp_types.UrlElicitationCapability(),
            )
            if getattr(session, "_elicitation_callback", None)
            is not _default_elicitation_callback
            else None
        )
        roots = (
            mcp_types.RootsCapability.model_validate({"listChanged": True})
            if getattr(session, "_list_roots_callback", None)
            is not _default_list_roots_callback
            else None
        )
        task_handlers = getattr(session, "_task_handlers", None)
        tasks = (
            task_handlers.build_capability()
            if task_handlers is not None
            else None
        )

        app_capability = {"mimeTypes": [UI_MIME_TYPE]}
        app_extensions = {
            extension_id: dict(app_capability)
            for extension_id in UI_COMPAT_EXTENSION_IDS
        }

        capabilities = mcp_types.ClientCapabilities.model_validate(
            {
                "sampling": sampling,
                "elicitation": elicitation,
                "experimental": dict(app_extensions),
                "roots": roots,
                "tasks": tasks,
                "extensions": dict(app_extensions),
            }
        )
        client_info = cast(
            "mcp_types.Implementation", getattr(session, "_client_info", None)
        )

        request = mcp_types.ClientRequest(
            root=mcp_types.InitializeRequest(
                params=mcp_types.InitializeRequestParams.model_validate(
                    {
                        "protocolVersion": mcp_types.LATEST_PROTOCOL_VERSION,
                        "capabilities": capabilities,
                        "clientInfo": client_info,
                    }
                ),
            ),
        )

        timeout = getattr(client, "_init_timeout", None)
        request_coro = session.send_request(
            request, mcp_types.InitializeResult
        )
        # Neither fastmcp nor the MCP SDK exposes a public hook for extra
        # client capabilities, so this mirrors ``Client.initialize`` and
        # touches the same optional internals it does, when present.
        monitor = getattr(client, "_await_with_session_monitoring", None)
        send_request: Awaitable[InitializeResult] = (
            monitor(request_coro) if callable(monitor) else request_coro
        )

        if timeout is None:
            result = await send_request
        else:
            try:
                with anyio.fail_after(timeout):
                    result = await send_request
            except TimeoutError as exc:
                msg = "Failed to initialize server session"
                raise RuntimeError(msg) from exc

        if result.protocolVersion not in SUPPORTED_PROTOCOL_VERSIONS:
            msg = (
                "Unsupported protocol version from the server: "
                f"{result.protocolVersion}"
            )
            raise RuntimeError(msg)

        session_state = getattr(client, "_session_state", None)
        if session_state is not None:
            session_state.initialize_result = result
        if hasattr(session, _SESSION_SERVER_CAPABILITIES):
            setattr(session, _SESSION_SERVER_CAPABILITIES, result.capabilities)

        await session.send_notification(
            mcp_types.ClientNotification(
                root=mcp_types.InitializedNotification()
            )
        )
        return result

    async def discover_mcp_resources(
        self, session: McpSession | None = None
    ) -> None:
        """Discover MCP resources and templates.

        Best-effort: doesn't fail if server doesn't support resources. With
        per-user sessions the resource list is user data, so it is kept on
        ``session`` instead of the node.

        Args:
            session: The session to ask; defaults to the shared session.
        """
        session = session or self._pool.get(SHARED_PRINCIPAL)
        if session is None:
            return

        try:
            resources = [
                _parse_resource_info(r)
                for r in _as_list(await session.client.list_resources())
            ]
            self._resources_supported = True
            if self.per_user_sessions:
                session.resources = resources
            else:
                self._resources = resources
            logger.debug(
                "Discovered %d MCP resources for '%s'",
                len(resources),
                self.config.name,
            )
        except Exception as e:
            # Server doesn't support resources - that's fine
            logger.debug(
                "MCP resources not supported by '%s': %s",
                self.config.name,
                e,
            )
            self._resources = []
            self._resources_supported = False

        await self._discover_resource_templates(session)

        # Generate planner tools for resources if supported
        if self._resources_supported:
            resource_tools = self.generate_resource_tools()
            self.tools.extend(resource_tools)

    async def _discover_resource_templates(self, session: McpSession) -> None:
        try:
            self._resource_templates = [
                _parse_template_info(t)
                for t in _as_list(
                    await session.client.list_resource_templates()
                )
            ]
            logger.debug(
                "Discovered %d MCP resource templates for '%s'",
                len(self._resource_templates),
                self.config.name,
            )
        except Exception as e:
            logger.debug(
                "MCP resource templates not supported by '%s': %s",
                self.config.name,
                e,
            )
            self._resource_templates = []

    @staticmethod
    def _parse_prompt_info(p: object) -> PromptInfo:
        """Parse a single MCP prompt object into PromptInfo."""
        raw_args = _attr_or_key(p, "arguments") or []
        arguments = [
            PromptArgumentInfo(
                name=str(_attr_or_key(a, "name", "")),
                description=_optional_text(_attr_or_key(a, "description")),
                required=bool(_attr_or_key(a, "required", False)),
            )
            for a in (raw_args if isinstance(raw_args, (list, tuple)) else [])
        ]
        return PromptInfo(
            name=str(_attr_or_key(p, "name", "")),
            description=_optional_text(_attr_or_key(p, "description")),
            arguments=arguments,
        )

    async def discover_mcp_prompts(
        self, session: McpSession | None = None
    ) -> None:
        """Discover MCP prompts.

        Best-effort: doesn't fail if server doesn't support prompts.

        Args:
            session: The session to ask; defaults to the shared session.
        """
        session = session or self._pool.get(SHARED_PRINCIPAL)
        if session is None:
            return

        if not self.config.prompts.enabled:
            self._prompts = []
            self._prompts_supported = False
            self.prompts_stale = False
            return

        try:
            self._prompts = [
                self._parse_prompt_info(p)
                for p in _as_list(await session.client.list_prompts())
            ]
            self._prompts_supported = True
            self.prompts_stale = False
            logger.debug(
                "Discovered %d MCP prompts for '%s'",
                len(self._prompts),
                self.config.name,
            )
        except Exception as e:
            logger.debug(
                "MCP prompts not supported by '%s': %s",
                self.config.name,
                e,
            )
            self._prompts = []
            self._prompts_supported = False
            self.prompts_stale = False

        # Generate planner tools for prompts if supported
        if self._prompts_supported and self.config.prompts.generate_tools:
            prompt_tools = self.generate_prompt_tools()
            self.tools.extend(prompt_tools)

    def _resolve_mcp_url_transport(
        self,
        connection: str,
        auth_headers: dict[str, str] | None,
        sse_transport_cls: type[Any],
        streamable_http_transport_cls: type[Any],
    ) -> object:
        """Resolve the appropriate MCP transport for URL-based connections.

        Transport selection logic:
        1. If mcp_transport_mode is explicitly set (SSE or STREAMABLE_HTTP),
           use that
        2. For AUTO mode without auth headers, let FastMCP auto-detect (pass
           URL string)
        3. For AUTO mode with auth headers, detect from URL pattern:
           - /sse → SSE transport
           - /mcp or other → StreamableHTTP (modern default)

        Args:
            connection: The URL to connect to
            auth_headers: Auth headers if any
            sse_transport_cls: SSETransport class from fastmcp
            streamable_http_transport_cls: StreamableHttpTransport class from
                fastmcp

        Returns:
            Transport object or URL string for FastMCP client
        """
        mode = self.config.mcp_transport_mode

        # Explicit SSE mode
        if mode == McpTransportMode.SSE:
            return sse_transport_cls(
                url=connection, headers=auth_headers or {}
            )

        # Explicit StreamableHTTP mode
        if mode == McpTransportMode.STREAMABLE_HTTP:
            return streamable_http_transport_cls(
                url=connection, headers=auth_headers or {}
            )

        # AUTO mode
        if not auth_headers:
            # No auth headers - let FastMCP auto-detect from URL
            return connection

        # AUTO mode with auth headers - need to construct transport explicitly
        # Detect from URL pattern
        if "/sse" in connection.lower():
            return sse_transport_cls(url=connection, headers=auth_headers)

        # Default to StreamableHTTP (modern MCP standard)
        return streamable_http_transport_cls(
            url=connection, headers=auth_headers
        )

    async def _connect_utcp(self) -> None:
        """Connect via UTCP client (manual_url or base_url)."""
        try:
            from utcp import UtcpClient
        except (
            ModuleNotFoundError
        ) as exc:  # pragma: no cover - dependency hint
            msg = (
                "utcp is required for UTCP ToolNode. Install "
                "penguiflow-lite[planner]."
            )
            raise ToolConnectionError(
                msg,
            ) from exc

        config = self.build_utcp_config()
        try:
            self.utcp_client = await UtcpClient.create(config=config)
            utcp_tools = await self.utcp_client.list_tools()
        except Exception as exc:
            if self.utcp_client:
                with contextlib.suppress(Exception):
                    await self.utcp_client.aclose()
                self.utcp_client = None
            msg = (
                "Failed to connect to UTCP tool source "
                f"'{self.config.name}': {exc}"
            )
            raise ToolConnectionError(
                msg,
            ) from exc

        self.tools = self.convert_utcp_tools(utcp_tools)

    def get_tools(self) -> list[NodeSpec]:
        """Return discovered tools as Penguiflow NodeSpec entries.

        Note: connect() must be called before this method.

        Returns:
            The list of :class:`~penguiflow.catalog.NodeSpec` discovered
            during ``connect()`` (including any generated resource/prompt
            tools). Empty if ``connect()`` has not been called yet.
        """
        return self.tools

    def get_tool_specs(self) -> list[NodeSpec]:
        """Alias for get_tools for compatibility with generators.

        Returns:
            Same value as :meth:`get_tools`.
        """
        return self.get_tools()

    async def call(
        self,
        tool_name: str,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> object:
        """Execute a tool with auth resolution and resilience.

        Connects (with ``ctx``) if the node is not connected or was
        connected on a different event loop, resolves auth headers (pausing for
        HITL OAuth via ``ctx`` if needed), invokes the underlying MCP/UTCP tool
        with retries per ``config.retry_policy``, and runs the result through
        the layered artifact-extraction pipeline (and, for MCP Apps-enabled
        tools, fetches and attaches the app UI resource). With
        ``OAUTH2_USER`` auth the call goes through the caller's own MCP
        session, opened with their token.

        Args:
            tool_name: Namespaced tool name (e.g. ``"{source}.{tool}"``) or
                the bare original tool name; both are accepted.
            args: Tool call arguments as a plain dict (already validated
                against the tool's args model by the caller/planner).
            ctx: ToolContext for the current run, used to resolve auth, pause
                for HITL flows, and access the artifact store.

        Returns:
            A dict of the form ``{"result": <transformed_output>}`` where
            ``<transformed_output>`` has had binary/oversized content
            replaced by artifact references.

        Raises:
            ToolAuthError: If OAuth is required but cannot be resolved.
            ToolNodeError: If no client is available or the call fails after
                exhausting retries.
        """
        await self._ensure_connected(ctx)

        async with self._semaphore:
            auth_headers = await self.resolve_auth(ctx)
            original_name = self._original_tool_name(tool_name)
            app_meta = self._call_app_metadata(tool_name)

            raw_result = await self._invoke(
                original_name,
                args,
                auth_headers,
                ctx,
                raw_mcp=app_meta is not None,
            )
            if self.config.transport == TransportType.MCP:
                result = self._serialize_mcp_result_for_planner(
                    raw_result,
                    prefer_text_content=app_meta is not None,
                )
            else:
                result = raw_result
            # Transform output through layered artifact extraction
            transformed = await self.transform_output(
                original_name, result, ctx
            )

            # MCP Apps: if tool has an app, fetch the UI resource and package
            # it
            if app_meta is not None:
                transformed = await self.enrich_with_app_html(
                    transformed,
                    app_meta,
                    args,
                    ctx,
                    raw_tool_result=raw_result,
                )

            # Wrap result to match the output model schema: {"result": <data>}
            return {"result": transformed}

    async def _ensure_connected(self, ctx: ToolContext) -> None:
        """Connect with ``ctx`` unless connected on the running loop."""
        current_loop = asyncio.get_running_loop()
        loop_changed = (
            self.connected_loop is not None
            and self.connected_loop is not current_loop
        )
        if not self.connected or loop_changed:
            await self.connect(ctx)

    def _original_tool_name(self, tool_name: str) -> str:
        original_name = self.tool_name_map.get(tool_name)
        if original_name is None:
            original_name = tool_name.removeprefix(f"{self.config.name}.")
        return original_name

    def _call_app_metadata(self, tool_name: str) -> AppMetadata | None:
        if self.config.apps.enabled and self.config.apps.fetch_html_on_call:
            return self.app_metadata.get(tool_name)
        return None

    async def _invoke(
        self,
        tool_name: str,
        args: dict[str, Any],
        auth_headers: dict[str, str],
        ctx: ToolContext,
        *,
        raw_mcp: bool,
    ) -> object:
        """Call the tool through ``ctx``'s MCP session, or through UTCP.

        ``auth_headers`` pick the caller's MCP session: a per-user session
        opened with other headers is replaced.
        """
        if self.config.transport != TransportType.MCP:
            return await self.call_with_retry(tool_name, args, raw_mcp=raw_mcp)
        async with self._lease(ctx, auth_headers) as session:
            return await self.call_with_retry(
                tool_name, args, raw_mcp=raw_mcp, session=session
            )

    async def close(self) -> None:
        """Clean up resources.

        Marks the node disconnected, clears discovered state, and closes
        every MCP session (each one's cache and subscriptions go with it)
        and the UTCP client. Exceptions during client teardown, e.g. from a
        stale event loop, are swallowed. Safe to call multiple times.
        """
        self.connected = False
        self.connected_loop = None
        self._reset_discovery_state()
        await self._pool.close_all()
        if self.utcp_client:
            # Best effort
            with contextlib.suppress(Exception):
                await self.utcp_client.aclose()
            self.utcp_client = None
        if self._background_tasks:
            await asyncio.gather(
                *self._background_tasks, return_exceptions=True
            )

    async def force_reconnect(self, ctx: ToolContext | None = None) -> None:
        """Close every session and connect again on the running loop.

        Subscriptions do not carry over: subscribe again after this.

        Args:
            ctx: The caller's context; needed for per-user auth.
        """
        await self.close()
        await self.connect(ctx)

    # ─── Auth Resolution ─────────────────────────────────────────────────────

    def _get_static_auth_headers(self) -> dict[str, str]:
        """Get static BEARER/API_KEY/COOKIE headers for connection-time auth.

        Returns empty dict for NONE or OAUTH2_USER (which requires
        ToolContext).
        """
        if self.config.auth_type == AuthType.API_KEY:
            key = self.substitute_env(
                str(self.config.auth_config.get("api_key", ""))
            )
            header = self.config.auth_config.get("header", "X-API-Key")
            return {str(header): key}

        if self.config.auth_type == AuthType.BEARER:
            token = self.substitute_env(
                str(self.config.auth_config.get("token", ""))
            )
            return {"Authorization": f"Bearer {token}"}

        if self.config.auth_type == AuthType.COOKIE:
            cookie_name = self.substitute_env(
                str(self.config.auth_config.get("cookie_name", ""))
            )
            cookie_value = self.substitute_env(
                str(self.config.auth_config.get("cookie_value", ""))
            )
            return {"Cookie": f"{cookie_name}={cookie_value}"}

        return {}

    async def resolve_auth(self, ctx: ToolContext) -> dict[str, str]:
        """Resolve authentication headers, pausing for OAuth if needed."""
        if self.config.auth_type == AuthType.NONE:
            return {}

        if self.config.auth_type == AuthType.API_KEY:
            key = self.substitute_env(
                str(self.config.auth_config.get("api_key", ""))
            )
            header = self.config.auth_config.get("header", "X-API-Key")
            return {str(header): key}

        if self.config.auth_type == AuthType.BEARER:
            token = self.substitute_env(
                str(self.config.auth_config.get("token", ""))
            )
            return {"Authorization": f"Bearer {token}"}

        if self.config.auth_type == AuthType.COOKIE:
            cookie_name = self.substitute_env(
                str(self.config.auth_config.get("cookie_name", ""))
            )
            cookie_value = self.substitute_env(
                str(self.config.auth_config.get("cookie_value", ""))
            )
            return {"Cookie": f"{cookie_name}={cookie_value}"}

        if self.config.auth_type == AuthType.OAUTH2_USER:
            return await self._resolve_user_oauth(ctx)

        assert_never(self.config.auth_type)

    async def _resolve_user_oauth(self, ctx: ToolContext) -> dict[str, str]:
        """Handle user-level OAuth with HITL pause/resume."""
        if not self.auth_manager:
            msg = (
                f"ToolNode '{self.config.name}' requires user OAuth but "
                "no auth_manager was provided"
            )
            raise ToolAuthError(
                msg,
            )

        user_id = ctx.tool_context.get("user_id")
        if not user_id:
            msg = "user_id required in tool_context for OAuth"
            raise ToolAuthError(msg)

        token = await self.auth_manager.get_token(user_id, self.config.name)
        if token:
            return _bearer_headers(token)

        trace_id = ctx.tool_context.get("trace_id", "")
        auth_request = self.auth_manager.get_auth_request(
            provider=self.config.name,
            user_id=user_id,
            trace_id=trace_id,
        )

        await ctx.pause(
            reason="external_event",
            payload={
                "pause_type": "oauth",
                "provider": self.config.name,
                **auth_request,
            },
        )

        token = await self.auth_manager.get_token(user_id, self.config.name)
        if not token:
            msg = f"OAuth for {self.config.name} was not completed"
            raise ToolAuthError(msg)

        return _bearer_headers(token)

    # ─── Resilience ──────────────────────────────────────────────────────────

    async def call_with_retry(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        raw_mcp: bool = False,
        session: McpSession | None = None,
    ) -> object:
        """Execute tool call with intelligent retry based on error category.

        Auth is not passed here: an MCP session carries the headers it was
        opened with, and UTCP credentials are client variables.

        Args:
            tool_name: The tool's name on the server.
            args: Tool call arguments.
            raw_mcp: Return the raw MCP result instead of a serialized one.
            session: The MCP session to call through; defaults to the
                shared session. With neither, the call goes through UTCP.

        Returns:
            The tool's result.
        """
        session = session or self._pool.get(SHARED_PRINCIPAL)
        transport = "mcp" if session is not None else "utcp"

        @_retrying(self.config.retry_policy)
        async def _execute() -> object:
            try:
                async with asyncio.timeout(self.config.timeout_s):
                    return await self._call_tool_once(
                        tool_name, args, session, raw_mcp=raw_mcp
                    )
            except (asyncio.CancelledError, ToolNodeError):
                raise
            except Exception as exc:  # pragma: no cover - wrapped in adapter
                raise adapt_exception(exc, transport) from exc

        return await _execute()

    async def _call_tool_once(
        self,
        tool_name: str,
        args: dict[str, Any],
        session: McpSession | None,
        *,
        raw_mcp: bool,
    ) -> object:
        """Call the tool once through the MCP session or UTCP client."""
        if session is not None:
            client = session.client
            if raw_mcp:
                call_tool_mcp = getattr(client, "call_tool_mcp", None)
                if callable(call_tool_mcp):
                    return await call_tool_mcp(tool_name, args)
            result = await client.call_tool(tool_name, args)
            return result if raw_mcp else _serialize_mcp_result(result)
        if self.utcp_client:
            return await self.call_utcp_tool(tool_name, args)
        msg = "No client available for tool execution"
        raise ToolNodeError(msg)

    def _serialize_mcp_result_for_planner(
        self,
        result: object,
        *,
        prefer_text_content: bool = False,
    ) -> object:
        """Serialize MCP results for planner context.

        For app-enabled tools, prefer the human-facing text content over
        structuredContent so the planner does not ingest large app state blobs.
        The raw MCP payload is still preserved separately for the app runtime.
        """
        if prefer_text_content and hasattr(result, "content"):
            texts: list[Any] = []
            for item in result.content:
                if hasattr(item, "text"):
                    texts.append(item.text)
                elif hasattr(item, "model_dump"):
                    texts.append(item.model_dump())
                else:
                    texts.append(str(item))

            if texts:
                if len(texts) == 1:
                    text = texts[0]
                    if isinstance(text, str) and text.strip().startswith(
                        ("{", "[")
                    ):
                        try:
                            return json.loads(text)
                        except json.JSONDecodeError:
                            return text
                    return text
                return texts

        return _serialize_mcp_result(result)

    async def call_utcp_tool(
        self, tool_name: str, args: dict[str, Any]
    ) -> object:
        """Invoke a UTCP tool.

        The UTCP client takes no per-call headers: static credentials reach
        it as variables (see :meth:`build_utcp_variables`).
        """
        if not self.utcp_client:
            msg = "UTCP client not initialised"
            raise ToolNodeError(msg)
        return await self.utcp_client.call_tool(tool_name, args)

    # ─── Tool Conversion ─────────────────────────────────────────────────────

    def convert_mcp_tools(self, mcp_tools: list[Any]) -> list[NodeSpec]:
        """Convert MCP tool schemas to Penguiflow NodeSpec."""
        specs: list[NodeSpec] = []
        for tool in mcp_tools:
            if not self.matches_filter(tool.name):
                continue

            namespaced = f"{self.config.name}.{tool.name}"
            if namespaced in self.tool_name_map:
                msg = (
                    f"Duplicate tool name '{namespaced}' in ToolNode "
                    f"'{self.config.name}'"
                )
                raise ToolNodeError(msg)
            self.tool_name_map[namespaced] = tool.name

            args_model = self.create_args_model(
                namespaced, getattr(tool, "inputSchema", {}) or {}
            )
            out_model = _create_result_model(namespaced)

            # Only register if not already in registry (handles reconnection
            # case)
            if not self.registry.has(namespaced):
                try:
                    self.registry.register(namespaced, args_model, out_model)
                except ValueError as exc:
                    msg = (
                        f"Tool name collision for '{namespaced}' (native "
                        "tool or another ToolNode)"
                    )
                    raise ToolNodeError(
                        msg,
                    ) from exc

            # Use functools.partial to capture `namespaced` by value, not
            # reference. Without this, all closures would reference the last
            # loop iteration's value.
            async def _make_call(
                bound_name: str, args: BaseModel, ctx: ToolContext
            ) -> object:
                return await self.call(bound_name, args.model_dump(), ctx)

            bound_fn = functools.partial(_make_call, namespaced)

            extra: dict[str, Any] = {
                "source": "mcp",
                "namespace": self.config.name,
                "tool_node": self,
            }
            if isinstance(self.config.arg_validation, dict):
                extra["arg_validation"] = dict(self.config.arg_validation)

            # Detect MCP Apps metadata (io.modelcontextprotocol/ui extension)
            if self.config.apps.enabled:
                app_meta = extract_app_metadata(tool)
                if app_meta is not None:
                    self.app_metadata[namespaced] = app_meta
                    extra["has_app"] = True
                    extra["app_metadata"] = app_meta.model_dump()

            specs.append(
                NodeSpec(
                    node=Node(bound_fn, name=namespaced),
                    name=namespaced,
                    desc=getattr(tool, "description", "") or "",
                    args_model=args_model,
                    out_model=out_model,
                    side_effects="external",
                    tags=("mcp", self.config.name),
                    extra=extra,
                ),
            )

        return specs

    def convert_utcp_tools(self, utcp_tools: list[Any]) -> list[NodeSpec]:
        """Convert UTCP tool schemas to Penguiflow NodeSpec."""
        specs: list[NodeSpec] = []
        for tool in utcp_tools:
            parts = tool.name.split(".")
            original_tool_name = parts[-1] if len(parts) > 1 else tool.name

            if not self.matches_filter(original_tool_name):
                continue

            namespaced = f"{self.config.name}.{original_tool_name}"
            if namespaced in self.tool_name_map:
                msg = (
                    f"Duplicate tool name '{namespaced}' in ToolNode "
                    f"'{self.config.name}'"
                )
                raise ToolNodeError(msg)
            self.tool_name_map[namespaced] = tool.name

            args_model = self.create_args_model(
                namespaced, getattr(tool, "inputs", {}) or {}
            )
            out_model = _create_result_model(namespaced)

            # Only register if not already in registry (handles reconnection
            # case)
            if not self.registry.has(namespaced):
                try:
                    self.registry.register(namespaced, args_model, out_model)
                except ValueError as exc:
                    msg = (
                        f"Tool name collision for '{namespaced}' (native "
                        "tool or another ToolNode)"
                    )
                    raise ToolNodeError(
                        msg,
                    ) from exc

            # Use functools.partial to capture `namespaced` by value, not
            # reference. Without this, all closures would reference the last
            # loop iteration's value.
            async def _make_call(
                bound_name: str, args: BaseModel, ctx: ToolContext
            ) -> object:
                return await self.call(bound_name, args.model_dump(), ctx)

            bound_fn = functools.partial(_make_call, namespaced)

            extra: dict[str, Any] = {
                "source": "utcp",
                "namespace": self.config.name,
                "tool_node": self,
            }
            if isinstance(self.config.arg_validation, dict):
                extra["arg_validation"] = dict(self.config.arg_validation)

            specs.append(
                NodeSpec(
                    node=Node(bound_fn, name=namespaced),
                    name=namespaced,
                    desc=getattr(tool, "description", "") or "",
                    args_model=args_model,
                    out_model=out_model,
                    side_effects="external",
                    tags=("utcp", self.config.name),
                    extra=extra,
                ),
            )
        return specs

    # ─── Model Creation ──────────────────────────────────────────────────────

    def create_args_model(
        self, name: str, schema: dict[str, Any]
    ) -> type[BaseModel]:
        """Create Pydantic model from JSON schema for tool arguments."""
        props = schema.get("properties", {})
        required = set(schema.get("required", []))

        if not props:
            return create_model(
                f"{name.replace('.', '_')}Args",
                data=(dict[str, Any] | None, None),
            )

        fields: dict[str, Any] = {}
        for prop_name, prop_schema in props.items():
            python_type = self.json_type_to_python(prop_schema)
            if prop_name in required:
                fields[prop_name] = (python_type, ...)
            else:
                fields[prop_name] = (python_type | None, None)

        return create_model(f"{name.replace('.', '_')}Args", **fields)

    def json_type_to_python(
        self, prop_schema: dict[str, Any]
    ) -> type[Any] | GenericAlias:
        """Map a JSON Schema property to a Python type.

        Handles plain scalar types, array item types, JSON Schema union
        ``type`` lists (e.g. ``["string", "null"]`` for nullable fields), and
        ``anyOf``/``oneOf`` schemas. Union forms resolve to their first
        non-null branch, which is sufficient for building a permissive args
        model (optionality is conveyed via field defaults).
        """
        json_type = prop_schema.get("type")

        # No explicit "type": fall back to anyOf/oneOf union branches, then
        # str.
        if json_type is None:
            branch = _first_typed_branch(prop_schema)
            return str if branch is None else self.json_type_to_python(branch)

        # Union type list, e.g. ["string", "null"]: resolve to first non-null
        # member.
        if isinstance(json_type, list):
            non_null = [t for t in json_type if t != "null"]
            if not non_null:
                return dict[str, Any]
            resolved = dict(prop_schema)
            resolved["type"] = non_null[0]
            return self.json_type_to_python(resolved)

        if json_type in _JSON_SCALAR_TYPES:
            return _JSON_SCALAR_TYPES[json_type]
        if json_type == "array":
            return _json_array_type(prop_schema)
        return dict[str, Any]

    # ─── UTCP Config ─────────────────────────────────────────────────────────

    def build_utcp_config(self) -> dict[str, Any]:
        """Build UTCP client configuration based on utcp_mode."""
        mode = self.config.utcp_mode

        if mode == UtcpMode.AUTO:
            if self.config.connection.endswith(
                (".json", "/utcp", "/.well-known/utcp")
            ):
                mode = UtcpMode.MANUAL_URL
            else:
                mode = UtcpMode.BASE_URL

        if mode == UtcpMode.MANUAL_URL:
            return {
                "manuals": [self.config.connection],
                "variables": self.build_utcp_variables(),
            }

        call_template_type = (
            "cli" if self.config.transport == TransportType.CLI else "http"
        )
        template: dict[str, Any] = {
            "name": self.config.name,
            "call_template_type": call_template_type,
            "url": self.config.connection,
            "http_method": "POST",
        }
        auth = self.build_utcp_auth() if call_template_type == "http" else None
        if auth is not None:
            # ``auth`` covers the discovery request, ``auth_tools`` the
            # tools generated from an OpenAPI spec.
            template["auth"] = auth
            template["auth_tools"] = dict(auth)
        return {
            "manual_call_templates": [template],
            "variables": self.build_utcp_variables(),
        }

    def build_utcp_auth(self) -> dict[str, str] | None:
        """Build the UTCP ``api_key`` auth of a base-URL call template.

        Returns:
            The auth for the configured static credentials (BEARER, API_KEY
            or COOKIE), or ``None`` when the source has no static auth.
        """
        auth_type = self.config.auth_type
        if auth_type == AuthType.COOKIE:
            auth_config = self.config.auth_config
            var_name = self.substitute_env(str(auth_config["cookie_name"]))
            key = self.substitute_env(str(auth_config["cookie_value"]))
            location = "cookie"
        elif auth_type in (AuthType.BEARER, AuthType.API_KEY):
            ((var_name, key),) = self._get_static_auth_headers().items()
            location = "header"
        else:
            return None
        return {
            "auth_type": "api_key",
            "api_key": key,
            "var_name": var_name,
            "location": location,
        }

    def build_utcp_variables(self) -> dict[str, str]:
        """Build UTCP variable substitutions from env and auth_config."""
        variables: dict[str, str] = {}

        for key, value in self.config.env.items():
            variables[key] = self.substitute_env(value)

        for key, value in self.config.auth_config.items():
            variables[key] = self.substitute_env(str(value))

        return variables

    # ─── Helpers ─────────────────────────────────────────────────────────────

    def matches_filter(self, tool_name: str) -> bool:
        """Return whether ``tool_name`` passes the configured tool filter."""
        if not self.config.tool_filter:
            return True
        return any(
            re.match(pattern, tool_name) for pattern in self.config.tool_filter
        )

    def substitute_env(self, value: str) -> str:
        """Substitute ${VAR} patterns from the environment, failing on gaps."""
        import os

        pattern = r"\$\{([^}]+)\}"

        def _replace(match: re.Match[str]) -> str:
            var = match.group(1)
            val = os.environ.get(var)
            if val is None:
                warnings.warn(
                    f"Environment variable '{var}' not set for "
                    f"ToolNode '{self.config.name}'",
                    DeprecationWarning,
                    stacklevel=2,
                )
                msg = (
                    f"Missing required environment variable '{var}' for "
                    f"ToolNode '{self.config.name}'"
                )
                raise ToolAuthError(
                    msg,
                )
            return val

        return re.sub(pattern, _replace, value)

    # ─── Output Transformation (Phase 1) ─────────────────────────────────────

    async def transform_output(
        self,
        tool_name: str,
        result: object,
        ctx: ToolContext,
    ) -> object:
        """Transform tool output through layered artifact extraction.

        Layer priority (highest to lowest):
        - L5: Custom transformer (escape hatch for complete control)
        - L4: Per-tool field configuration (explicit field extraction)
        - L1: Resource links (MCP resource_link content blocks)
        - L2: MCP typed content (embedded content blocks with mime_type)
        - L3: Heuristic binary detection (base64 signature matching)
        - L0: Size safety net (clamp oversized string content)

        Args:
            tool_name: The original (unnamespaced) tool name
            result: Raw tool result from MCP/UTCP
            ctx: Tool context with artifact store access

        Returns:
            Transformed result with binary/large content replaced by
            ArtifactRefs
        """
        config = self.config.artifact_extraction

        # L5: Custom transformer (complete override)
        if self.config.output_transformer is not None:
            namespaced = f"{self.config.name}.{tool_name}"
            transformed = self.config.output_transformer(
                namespaced, result, ctx
            )
            if inspect.isawaitable(transformed):
                return await transformed
            return transformed

        # L4: Per-tool field configuration
        namespaced = f"{self.config.name}.{tool_name}"
        if namespaced in config.tool_fields:
            result = await self._apply_field_extraction(
                result, config.tool_fields[namespaced], ctx
            )

        # L1: Resource links (MCP resource_link content blocks)
        result = await self._handle_resource_links(result, ctx)

        # L2: MCP typed content blocks
        if config.handle_mcp_typed_content:
            result = await self._transform_mcp_content_blocks(result, ctx)

        # L3: Heuristic binary detection
        if config.binary_detection.enabled:
            result = await self._detect_and_extract_binary(result, ctx)

        # L0: Size safety net
        return await self._apply_size_limits(result, ctx)

    async def _apply_field_extraction(
        self,
        result: object,
        field_configs: list[Any],  # list[ArtifactFieldConfig]
        ctx: ToolContext,
    ) -> object:
        """Extract configured fields as artifacts, per tool configuration.

        Args:
            result: The tool result to process
            field_configs: List of ArtifactFieldConfig for this tool
            ctx: Tool context with artifact store

        Returns:
            Result with configured fields replaced by ArtifactRefs
        """
        if isinstance(result, list):
            transformed: list[Any] = []
            for item in result:
                if isinstance(item, (dict, list)):
                    transformed.append(
                        await self._apply_field_extraction(
                            item, field_configs, ctx
                        )
                    )
                else:
                    transformed.append(item)
            return transformed

        if not isinstance(result, dict):
            return result

        for field_config in field_configs:
            parts = field_config.field_path.split(".")
            targets = self._resolve_field_targets(result, parts)
            # Common wrapper pattern from some MCP servers: {"result": {...}}
            if not targets and isinstance(result.get("result"), (dict, list)):
                targets = self._resolve_field_targets(result["result"], parts)

            for parent, key in targets:
                value = _container_get(parent, key)
                if not isinstance(value, str) or len(value) <= 100:
                    continue
                try:
                    replacement = await self._field_artifact(
                        field_config, value, ctx
                    )
                except Exception as e:
                    logger.debug(
                        f"Field extraction failed for "
                        f"{field_config.field_path}: {e}"
                    )
                    continue
                _container_set(parent, key, replacement)

        return result

    async def _field_artifact(
        self, field_config: ArtifactFieldConfig, value: str, ctx: ToolContext
    ) -> dict[str, Any]:
        """Upload a field value as an artifact; return its replacement."""
        if field_config.content_type in ("pdf", "image", "binary"):
            data = base64.b64decode(value)
        else:
            data = value.encode("utf-8")

        mime = field_config.mime_type
        if mime is None:
            mime = self._infer_mime_type(field_config.content_type)

        ref = await ctx.artifacts.upload(
            data,
            mime_type=mime,
            namespace=self.config.name,
        )
        summary = field_config.summary_template.format(
            content_type=field_config.content_type,
            size=len(data),
            artifact_id=ref.id,
        )
        return {"artifact": ref.model_dump(), "summary": summary}

    def _resolve_field_targets(
        self,
        root: object,
        parts: list[str],
    ) -> list[tuple[dict[str, Any] | list[Any], str | int]]:
        """Resolve container/key targets for a dot-path in dicts and lists."""
        if not parts:
            return []
        return self._resolve_field_targets_recursive(root, parts)

    def _resolve_field_targets_recursive(
        self,
        current: object,
        parts: list[str],
    ) -> list[tuple[dict[str, Any] | list[Any], str | int]]:
        part = parts[0]
        rest = parts[1:]
        targets: list[tuple[dict[str, Any] | list[Any], str | int]] = []

        if isinstance(current, dict):
            if part not in current:
                return targets
            if not rest:
                return [(current, part)]
            return self._resolve_field_targets_recursive(current[part], rest)

        if isinstance(current, list):
            if part.isdigit():
                idx = int(part)
                if idx < 0 or idx >= len(current):
                    return targets
                if not rest:
                    return [(current, idx)]
                return self._resolve_field_targets_recursive(
                    current[idx], rest
                )

            # For list roots, apply the same path to each element
            # (content-block lists)
            for item in current:
                targets.extend(
                    self._resolve_field_targets_recursive(item, parts)
                )

        return targets

    async def _handle_resource_links(
        self, result: object, ctx: ToolContext
    ) -> object:
        """Handle MCP resource_link content blocks.

        Replaces each resource_link with a stub naming its URI; the LLM
        reads the content with the generated resources_read tool.

        Args:
            result: Tool result that may contain resource_link content
            ctx: Tool context

        Returns:
            Result with resource_links converted to artifact references
        """
        config = self.config.artifact_extraction.resources
        if not config.enabled:
            return result

        # Handle MCP CallToolResult with content blocks
        if hasattr(result, "content") and isinstance(result.content, list):
            for i, item in enumerate(result.content):
                if hasattr(item, "type") and item.type == "resource_link":
                    # Convert resource_link to artifact reference stub
                    resource_uri = getattr(item, "uri", None) or getattr(
                        item, "resource", {}
                    ).get("uri")
                    if resource_uri:
                        result.content[i] = {
                            "type": "artifact_stub",
                            "resource_uri": str(resource_uri),
                            "summary": f"Resource link: {resource_uri}",
                            "note": "Use MCP resources/read to fetch content",
                        }
        elif isinstance(result, dict):
            result = await self._walk_and_transform_resource_links(result, ctx)
        elif isinstance(result, list):
            result = [
                await self._handle_resource_links(item, ctx) for item in result
            ]

        return result

    async def _walk_and_transform_resource_links(
        self,
        data: dict[str, Any],
        ctx: ToolContext,
    ) -> dict[str, Any]:
        """Recursively walk dict and transform resource_link entries."""
        transformed: dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(value, dict):
                if value.get("type") == "resource_link":
                    # Transform resource_link to stub
                    uri = value.get("uri") or value.get("resource", {}).get(
                        "uri"
                    )
                    transformed[key] = {
                        "type": "artifact_stub",
                        "resource_uri": str(uri) if uri else None,
                        "summary": f"Resource link: {uri}",
                    }
                else:
                    transformed[
                        key
                    ] = await self._walk_and_transform_resource_links(
                        value, ctx
                    )
            elif isinstance(value, list):
                transformed[key] = [
                    await self._walk_and_transform_resource_links(item, ctx)
                    if isinstance(item, dict)
                    else item
                    for item in value
                ]
            else:
                transformed[key] = value
        return transformed

    async def _transform_mcp_content_blocks(
        self, result: object, ctx: ToolContext
    ) -> object:
        """Transform MCP typed content blocks (embedded, with a mime_type).

        Handles:
        - EmbeddedResource with blob data
        - Content blocks with explicit mime_type

        Args:
            result: Tool result
            ctx: Tool context

        Returns:
            Result with embedded binary content extracted as artifacts
        """
        if hasattr(result, "content") and isinstance(result.content, list):
            for i, item in enumerate(result.content):
                # Handle EmbeddedResource with blob
                if hasattr(item, "type") and item.type == "resource":
                    resource = getattr(item, "resource", None)
                    if resource and hasattr(resource, "blob"):
                        try:
                            data = base64.b64decode(resource.blob)
                            mime = (
                                getattr(resource, "mimeType", None)
                                or "application/octet-stream"
                            )
                            ref = await ctx.artifacts.upload(
                                data,
                                mime_type=mime,
                                namespace=self.config.name,
                            )
                            config = self.config.artifact_extraction
                            summary = config.default_binary_summary.format(
                                mime_type=mime,
                                size=len(data),
                                artifact_id=ref.id,
                            )
                            result.content[i] = {
                                "type": "artifact",
                                "artifact": ref.model_dump(),
                                "summary": summary,
                            }
                        except Exception as e:
                            logger.debug(
                                f"MCP content block extraction failed: {e}"
                            )

        elif isinstance(result, dict):
            # Check for blob fields in dict structure
            for key, value in list(result.items()):
                if isinstance(value, dict) and "blob" in value:
                    try:
                        data = base64.b64decode(value["blob"])
                        mime = value.get(
                            "mimeType", "application/octet-stream"
                        )
                        ref = await ctx.artifacts.upload(
                            data,
                            mime_type=mime,
                            namespace=self.config.name,
                        )
                        config = self.config.artifact_extraction
                        result[key] = {
                            "artifact": ref.model_dump(),
                            "summary": config.default_binary_summary.format(
                                mime_type=mime,
                                size=len(data),
                                artifact_id=ref.id,
                            ),
                        }
                    except Exception as e:
                        logger.debug(f"Blob extraction failed for {key}: {e}")

        return result

    async def _detect_and_extract_binary(
        self, result: object, ctx: ToolContext
    ) -> object:
        """Detect binary content heuristically and extract as artifacts.

        Uses base64 signature matching to identify binary content.

        Args:
            result: Tool result to scan
            ctx: Tool context

        Returns:
            Result with detected binary content replaced by artifact references
        """
        if isinstance(result, str):
            return await self._maybe_extract_binary_string(result, ctx)
        if isinstance(result, dict):
            return await self._walk_and_extract_binary(result, ctx)
        if isinstance(result, list):
            return [
                await self._detect_and_extract_binary(item, ctx)
                for item in result
            ]

        return result

    async def _maybe_extract_binary_string(
        self, value: str, ctx: ToolContext
    ) -> object:
        """Check if a string is base64-encoded binary and extract if so.

        Args:
            value: String to check
            ctx: Tool context

        Returns:
            ArtifactRef dict if binary detected, original string otherwise
        """
        config = self.config.artifact_extraction.binary_detection

        # Skip short strings
        if len(value) < config.min_size_for_detection:
            return value

        # Check for binary signatures
        detected = self.detect_binary_signature(value)
        if detected is None:
            return value

        extension, mime_type = detected

        try:
            # Decode and validate
            data = base64.b64decode(value[: config.max_decode_bytes * 4 // 3])
            if len(data) > config.max_decode_bytes:
                data = data[: config.max_decode_bytes]

            # Optional magic byte validation
            if config.require_magic_bytes and not _validate_magic_bytes(
                data, extension
            ):
                return value

            # Store as artifact
            ref = await ctx.artifacts.upload(
                data,
                mime_type=mime_type,
                namespace=self.config.name,
            )

            extract_config = self.config.artifact_extraction
            summary = extract_config.default_binary_summary.format(
                mime_type=mime_type,
                size=len(data),
                artifact_id=ref.id,
            )

            return {
                "artifact": ref.model_dump(),
                "summary": summary,
            }

        except Exception as e:
            logger.debug(f"Binary extraction failed: {e}")
            return value

    async def _walk_and_extract_binary(
        self,
        data: dict[str, Any],
        ctx: ToolContext,
    ) -> dict[str, Any]:
        """Recursively walk dict and extract binary content."""
        transformed: dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(value, str):
                transformed[key] = await self._maybe_extract_binary_string(
                    value, ctx
                )
            elif isinstance(value, dict):
                transformed[key] = await self._walk_and_extract_binary(
                    value, ctx
                )
            elif isinstance(value, list):
                transformed[key] = [
                    await self._detect_and_extract_binary(item, ctx)
                    for item in value
                ]
            else:
                transformed[key] = value
        return transformed

    def detect_binary_signature(self, value: str) -> tuple[str, str] | None:
        """Check if string starts with a known binary signature.

        Args:
            value: String to check (potentially base64 encoded)

        Returns:
            Tuple of (extension, mime_type) if detected, None otherwise
        """
        signatures = (
            self.config.artifact_extraction.binary_detection.signatures
        )
        for prefix, (ext, mime) in signatures.items():
            if value.startswith(prefix):
                return (ext, mime)
        return None

    def _infer_mime_type(self, content_type: str) -> str:
        """Infer MIME type from content type hint.

        Args:
            content_type: Hint like "pdf", "image", "text"

        Returns:
            MIME type string
        """
        mapping = {
            "pdf": "application/pdf",
            "image": "image/png",
            "binary": "application/octet-stream",
            "text": "text/plain",
        }
        return mapping.get(content_type, "application/octet-stream")

    async def _apply_size_limits(
        self, result: object, ctx: ToolContext
    ) -> object:
        """Apply size limits as safety net for oversized content.

        Args:
            result: Tool result
            ctx: Tool context

        Returns:
            Result with oversized strings converted to artifact references
        """
        config = self.config.artifact_extraction

        if not config.auto_artifact_large_content:
            return result

        max_size = config.max_inline_size

        if isinstance(result, str) and len(result) > max_size:
            # Large string - store as text artifact
            ref = await ctx.artifacts.upload(
                result,
                namespace=self.config.name,
            )
            summary = config.default_text_summary.format(
                size=len(result),
                artifact_id=ref.id,
            )
            return {
                "artifact": ref.model_dump(),
                "summary": summary,
                "preview": result[:500] + "..."
                if len(result) > 500
                else result,
            }

        if isinstance(result, dict):
            return await self._walk_and_apply_size_limits(result, ctx)

        if isinstance(result, list):
            return [
                await self._apply_size_limits(item, ctx) for item in result
            ]

        return result

    async def _walk_and_apply_size_limits(
        self,
        data: dict[str, Any],
        ctx: ToolContext,
    ) -> dict[str, Any]:
        """Recursively walk dict and apply size limits."""
        config = self.config.artifact_extraction
        max_size = config.max_inline_size
        transformed: dict[str, Any] = {}

        for key, value in data.items():
            if isinstance(value, str) and len(value) > max_size:
                # Store as text artifact
                ref = await ctx.artifacts.upload(
                    value,
                    namespace=self.config.name,
                )
                transformed[key] = {
                    "artifact": ref.model_dump(),
                    "summary": config.default_text_summary.format(
                        size=len(value),
                        artifact_id=ref.id,
                    ),
                    "preview": value[:500] + "..."
                    if len(value) > 500
                    else value,
                }
            elif isinstance(value, dict):
                transformed[key] = await self._walk_and_apply_size_limits(
                    value, ctx
                )
            elif isinstance(value, list):
                transformed[key] = [
                    await self._apply_size_limits(item, ctx) for item in value
                ]
            else:
                transformed[key] = value

        return transformed

    # ─── MCP Resources Methods (Phase 2) ─────────────────────────────────────

    def generate_resource_tools(self) -> list[NodeSpec]:
        """Generate NodeSpecs for MCP resource operations.

        Creates tools for:
        - {namespace}.resources_list: List available resources
        - {namespace}.resources_read: Read resource by URI
        - {namespace}.resources_templates_list: List resource templates

        Returns:
            List of NodeSpec for resource tools
        """
        specs: list[NodeSpec] = []

        # Only generate resource tools if resources are supported
        if not self._resources_supported:
            return specs

        namespace = self.config.name

        # Create models for resources_list
        resource_list_input_model = create_model(
            f"{namespace}_ResourceListInput"
        )
        resource_list_output_model = create_model(
            f"{namespace}_ResourceListOutput",
            resources=(list[dict[str, Any]], []),
            count=(int, 0),
        )

        # Create model for resources_read input/output
        resource_read_input_model = create_model(
            f"{namespace}_ResourceReadInput",
            uri=(str, ...),
        )
        resource_read_output_model = create_model(
            f"{namespace}_ResourceReadOutput",
            result=(dict[str, Any], {}),
            uri=(str, ""),
        )

        # Create model for templates list output
        templates_list_input_model = create_model(
            f"{namespace}_TemplatesListInput"
        )
        templates_list_output_model = create_model(
            f"{namespace}_TemplatesListOutput",
            templates=(list[dict[str, Any]], []),
            count=(int, 0),
        )

        # Register in registry (if not already registered)
        if not self.registry.has(f"{namespace}.resources_list"):
            self.registry.register(
                f"{namespace}.resources_list",
                resource_list_input_model,
                resource_list_output_model,
            )
        if not self.registry.has(f"{namespace}.resources_read"):
            self.registry.register(
                f"{namespace}.resources_read",
                resource_read_input_model,
                resource_read_output_model,
            )
        if not self.registry.has(f"{namespace}.resources_templates_list"):
            self.registry.register(
                f"{namespace}.resources_templates_list",
                templates_list_input_model,
                templates_list_output_model,
            )

        # resources_list tool
        specs.append(
            NodeSpec(
                name=f"{namespace}.resources_list",
                desc=f"List available resources from {namespace}",
                args_model=resource_list_input_model,
                out_model=resource_list_output_model,
                node=Node(
                    self.handle_resources_list,
                    name=f"{namespace}.resources_list",
                ),
                tags=["mcp", "resources", namespace],
                extra={
                    "source": "mcp",
                    "namespace": namespace,
                    "tool_node": self,
                    "resource_tool": True,
                },
            )
        )

        # resources_read tool
        specs.append(
            NodeSpec(
                name=f"{namespace}.resources_read",
                desc=f"Read a resource by URI from {namespace}",
                args_model=resource_read_input_model,
                out_model=resource_read_output_model,
                node=Node(
                    self.handle_resources_read,
                    name=f"{namespace}.resources_read",
                ),
                tags=["mcp", "resources", namespace],
                extra={
                    "source": "mcp",
                    "namespace": namespace,
                    "tool_node": self,
                    "resource_tool": True,
                },
            )
        )

        # resources_templates_list tool
        specs.append(
            NodeSpec(
                name=f"{namespace}.resources_templates_list",
                desc=f"List resource templates from {namespace}",
                args_model=templates_list_input_model,
                out_model=templates_list_output_model,
                node=Node(
                    self.handle_resources_templates_list,
                    name=f"{namespace}.resources_templates_list",
                ),
                tags=["mcp", "resources", namespace],
                extra={
                    "source": "mcp",
                    "namespace": namespace,
                    "tool_node": self,
                    "resource_tool": True,
                },
            )
        )

        # Store tool name mappings
        self.tool_name_map[f"{namespace}.resources_list"] = "resources_list"
        self.tool_name_map[f"{namespace}.resources_read"] = "resources_read"
        self.tool_name_map[f"{namespace}.resources_templates_list"] = (
            "resources_templates_list"
        )

        logger.debug(
            "Generated %d resource tools for '%s'",
            len(specs),
            namespace,
        )

        return specs

    async def handle_resources_list(
        self,
        _args: object,
        ctx: ToolContext,
    ) -> dict[str, Any]:
        """Handler for resources_list tool."""
        resources = await self.list_resources(ctx=ctx)
        return {
            "resources": [r.model_dump() for r in resources],
            "count": len(resources),
        }

    async def handle_resources_read(
        self,
        args: object,
        ctx: ToolContext,
    ) -> dict[str, Any]:
        """Handler for resources_read tool."""
        uri = getattr(
            args, "uri", args.get("uri") if isinstance(args, dict) else None
        )
        if not uri:
            return {
                "result": {"error": "Missing required 'uri' parameter"},
                "uri": "",
            }

        result = await self.read_resource(uri, ctx)
        return {"result": result, "uri": uri}

    async def handle_resources_templates_list(
        self,
        _args: object,
        ctx: ToolContext,
    ) -> dict[str, Any]:
        """Handler for resources_templates_list tool."""
        templates = await self.list_resource_templates(ctx=ctx)
        return {
            "templates": [t.model_dump() for t in templates],
            "count": len(templates),
        }

    async def list_resources(
        self, refresh: bool = False, *, ctx: ToolContext | None = None
    ) -> list[ResourceInfo]:
        """List available MCP resources.

        With per-user sessions the list is the caller's (``ctx``), read
        from their session the first time.

        Args:
            refresh: Force refresh from server
            ctx: The caller's context; needed for per-user sessions.

        Returns:
            List of ResourceInfo objects (empty if not connected or resources
            not supported)
        """
        if not self.connected or not self._resources_supported:
            return self._resources  # Return cached resources or empty list

        async with self._optional_lease(ctx) as session:
            if session is None:
                return self._resources
            if self.per_user_sessions:
                return await self._session_resources(session, refresh=refresh)
            if refresh:
                await self._refresh_shared_resources(session)
        return self._resources

    async def _session_resources(
        self, session: McpSession, *, refresh: bool
    ) -> list[ResourceInfo]:
        """Return ``session``'s resource list, reading it when needed."""
        if refresh or session.resources is None:
            try:
                session.resources = [
                    _parse_resource_info(r)
                    for r in _as_list(await session.client.list_resources())
                ]
            except Exception as e:
                logger.warning(f"Failed to refresh resources: {e}")
        return session.resources or []

    async def _refresh_shared_resources(self, session: McpSession) -> None:
        try:
            self._resources = [
                _parse_resource_info(r)
                for r in _as_list(await session.client.list_resources())
            ]
        except Exception as e:
            logger.warning(f"Failed to refresh resources: {e}")

    async def list_resource_templates(
        self, refresh: bool = False, *, ctx: ToolContext | None = None
    ) -> list[ResourceTemplateInfo]:
        """List available MCP resource templates.

        Args:
            refresh: Force refresh from server
            ctx: The caller's context; needed for per-user sessions.

        Returns:
            List of ResourceTemplateInfo objects (empty if not connected or not
            supported)
        """
        if not self.connected or not self._resources_supported:
            return (
                self._resource_templates
            )  # Return cached templates or empty list

        if refresh:
            async with self._optional_lease(ctx) as session:
                if session is not None:
                    await self._refresh_templates(session)
        return self._resource_templates

    async def _refresh_templates(self, session: McpSession) -> None:
        try:
            self._resource_templates = [
                _parse_template_info(t)
                for t in _as_list(
                    await session.client.list_resource_templates()
                )
            ]
        except Exception as e:
            logger.warning(f"Failed to refresh resource templates: {e}")

    async def read_resource(
        self,
        uri: str,
        ctx: ToolContext,
        *,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        """Read a resource by URI.

        Reads through the caller's MCP session and its ResourceCache, so
        one user's reads are never served to another. Binary content is
        stored in ArtifactStore; small text may be inlined.

        Args:
            uri: Resource URI to read
            ctx: Tool context with artifact store access
            use_cache: Whether to use caching (default True)

        Returns:
            Dict with 'artifact' (ArtifactRef) or 'text' (inline content) or
            'error'
        """
        if not self.connected:
            return {"error": f"ToolNode '{self.config.name}' not connected"}

        if not self._resources_supported:
            return {
                "error": f"Resources not supported by '{self.config.name}'"
            }

        async with self._optional_lease(ctx) as session:
            if session is None:
                return {"error": "MCP client not available"}
            return await self._read_through(
                session, uri, ctx, use_cache=use_cache
            )

    async def _read_through(
        self,
        session: McpSession,
        uri: str,
        ctx: ToolContext,
        *,
        use_cache: bool,
    ) -> dict[str, Any]:
        client = session.client

        async def read_fn(resource_uri: str) -> object:
            return await client.read_resource(resource_uri)

        if use_cache:
            return await session.cache.get_or_fetch(uri, read_fn, ctx)
        # Bypass cache, read directly
        try:
            contents = await client.read_resource(uri)
            return await self._process_resource_contents(contents, ctx)
        except Exception as e:
            return {"error": str(e)}

    async def _process_resource_contents(
        self,
        contents: object,
        ctx: ToolContext,
    ) -> dict[str, Any]:
        """Process resource contents into artifact or inline text."""
        import base64

        # Supported response shapes: a dict with a "contents" list, a list
        # of content items, or a single content item.
        if isinstance(contents, dict):
            resource_data = contents.get("contents", [contents])
            if isinstance(resource_data, list) and resource_data:
                content_item = resource_data[0]
            else:
                content_item = resource_data
        elif isinstance(contents, list):
            content_item = contents[0] if contents else {}
        else:
            content_item = contents

        text = getattr(content_item, "text", None) or (
            content_item.get("text")
            if isinstance(content_item, dict)
            else None
        )
        blob = getattr(content_item, "blob", None) or (
            content_item.get("blob")
            if isinstance(content_item, dict)
            else None
        )
        mime_type = getattr(content_item, "mimeType", None) or (
            content_item.get("mimeType")
            if isinstance(content_item, dict)
            else None
        )

        if blob is not None:
            try:
                data = base64.b64decode(blob)
                ref = await ctx.artifacts.upload(
                    data,
                    mime_type=mime_type or "application/octet-stream",
                    namespace=f"{self.config.name}.resource",
                )
                return {"artifact": ref.model_dump()}
            except Exception as e:
                return {"error": f"Failed to process blob: {e}"}

        elif text is not None:
            resources_config = self.config.artifact_extraction.resources
            threshold = resources_config.inline_text_if_under_chars
            if len(text) <= threshold:
                return {"text": text}
            ref = await ctx.artifacts.upload(
                text,
                mime_type=mime_type or "text/plain",
                namespace=f"{self.config.name}.resource",
            )
            return {"artifact": ref.model_dump()}

        return {"error": "Resource has no content"}

    async def subscribe_resource(
        self,
        uri: str,
        callback: Callable[[str], object] | None = None,
        *,
        ctx: ToolContext | None = None,
    ) -> bool:
        """Subscribe to resource updates.

        The subscription belongs to the caller's MCP session. It ends when
        that session closes (``close()``, a reconnect, or a per-user session
        being replaced or evicted); subscribe again after that.

        Args:
            uri: Resource URI to subscribe to
            callback: Optional callback for updates
            ctx: The caller's context; needed for per-user sessions.

        Returns:
            True if subscription successful
        """
        if not self.connected or not self._resources_supported:
            return False

        async with self._optional_lease(ctx) as session:
            if session is None:
                return False
            subscribe_method = _mcp_resource_method(
                session.client, "subscribe_resource", "subscribe"
            )
            if subscribe_method is None:
                logger.debug(
                    "MCP client for '%s' does not support resource "
                    "subscriptions",
                    self.config.name,
                )
                return False
            return await session.subscriptions.subscribe(
                uri, subscribe_method, callback
            )

    async def unsubscribe_resource(
        self, uri: str, *, ctx: ToolContext | None = None
    ) -> bool:
        """Unsubscribe from resource updates.

        Args:
            uri: Resource URI to unsubscribe from
            ctx: The caller's context; needed for per-user sessions.

        Returns:
            True if unsubscription successful
        """
        if not self.connected:
            return False

        async with self._optional_lease(ctx) as session:
            if session is None:
                return False
            unsubscribe_method = _mcp_resource_method(
                session.client, "unsubscribe_resource", "unsubscribe"
            )
            if unsubscribe_method is None:
                logger.debug(
                    "MCP client for '%s' does not support resource "
                    "unsubscriptions",
                    self.config.name,
                )
                return False
            return await session.subscriptions.unsubscribe(
                uri, unsubscribe_method
            )

    def handle_mcp_message(self, message: object) -> None:
        """Handle one message an MCP server sent to this node.

        Each session's client routes its own server's messages to that
        session. This entry point treats a resource update notification as
        applying to every session (see :meth:`handle_resource_updated`).
        Other messages are ignored here.

        Args:
            message: A message from the MCP session: a server
                notification, a server request or an exception.
        """
        uri = resource_updated_uri(message)
        if uri is not None:
            self.handle_resource_updated(uri)

    def handle_resource_updated(self, uri: str) -> None:
        """Handle a resource updated notification from MCP server.

        Invalidates ``uri`` in every session's cache and notifies every
        session's subscribers, then the node's update callback once, with
        no principal.

        Args:
            uri: URI of updated resource
        """
        for session in self._pool.sessions():
            self._invalidate_session(session, uri)
        self._fire_resource_callback(uri, None)

    def set_resource_updated_callback(
        self, callback: Callable[[str, Principal | None], None] | None
    ) -> None:
        """Register a callback invoked when MCP resources are updated.

        The callback is called as ``callback(uri, principal)``.
        ``principal`` is the ``(tenant_id, user_id)`` whose MCP session
        received the update, or ``None`` when the update applies to every
        caller (the shared session, or :meth:`handle_resource_updated`).
        With per-user auth, deliver a per-user update only to that user.

        Args:
            callback: Callable invoked with the updated resource URI and
                its principal, or ``None`` to clear a previously
                registered callback.
        """
        self._resource_update_callback = callback

    @property
    def resources_supported(self) -> bool:
        """Check if MCP resources are supported by this tool source."""
        return self._resources_supported

    @resources_supported.setter
    def resources_supported(self, value: bool) -> None:
        self._resources_supported = value

    @property
    def resources(self) -> list[ResourceInfo]:
        """Get cached list of resources (call list_resources for refresh)."""
        return self._resources

    @resources.setter
    def resources(self, value: list[ResourceInfo]) -> None:
        self._resources = value

    @property
    def resource_templates(self) -> list[ResourceTemplateInfo]:
        """Get cached list of resource templates."""
        return self._resource_templates

    @resource_templates.setter
    def resource_templates(self, value: list[ResourceTemplateInfo]) -> None:
        self._resource_templates = value

    # -------------------------------------------------------------------------
    # MCP Apps
    # -------------------------------------------------------------------------

    async def enrich_with_app_html(
        self,
        tool_result: object,
        app_meta: AppMetadata,
        tool_input: dict[str, Any],
        ctx: ToolContext,
        *,
        raw_tool_result: object = None,
    ) -> object:
        """Fetch the UI resource and attach it to the tool result.

        After an app-enabled tool executes, this method fetches the HTML
        from the ui:// resource and stores it as an artifact. The tool result
        is enriched with a ``__mcp_app__`` marker so the output pipeline and
        frontend can detect and render it.

        Args:
            tool_result: The original transformed tool result
            app_meta: App metadata from tool discovery
            tool_input: The arguments the tool was called with
            ctx: Tool context with artifact store access
            raw_tool_result: The untransformed tool result, when available

        Returns:
            Enriched result with ``__mcp_app__`` key, or the original result on
            failure
        """
        html = await self.fetch_app_html(app_meta.resource_uri, ctx=ctx)
        if html is None:
            return tool_result

        session_id: str | None = None
        try:
            tool_ctx = getattr(ctx, "tool_context", None)
            if isinstance(tool_ctx, dict):
                raw_session_id = tool_ctx.get("session_id")
                if raw_session_id is not None:
                    session_id = str(raw_session_id)
        except Exception:
            session_id = None

        app_meta_payload: dict[str, Any] = {
            "csp": app_meta.csp.model_dump(),
            "permissions": app_meta.permissions.model_dump(),
            "tool_data": self._serialize_app_tool_result(
                raw_tool_result, fallback=tool_result
            ),
            "tool_input": tool_input,
            "resource_uri": app_meta.resource_uri,
            "prefers_border": app_meta.prefers_border,
            "namespace": self.config.name,
            "sandbox": self.config.apps.default_sandbox,
        }
        if session_id is not None:
            app_meta_payload["session_id"] = session_id

        # Store HTML as artifact
        try:
            ref = await ctx.artifacts.upload(
                html,
                mime_type=UI_MIME_TYPE,
                namespace=f"{self.config.name}.app",
                meta=app_meta_payload,
            )
        except Exception as e:
            logger.warning("Failed to store app HTML artifact: %s", e)
            return tool_result

        # Attach app metadata to result
        app_payload = {
            "artifact_id": ref.id,
            **app_meta_payload,
        }

        if isinstance(tool_result, dict):
            enriched = dict(tool_result)
            enriched["__mcp_app__"] = app_payload
            return enriched

        return {"value": tool_result, "__mcp_app__": app_payload}

    async def fetch_app_html(
        self, uri: str, *, ctx: ToolContext | None = None
    ) -> str | None:
        """Fetch ui:// resource HTML for MCP App rendering.

        Args:
            uri: Resource URI (typically ui:// scheme)
            ctx: The caller's context; needed for per-user sessions.

        Returns:
            HTML string or None on failure
        """
        async with self._optional_lease(ctx) as session:
            if session is None:
                return None
            try:
                contents = await session.client.read_resource(uri)
                return _extract_text_from_resource(contents)
            except Exception as e:
                logger.warning("Failed to fetch app HTML from %s: %s", uri, e)
                return None

    @staticmethod
    def _serialize_app_tool_result(
        raw_tool_result: object, *, fallback: object
    ) -> object:
        """Keep structured MCP results for apps, falling back safely."""
        if raw_tool_result is None:
            return fallback
        if isinstance(
            raw_tool_result, (dict, list, str, int, float, bool, type(None))
        ):
            return raw_tool_result
        if hasattr(raw_tool_result, "model_dump"):
            try:
                return raw_tool_result.model_dump(mode="json")
            except Exception:
                return fallback
        return fallback

    def has_app(self, tool_name: str) -> bool:
        """Check if a tool has MCP App metadata.

        Args:
            tool_name: Namespaced tool name

        Returns:
            True if the tool has app metadata
        """
        return tool_name in self.app_metadata

    def get_app_metadata(self, tool_name: str) -> AppMetadata | None:
        """Get MCP App metadata for a tool.

        Args:
            tool_name: Namespaced tool name

        Returns:
            AppMetadata or None
        """
        return self.app_metadata.get(tool_name)

    @property
    def app_tools(self) -> dict[str, AppMetadata]:
        """Get all tools with MCP App metadata."""
        return dict(self.app_metadata)

    # -------------------------------------------------------------------------
    # MCP Prompts (Phase 3)
    # -------------------------------------------------------------------------

    def generate_prompt_tools(self) -> list[NodeSpec]:
        """Generate NodeSpecs for MCP prompt operations.

        Creates tools for:
        - {namespace}.prompts_list: List available prompts
        - {namespace}.prompts_get: Execute a prompt and return rendered
          messages

        Returns:
            List of NodeSpec for prompt tools
        """
        specs: list[NodeSpec] = []

        if not self._prompts_supported:
            return specs

        namespace = self.config.name

        # Create models for prompts_list
        prompts_list_input_model = create_model(
            f"{namespace}_PromptsListInput"
        )
        prompts_list_output_model = create_model(
            f"{namespace}_PromptsListOutput",
            prompts=(list[dict[str, Any]], []),
            count=(int, 0),
        )

        # Create models for prompts_get
        prompts_get_input_model = create_model(
            f"{namespace}_PromptsGetInput",
            name=(str, ...),
            arguments=(dict[str, str], {}),
        )
        prompts_get_output_model = create_model(
            f"{namespace}_PromptsGetOutput",
            messages=(list[dict[str, Any]], []),
            description=(str | None, None),
        )

        # Register in registry (if not already registered)
        if not self.registry.has(f"{namespace}.prompts_list"):
            self.registry.register(
                f"{namespace}.prompts_list",
                prompts_list_input_model,
                prompts_list_output_model,
            )
        if not self.registry.has(f"{namespace}.prompts_get"):
            self.registry.register(
                f"{namespace}.prompts_get",
                prompts_get_input_model,
                prompts_get_output_model,
            )

        # prompts_list tool
        specs.append(
            NodeSpec(
                name=f"{namespace}.prompts_list",
                desc=f"List available prompts from {namespace}",
                args_model=prompts_list_input_model,
                out_model=prompts_list_output_model,
                node=Node(
                    self.handle_prompts_list, name=f"{namespace}.prompts_list"
                ),
                tags=["mcp", "prompts", namespace],
                extra={
                    "source": "mcp",
                    "namespace": namespace,
                    "tool_node": self,
                    "prompt_tool": True,
                },
            )
        )

        # prompts_get tool
        specs.append(
            NodeSpec(
                name=f"{namespace}.prompts_get",
                desc=(
                    f"Execute a prompt by name from {namespace} and "
                    "return rendered messages"
                ),
                args_model=prompts_get_input_model,
                out_model=prompts_get_output_model,
                node=Node(
                    self.handle_prompts_get, name=f"{namespace}.prompts_get"
                ),
                tags=["mcp", "prompts", namespace],
                extra={
                    "source": "mcp",
                    "namespace": namespace,
                    "tool_node": self,
                    "prompt_tool": True,
                },
            )
        )

        # Store tool name mappings
        self.tool_name_map[f"{namespace}.prompts_list"] = "prompts_list"
        self.tool_name_map[f"{namespace}.prompts_get"] = "prompts_get"

        logger.debug(
            "Generated %d prompt tools for '%s'",
            len(specs),
            namespace,
        )

        return specs

    async def handle_prompts_list(
        self,
        _args: object,
        ctx: ToolContext,
    ) -> dict[str, Any]:
        """Handler for prompts_list tool."""
        prompts = await self.list_prompts(ctx=ctx)
        return {
            "prompts": [p.model_dump() for p in prompts],
            "count": len(prompts),
        }

    async def handle_prompts_get(
        self,
        args: object,
        ctx: ToolContext,
    ) -> dict[str, Any]:
        """Handler for prompts_get tool."""
        name = getattr(
            args, "name", args.get("name") if isinstance(args, dict) else None
        )
        if not name:
            return {
                "messages": [],
                "description": "Missing required 'name' parameter",
            }

        arguments = (
            getattr(
                args,
                "arguments",
                args.get("arguments") if isinstance(args, dict) else None,
            )
            or {}
        )

        return await self.get_prompt(name, arguments, ctx=ctx)

    async def list_prompts(
        self, refresh: bool = False, *, ctx: ToolContext | None = None
    ) -> list[PromptInfo]:
        """List available MCP prompts.

        Args:
            refresh: Force refresh from server
            ctx: The caller's context; needed for per-user sessions.

        Returns:
            List of PromptInfo objects (empty if not connected or prompts not
            supported)
        """
        if not self.connected:
            return self._prompts

        needs_refresh = refresh or self.prompts_stale
        if not needs_refresh:
            return self._prompts

        async with self._optional_lease(ctx) as session:
            if session is not None:
                await self._refresh_prompts(session)
        return self._prompts

    async def _refresh_prompts(self, session: McpSession) -> None:
        try:
            self._prompts = [
                self._parse_prompt_info(p)
                for p in _as_list(await session.client.list_prompts())
            ]
            self._prompts_supported = True
            self.prompts_stale = False
        except Exception as e:
            logger.warning(f"Failed to refresh prompts: {e}")

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, str] | None = None,
        *,
        ctx: ToolContext | None = None,
    ) -> dict[str, Any]:
        """Execute a prompt and return rendered messages.

        Args:
            name: Prompt name
            arguments: Optional arguments for the prompt
            ctx: The caller's context; needed for per-user sessions.

        Returns:
            Dict with 'messages' (list of serialized messages) and
            'description'
        """
        if not self.connected:
            return {
                "messages": [],
                "description": f"ToolNode '{self.config.name}' not connected",
            }

        if not self._prompts_supported:
            return {
                "messages": [],
                "description": "Prompts not supported by "
                f"'{self.config.name}'",
            }

        async with self._optional_lease(ctx) as session:
            if session is None:
                return {
                    "messages": [],
                    "description": "MCP client not available",
                }
            return await self._render_prompt(session, name, arguments)

    @staticmethod
    async def _render_prompt(
        session: McpSession, name: str, arguments: dict[str, str] | None
    ) -> dict[str, Any]:
        try:
            result = await session.client.get_prompt(name, arguments)
            messages = serialize_prompt_messages(
                getattr(result, "messages", [])
            )
        except Exception as e:
            logger.warning(f"Failed to get prompt '{name}': {e}")
            return {"messages": [], "description": f"Error: {e}"}
        return {
            "messages": messages,
            "description": getattr(result, "description", None),
        }

    def handle_prompts_changed(self) -> None:
        """Handle a prompts/list_changed notification from MCP server.

        Clears cached prompts so they are re-discovered on next access.
        """
        self._prompts = []
        self.prompts_stale = True
        logger.debug("Prompts cache invalidated for '%s'", self.config.name)

    @property
    def prompts_supported(self) -> bool:
        """Check if MCP prompts are supported by this tool source."""
        return self._prompts_supported

    @prompts_supported.setter
    def prompts_supported(self, value: bool) -> None:
        self._prompts_supported = value

    @property
    def prompts(self) -> list[PromptInfo]:
        """Get cached list of prompts (call list_prompts for refresh)."""
        return self._prompts

    @prompts.setter
    def prompts(self, value: list[PromptInfo]) -> None:
        self._prompts = value


__all__ = ["ToolNode"]
