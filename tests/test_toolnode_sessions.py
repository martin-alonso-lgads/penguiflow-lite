"""Tests for ToolNode's per-user MCP sessions (``OAUTH2_USER``)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import mcp.types as mcp_types
import pytest
from pydantic import AnyUrl

from penguiflow.registry import ModelRegistry
from penguiflow.tools.auth import (
    InMemoryTokenStore,
    OAuthManager,
    OAuthProviderConfig,
)
from penguiflow.tools.config import AuthType, ExternalToolConfig
from penguiflow.tools.errors import (
    ToolAuthError,
    ToolConnectionError,
    ToolNodeError,
)
from penguiflow.tools.node import McpMessageRouter, ToolNode

from .tool_context_doubles import FakeToolContext

if TYPE_CHECKING:
    from collections.abc import Callable

    from penguiflow.tools.mcp_pool import Principal

pytest.importorskip("tenacity")

URL = "https://mcp.example.com/mcp"


class FakeTool:
    """An MCP tool as the server lists it."""

    def __init__(self, name: str) -> None:
        """Describe a tool called ``name`` with no arguments."""
        self.name = name
        self.description = f"The {name} tool"
        self.inputSchema: dict[str, object] = {"properties": {}}


class _UnauthorizedError(Exception):
    """A server rejection carrying HTTP 401."""

    status_code = 401


class FakeSession:
    """MCP session double that records resource subscriptions."""

    def __init__(self) -> None:
        """Start with no recorded calls."""
        self.subscribed: list[str] = []

    async def subscribe_resource(self, uri: AnyUrl) -> None:
        """Record a subscription to ``uri``."""
        self.subscribed.append(str(uri))

    async def unsubscribe_resource(self, uri: AnyUrl) -> None:
        """Record an unsubscription from ``uri``."""
        self.subscribed.remove(str(uri))


class FakeMcpClient:
    """MCP client double bound to the headers it was opened with."""

    def __init__(
        self, headers: dict[str, str], router: McpMessageRouter
    ) -> None:
        """Record the opening headers and the session's message router."""
        self.headers = dict(headers)
        self.router = router
        self.session = FakeSession()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.reads: list[str] = []
        self.prompt_calls: list[str] = []
        self.exited = False
        self.exit_info: tuple[object, ...] = ()
        self.fail_next_call: Exception | None = None
        self.release: asyncio.Event | None = None
        self.waiting = asyncio.Event()

    @property
    def token(self) -> str:
        """The bearer token this client sends."""
        return self.headers.get("Authorization", "").removeprefix("Bearer ")

    async def list_tools(self) -> list[FakeTool]:
        """List one tool."""
        return [FakeTool("whoami")]

    async def list_resources(self) -> list[dict[str, str]]:
        """List one resource named after the token's user."""
        return [{"uri": f"file:///{self.token}.txt", "name": self.token}]

    async def list_resource_templates(self) -> list[dict[str, str]]:
        """List no templates."""
        return []

    async def list_prompts(self) -> list[dict[str, str]]:
        """List one prompt."""
        return [{"name": "greet"}]

    async def call_tool(
        self, name: str, args: dict[str, Any]
    ) -> dict[str, object]:
        """Return the token the call was made with."""
        self.calls.append((name, args))
        if self.release is not None:
            self.waiting.set()
            await self.release.wait()
        if self.fail_next_call is not None:
            error, self.fail_next_call = self.fail_next_call, None
            raise error
        return {"token": self.token}

    async def read_resource(self, uri: str) -> dict[str, object]:
        """Return text that names the token's user."""
        self.reads.append(uri)
        return {"contents": [{"uri": uri, "text": f"secret of {self.token}"}]}

    async def get_prompt(
        self, name: str, arguments: dict[str, str] | None
    ) -> mcp_types.GetPromptResult:
        """Render a prompt that names the token's user."""
        self.prompt_calls.append(name)
        text = f"{name} {self.token} {sorted((arguments or {}).items())}"
        return mcp_types.GetPromptResult(
            messages=[
                mcp_types.PromptMessage(
                    role="user",
                    content=mcp_types.TextContent(type="text", text=text),
                )
            ]
        )

    async def __aexit__(self, *exc_info: object) -> None:
        """Record that the client was closed, and how."""
        self.exit_info = exc_info
        self.exited = True


class Clock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        """Start at 1000 seconds."""
        self.now = 1000.0

    def __call__(self) -> float:
        """Return the current time."""
        return self.now


class Harness:
    """A per-user ToolNode whose MCP clients are fakes."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        clock: Callable[[], float] | None = None,
        **overrides: object,
    ) -> None:
        """Build the node, a token store and the fake client factory."""
        base: dict[str, object] = {
            "name": "docs",
            "transport": "mcp",
            "connection": URL,
            "auth_type": AuthType.OAUTH2_USER,
        }
        base.update(overrides)
        self.store = InMemoryTokenStore()
        manager = OAuthManager(
            providers={
                "docs": OAuthProviderConfig(
                    name="docs",
                    display_name="Docs",
                    auth_url="https://auth.example.com/authorize",
                    token_url="https://auth.example.com/token",
                    client_id="id",
                    client_secret="secret",
                    redirect_uri="https://app.example.com/cb",
                )
            },
            token_store=self.store,
        )
        options: dict[str, Any] = {}
        if clock is not None:
            options["time_source"] = clock
        self.node = ToolNode(
            config=ExternalToolConfig.model_validate(base),
            registry=ModelRegistry(),
            auth_manager=manager,
            **options,
        )
        self.clients: list[FakeMcpClient] = []
        self.open_delay = 0.0
        self.open_error: Exception | None = None
        monkeypatch.setattr(self.node, "open_mcp_client", self._open)

    async def _open(
        self, auth_headers: dict[str, str], router: McpMessageRouter
    ) -> FakeMcpClient:
        if self.open_delay:
            await asyncio.sleep(self.open_delay)
        if self.open_error is not None:
            # Like the real opener, wrap the server's rejection.
            msg = f"Failed to connect: {self.open_error}"
            raise ToolConnectionError(msg) from self.open_error
        client = FakeMcpClient(auth_headers, router)
        self.clients.append(client)
        return client

    async def login(self, user: str, token: str | None = None) -> None:
        """Store a token for ``user`` (``tok-<user>`` by default)."""
        await self.store.store(user, "docs", token or f"tok-{user}", None)

    def ctx(self, user: str, tenant: str = "acme") -> FakeToolContext:
        """Return a context for ``user`` in ``tenant``."""
        return FakeToolContext(
            tool_context={"user_id": user, "tenant_id": tenant}
        )

    def client_for(self, token: str) -> FakeMcpClient:
        """Return the open client that sends ``token``."""
        matches = [c for c in self.clients if c.token == token]
        assert matches, f"no client opened with {token}"
        return matches[-1]


async def _call(harness: Harness, user: str) -> object:
    return await harness.node.call("docs.whoami", {}, harness.ctx(user))


@pytest.mark.asyncio
async def test_two_users_get_own_sessions_and_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each user's calls go out with their own token."""
    harness = Harness(monkeypatch)
    await harness.login("u1")
    await harness.login("u2")
    await harness.node.connect(harness.ctx("u1"))

    first = await _call(harness, "u1")
    second = await _call(harness, "u2")

    assert first == {"result": {"token": "tok-u1"}}
    assert second == {"result": {"token": "tok-u2"}}
    assert harness.node.mcp_session_count == 2
    assert harness.node.mcp_client is None


@pytest.mark.asyncio
async def test_same_user_in_other_tenant_gets_own_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The session key includes the tenant."""
    harness = Harness(monkeypatch)
    await harness.login("u1")
    await harness.node.connect(harness.ctx("u1", tenant="acme"))

    await harness.node.call("docs.whoami", {}, harness.ctx("u1", "globex"))

    assert harness.node.mcp_session_count == 2


@pytest.mark.asyncio
async def test_resource_reads_cached_per_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One user's cached read is never served to another user."""
    harness = Harness(monkeypatch)
    await harness.login("u1")
    await harness.login("u2")
    await harness.node.connect(harness.ctx("u1"))

    uri = "file:///shared.txt"
    u1_read = await harness.node.read_resource(uri, harness.ctx("u1"))
    u2_read = await harness.node.read_resource(uri, harness.ctx("u2"))
    u1_again = await harness.node.read_resource(uri, harness.ctx("u1"))

    assert u1_read == {"text": "secret of tok-u1"}
    assert u2_read == {"text": "secret of tok-u2"}
    assert u1_again == u1_read
    assert harness.client_for("tok-u1").reads == [uri]
    assert harness.client_for("tok-u2").reads == [uri]


@pytest.mark.asyncio
async def test_token_change_replaces_session_and_clears_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new token for the same user opens a new session."""
    harness = Harness(monkeypatch)
    await harness.login("u1", "old-token")
    await harness.node.connect(harness.ctx("u1"))
    uri = "file:///doc.txt"
    await harness.node.read_resource(uri, harness.ctx("u1"))
    old_client = harness.client_for("old-token")

    await harness.login("u1", "new-token")
    result = await _call(harness, "u1")
    reread = await harness.node.read_resource(uri, harness.ctx("u1"))
    await asyncio.sleep(0)

    assert result == {"result": {"token": "new-token"}}
    assert reread == {"text": "secret of new-token"}
    assert old_client.exited is True
    assert harness.node.mcp_session_count == 1


@pytest.mark.asyncio
async def test_401_discards_session_and_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 401 closes the session and deletes the token, so consent runs."""
    harness = Harness(monkeypatch)
    await harness.login("u1")
    await harness.node.connect(harness.ctx("u1"))
    first_client = harness.client_for("tok-u1")
    first_client.fail_next_call = _UnauthorizedError("token revoked")

    with pytest.raises(ToolAuthError):
        await _call(harness, "u1")
    await asyncio.sleep(0)
    ctx = harness.ctx("u1")
    with pytest.raises(ToolAuthError, match="was not completed"):
        await harness.node.call("docs.whoami", {}, ctx)

    assert first_client.exited is True
    assert await harness.store.get("u1", "docs") is None
    assert ctx.paused_payload is not None
    pause_payload = ctx.paused_payload["payload"]
    assert isinstance(pause_payload, dict)
    assert pause_payload["pause_type"] == "oauth"


@pytest.mark.asyncio
async def test_after_401_new_token_opens_new_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the user consents again, the next call uses the new token."""
    harness = Harness(monkeypatch)
    await harness.login("u1")
    await harness.node.connect(harness.ctx("u1"))
    harness.client_for("tok-u1").fail_next_call = _UnauthorizedError(
        "token revoked"
    )
    with pytest.raises(ToolAuthError):
        await _call(harness, "u1")

    await harness.login("u1", "tok-u1-new")
    result = await _call(harness, "u1")

    assert result == {"result": {"token": "tok-u1-new"}}
    assert len(harness.clients) == 2


@pytest.mark.asyncio
async def test_401_keeps_a_token_stored_since(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 401 never deletes a token newer than the rejected one."""
    harness = Harness(monkeypatch)
    await harness.login("u1")
    await harness.node.connect(harness.ctx("u1"))
    client = harness.client_for("tok-u1")
    client.release = asyncio.Event()
    client.fail_next_call = _UnauthorizedError("token revoked")

    call = asyncio.create_task(_call(harness, "u1"))
    await client.waiting.wait()  # the call is now inside the server
    await harness.login("u1", "tok-u1-new")
    client.release.set()
    with pytest.raises(ToolAuthError):
        await call

    assert await harness.store.get("u1", "docs") == "tok-u1-new"


@pytest.mark.asyncio
async def test_401_while_opening_deletes_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token the server rejects at connect time is not reused either."""
    harness = Harness(monkeypatch)
    await harness.login("u1")
    harness.open_error = _UnauthorizedError("token revoked")

    with pytest.raises(ToolConnectionError):
        await _call(harness, "u1")

    assert await harness.store.get("u1", "docs") is None


@pytest.mark.asyncio
async def test_other_open_failure_keeps_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connect failure that is not a 401 leaves the token stored."""
    harness = Harness(monkeypatch)
    await harness.login("u1")
    harness.open_error = RuntimeError("server down")

    with pytest.raises(ToolConnectionError):
        await _call(harness, "u1")

    assert await harness.store.get("u1", "docs") == "tok-u1"


@pytest.mark.asyncio
async def test_call_connects_lazily_with_ctx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """call() on an unconnected node connects with the caller's context."""
    harness = Harness(monkeypatch)
    await harness.login("u1")

    result = await _call(harness, "u1")

    assert result == {"result": {"token": "tok-u1"}}
    assert harness.node.connected is True


def test_call_on_new_loop_reconnects_with_ctx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call on a new event loop reconnects with the caller's context."""
    harness = Harness(monkeypatch)
    asyncio.run(harness.login("u1"))
    asyncio.run(harness.node.connect(harness.ctx("u1")))

    result = asyncio.run(_call(harness, "u1"))

    assert result == {"result": {"token": "tok-u1"}}
    assert len(harness.clients) == 2


@pytest.mark.asyncio
async def test_connect_without_ctx_rejected_for_user_oauth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-user auth needs a context to know whose session to open."""
    harness = Harness(monkeypatch)

    with pytest.raises(ToolAuthError, match="no ToolContext provided"):
        await harness.node.connect()


@pytest.mark.asyncio
async def test_call_without_user_id_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call with no ``user_id`` cannot pick a session."""
    harness = Harness(monkeypatch)
    await harness.login("u1")
    await harness.node.connect(harness.ctx("u1"))

    with pytest.raises(ToolAuthError, match="user_id required"):
        await harness.node.call("docs.whoami", {}, FakeToolContext())


@pytest.mark.asyncio
async def test_concurrent_first_calls_open_one_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent first calls for one user open a single session."""
    harness = Harness(monkeypatch)
    await harness.login("u1")
    await harness.login("u2")
    await harness.node.connect(harness.ctx("u1"))
    harness.open_delay = 0.01

    await asyncio.gather(*(_call(harness, "u2") for _ in range(3)))

    assert [c.token for c in harness.clients] == ["tok-u1", "tok-u2"]


@pytest.mark.asyncio
async def test_idle_session_closed_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A per-user session unused past the idle timeout is closed."""
    clock = Clock()
    harness = Harness(
        monkeypatch, clock=clock, session_pool={"idle_timeout_s": 60}
    )
    await harness.login("u1")
    await harness.login("u2")
    await harness.node.connect(harness.ctx("u1"))

    clock.now += 61
    await _call(harness, "u2")
    await asyncio.sleep(0)

    assert harness.client_for("tok-u1").exited is True
    assert harness.node.mcp_session_count == 1


@pytest.mark.asyncio
async def test_lru_evicts_least_recent_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past ``max_sessions`` the least recently used session closes."""
    clock = Clock()
    harness = Harness(
        monkeypatch, clock=clock, session_pool={"max_sessions": 2}
    )
    for user in ("u1", "u2", "u3"):
        await harness.login(user)
    await harness.node.connect(harness.ctx("u1"))
    clock.now += 1
    await _call(harness, "u2")
    clock.now += 1
    await _call(harness, "u1")
    clock.now += 1

    await _call(harness, "u3")
    await asyncio.sleep(0)

    assert harness.client_for("tok-u2").exited is True
    assert harness.client_for("tok-u1").exited is False
    assert harness.node.mcp_session_count == 2


@pytest.mark.asyncio
async def test_in_flight_session_not_evicted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session with a call in flight stays open past the cap."""
    harness = Harness(monkeypatch, session_pool={"max_sessions": 1})
    await harness.login("u1")
    await harness.login("u2")
    await harness.node.connect(harness.ctx("u1"))
    u1_client = harness.client_for("tok-u1")
    u1_client.release = asyncio.Event()

    slow_call = asyncio.create_task(_call(harness, "u1"))
    await asyncio.sleep(0.01)
    await _call(harness, "u2")
    assert u1_client.exited is False

    u1_client.release.set()
    assert await slow_call == {"result": {"token": "tok-u1"}}


@pytest.mark.asyncio
async def test_close_closes_every_session_and_resets_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """close() exits every client and forgets discovered state."""
    harness = Harness(monkeypatch)
    await harness.login("u1")
    await harness.login("u2")
    await harness.node.connect(harness.ctx("u1"))
    await _call(harness, "u2")

    await harness.node.close()

    assert all(client.exited for client in harness.clients)
    assert harness.node.mcp_session_count == 0
    assert harness.node.connected is False
    assert harness.node.get_tools() == []
    assert harness.node.resources_supported is False


@pytest.mark.asyncio
async def test_update_notification_invalidates_only_that_users_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server's update reaches only the session it was sent on."""
    harness = Harness(monkeypatch)
    await harness.login("u1")
    await harness.login("u2")
    await harness.node.connect(harness.ctx("u1"))
    uri = "file:///doc.txt"
    await harness.node.read_resource(uri, harness.ctx("u1"))
    await harness.node.read_resource(uri, harness.ctx("u2"))
    updated: list[tuple[str, Principal | None]] = []
    harness.node.set_resource_updated_callback(
        lambda uri, principal: updated.append((uri, principal))
    )

    await harness.client_for("tok-u1").router(
        mcp_types.ServerNotification(
            mcp_types.ResourceUpdatedNotification(
                params=mcp_types.ResourceUpdatedNotificationParams(
                    uri=AnyUrl(uri)
                )
            )
        )
    )
    await harness.node.read_resource(uri, harness.ctx("u1"))
    await harness.node.read_resource(uri, harness.ctx("u2"))

    assert updated == [(uri, ("acme", "u1"))]
    assert harness.client_for("tok-u1").reads == [uri, uri]
    assert harness.client_for("tok-u2").reads == [uri]


@pytest.mark.asyncio
async def test_force_reconnect_exits_old_client_and_drops_subscriptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subscriptions end with their session; the new one needs its own."""
    harness = Harness(monkeypatch)
    await harness.login("u1")
    ctx = harness.ctx("u1")
    await harness.node.connect(ctx)
    uri = "file:///doc.txt"
    assert await harness.node.subscribe_resource(uri, ctx=ctx) is True
    old_client = harness.client_for("tok-u1")

    await harness.node.force_reconnect(ctx)
    assert await harness.node.subscribe_resource(uri, ctx=ctx) is True

    new_client = harness.clients[-1]
    assert old_client.exited is True
    assert new_client is not old_client
    assert new_client.session.subscribed == [uri]


@pytest.mark.asyncio
async def test_resources_list_is_per_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``resources/list`` results are each user's own."""
    harness = Harness(monkeypatch)
    await harness.login("u1")
    await harness.login("u2")
    await harness.node.connect(harness.ctx("u1"))

    u1_list = await harness.node.list_resources(ctx=harness.ctx("u1"))
    u2_list = await harness.node.list_resources(ctx=harness.ctx("u2"))

    assert [r.uri for r in u1_list] == ["file:///tok-u1.txt"]
    assert [r.uri for r in u2_list] == ["file:///tok-u2.txt"]
    assert harness.node.resources == []


@pytest.mark.asyncio
async def test_get_prompt_uses_callers_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prompts render through the caller's own session."""
    harness = Harness(monkeypatch)
    await harness.login("u1")
    await harness.login("u2")
    await harness.node.connect(harness.ctx("u1"))

    rendered = await harness.node.get_prompt(
        "greet", {"who": "x"}, ctx=harness.ctx("u2")
    )

    assert harness.client_for("tok-u2").prompt_calls == ["greet"]
    assert harness.client_for("tok-u1").prompt_calls == []
    assert "tok-u2" in str(rendered["messages"])


@pytest.mark.asyncio
async def test_mcp_client_setter_rejected_in_user_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-user nodes have no shared client to set."""
    harness = Harness(monkeypatch)

    with pytest.raises(ToolNodeError, match="per-user MCP sessions"):
        harness.node.mcp_client = object()


@pytest.mark.asyncio
async def test_shared_auth_uses_one_session_for_all_users(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Static auth keeps one shared session whoever calls."""
    harness = Harness(
        monkeypatch,
        auth_type=AuthType.BEARER,
        auth_config={"token": "static"},
    )
    await harness.node.connect()

    first = await _call(harness, "u1")
    second = await _call(harness, "u2")

    assert first == second == {"result": {"token": "static"}}
    assert harness.node.mcp_session_count == 1
    assert harness.node.mcp_client is harness.clients[0]
