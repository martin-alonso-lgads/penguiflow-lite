"""Per-user MCP client sessions for ToolNode.

An MCP client fixes its HTTP headers when its session opens, and servers
bind the session to whoever opened it. A ``ToolNode`` whose source uses
``AuthType.OAUTH2_USER`` therefore opens one session per user, each with
that user's token, its own resource cache and its own subscriptions. Every
other auth type shares one session (:data:`SHARED_PRINCIPAL`).

:class:`McpSessionPool` holds those sessions. A session that is replaced,
evicted or closed leaves the pool at once, but its client is only closed
once no call is using it.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import (
        AsyncGenerator,
        Awaitable,
        Callable,
        Coroutine,
        Mapping,
    )

    from .resources import (
        ResourceCache,
        ResourceInfo,
        ResourceSubscriptionManager,
    )

__all__ = [
    "SHARED_PRINCIPAL",
    "McpSession",
    "McpSessionPool",
    "Principal",
    "auth_fingerprint",
    "principal_from_tool_context",
]

logger = logging.getLogger(__name__)

Principal = tuple[str, str]
"""``(tenant_id, user_id)`` of the user a session belongs to."""

SHARED_PRINCIPAL: Principal = ("", "")
"""Key of the one session shared by every caller (non-user auth)."""

_CLOSE_TIMEOUT_S = 5.0


def principal_from_tool_context(
    tool_context: Mapping[str, Any],
) -> Principal | None:
    """Return the ``(tenant_id, user_id)`` in ``tool_context``, if any.

    Args:
        tool_context: A run's tool context.

    Returns:
        The principal, or ``None`` when ``user_id`` is missing or empty.
    """
    user_id = tool_context.get("user_id")
    if not user_id:
        return None
    tenant_id = tool_context.get("tenant_id")
    return ("" if tenant_id is None else str(tenant_id), str(user_id))


def auth_fingerprint(headers: Mapping[str, str]) -> str:
    """Return a stable digest of ``headers``, so no raw token is kept.

    Args:
        headers: The auth headers a session was opened with.

    Returns:
        A hex SHA-256 digest of the sorted headers.
    """
    digest = hashlib.sha256()
    for key in sorted(headers):
        digest.update(key.encode())
        digest.update(b"\0")
        digest.update(headers[key].encode())
        digest.update(b"\0")
    return digest.hexdigest()


@dataclass(eq=False)
class McpSession:
    """One open MCP client and the state that belongs to its session.

    Attributes:
        principal: The user the session belongs to.
        client: The connected MCP client (a ``fastmcp.Client``).
        cache: Resource reads made through this session.
        subscriptions: Resource subscriptions made through this session.
        fingerprint: Digest of the auth headers the session was opened with.
        loop: Event loop the client was opened on.
        resources: This user's ``resources/list`` result, once read.
        last_used: Time of the last acquire or release.
        in_flight: Calls currently using the session.
        retired: Whether the session has left the pool.
    """

    principal: Principal
    client: Any
    cache: ResourceCache
    subscriptions: ResourceSubscriptionManager
    fingerprint: str = ""
    loop: asyncio.AbstractEventLoop | None = None
    resources: list[ResourceInfo] | None = None
    last_used: float = 0.0
    in_flight: int = 0
    retired: bool = False


async def _exit_client(session: McpSession) -> None:
    """Close ``session``'s client, dropping its cache and subscriptions."""
    session.cache.invalidate_all()
    dropped = session.subscriptions.clear()
    if dropped:
        logger.info(
            "mcp_session_subscriptions_dropped",
            extra={"uris": dropped, "count": len(dropped)},
        )
    with contextlib.suppress(Exception):
        await asyncio.wait_for(
            session.client.__aexit__(None, None, None),
            timeout=_CLOSE_TIMEOUT_S,
        )


def _current_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


@dataclass
class McpSessionPool:
    """The open MCP sessions of one ToolNode, keyed by principal.

    The shared session is never evicted. Per-user sessions are closed after
    ``idle_timeout_s`` without use and, past ``max_sessions``, least
    recently used first. A session with a call in flight, or one being
    opened, is never evicted.

    Attributes:
        max_sessions: Most per-user sessions kept open.
        idle_timeout_s: Seconds without use before a per-user session is
            closed (0 = never).
        time_source: Clock for ``last_used``.
    """

    max_sessions: int = 64
    idle_timeout_s: float = 900.0
    time_source: Callable[[], float] = time.monotonic
    _sessions: OrderedDict[Principal, McpSession] = field(
        default_factory=OrderedDict, init=False, repr=False
    )
    _locks: dict[Principal, asyncio.Lock] = field(
        default_factory=dict, init=False, repr=False
    )
    _closing: set[asyncio.Task[None]] = field(
        default_factory=set, init=False, repr=False
    )

    def __len__(self) -> int:
        """Return the number of open sessions."""
        return len(self._sessions)

    def get(self, principal: Principal) -> McpSession | None:
        """Return the open session of ``principal``, if any."""
        return self._sessions.get(principal)

    def sessions(self) -> list[McpSession]:
        """Return the open sessions."""
        return list(self._sessions.values())

    def install(self, session: McpSession) -> None:
        """Put ``session`` in the pool, retiring the one it replaces."""
        current = self._sessions.get(session.principal)
        if current is not None and current is not session:
            self.retire(current)
        session.retired = False
        session.last_used = self.time_source()
        self._sessions[session.principal] = session
        self._sessions.move_to_end(session.principal)

    def discard(self, principal: Principal) -> McpSession | None:
        """Remove ``principal``'s session without closing its client."""
        return self._sessions.pop(principal, None)

    async def acquire(
        self,
        principal: Principal,
        fingerprint: str,
        opener: Callable[[], Awaitable[McpSession]],
    ) -> McpSession:
        """Return ``principal``'s session, opening one when needed.

        An open session is reused when it was opened on the running loop
        (or on no loop) and, for a per-user session, with the same auth.
        Otherwise it is retired and ``opener`` opens a new one. Concurrent
        calls for one principal open at most one session.

        Args:
            principal: The user the session is for.
            fingerprint: :func:`auth_fingerprint` of the user's headers.
            opener: Opens a new session.

        Returns:
            The session, already in the pool.
        """
        lock = self._locks.setdefault(principal, asyncio.Lock())
        async with lock:
            session = self._reusable(principal, fingerprint)
            if session is None:
                session = await opener()
                session.fingerprint = fingerprint
                session.loop = _current_loop()
                self.install(session)
            else:
                self._touch(session)
        self.enforce_limits(keep=principal)
        return session

    def _reusable(
        self, principal: Principal, fingerprint: str
    ) -> McpSession | None:
        session = self._sessions.get(principal)
        if session is None:
            return None
        loop = _current_loop()
        same_loop = session.loop is None or session.loop is loop
        same_auth = (
            principal == SHARED_PRINCIPAL or session.fingerprint == fingerprint
        )
        if same_loop and same_auth:
            if session.loop is None:
                session.loop = loop
            return session
        self.retire(session)
        return None

    def _touch(self, session: McpSession) -> None:
        session.last_used = self.time_source()
        if self._sessions.get(session.principal) is session:
            self._sessions.move_to_end(session.principal)

    @contextlib.asynccontextmanager
    async def lease(
        self, session: McpSession
    ) -> AsyncGenerator[McpSession, None]:
        """Mark ``session`` in use while the block runs.

        A session retired during the block is closed when it ends.
        """
        session.in_flight += 1
        try:
            yield session
        finally:
            session.in_flight -= 1
            self._touch(session)
            if session.retired and session.in_flight == 0:
                self._schedule_close(session)

    def retire(self, session: McpSession) -> None:
        """Take ``session`` out of the pool and close it once unused."""
        if self._sessions.get(session.principal) is session:
            del self._sessions[session.principal]
        if session.retired:
            return
        session.retired = True
        if session.in_flight == 0:
            self._schedule_close(session)

    def enforce_limits(self, *, keep: Principal) -> None:
        """Close idle per-user sessions and apply ``max_sessions``.

        Args:
            keep: A principal whose session must stay open.
        """
        now = self.time_source()
        if self.idle_timeout_s > 0:
            for session in self._evictable(keep):
                if now - session.last_used >= self.idle_timeout_s:
                    self.retire(session)
        while self._per_user_count() > self.max_sessions:
            victims = self._evictable(keep)
            if not victims:
                break
            self.retire(victims[0])

    def _per_user_count(self) -> int:
        return sum(p != SHARED_PRINCIPAL for p in self._sessions)

    def _evictable(self, keep: Principal) -> list[McpSession]:
        """Return idle per-user sessions, least recently used first."""
        return [
            session
            for principal, session in self._sessions.items()
            if principal not in (keep, SHARED_PRINCIPAL)
            and session.in_flight == 0
            and not self._opening(principal)
        ]

    def _opening(self, principal: Principal) -> bool:
        lock = self._locks.get(principal)
        return lock is not None and lock.locked()

    def _schedule_close(self, session: McpSession) -> None:
        """Close ``session``'s client in the background, on its own loop."""
        loop = _current_loop()
        owner = session.loop
        if owner is not None and owner is not loop:
            _close_on_owner_loop(session, owner)
            return
        if loop is None:
            return
        task = loop.create_task(_exit_client(session))
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    async def close_all(self) -> None:
        """Close every session and wait for pending closes."""
        sessions = list(self._sessions.values())
        self._sessions.clear()
        self._locks.clear()
        loop = _current_loop()
        here: list[Coroutine[Any, Any, None]] = []
        for session in sessions:
            session.retired = True
            if session.loop is None or session.loop is loop:
                here.append(_exit_client(session))
            else:
                _close_on_owner_loop(session, session.loop)
        pending = [*here, *self._drain_closing()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def _drain_closing(self) -> list[asyncio.Task[None]]:
        tasks = [task for task in self._closing if not task.done()]
        self._closing.clear()
        return tasks

    def detach(self) -> None:
        """Drop every session after an event loop change.

        Clients opened on a loop that is still running are closed there;
        the rest are dropped, since their loop can no longer run them.
        """
        sessions = list(self._sessions.values())
        self._sessions.clear()
        self._locks.clear()
        self._closing.clear()
        for session in sessions:
            session.retired = True
            if session.loop is not None:
                _close_on_owner_loop(session, session.loop)


def _close_on_owner_loop(
    session: McpSession, owner: asyncio.AbstractEventLoop
) -> None:
    """Close ``session`` on ``owner`` when that loop still runs."""
    if owner.is_closed() or not owner.is_running():
        session.cache.invalidate_all()
        session.subscriptions.clear()
        return
    asyncio.run_coroutine_threadsafe(_exit_client(session), owner)
