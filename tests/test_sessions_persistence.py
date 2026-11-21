"""Tests for sessions persistence."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from typing_extensions import override

from penguiflow.sessions.models import (
    StateUpdate,
    TaskContextSnapshot,
    TaskState,
    TaskStatus,
    TaskType,
    UpdateType,
)
from penguiflow.sessions.persistence import (
    InMemorySessionStateStore,
    StateStoreSessionAdapter,
)
from penguiflow.sessions.session import StreamingSession
from penguiflow.state import (
    RemoteBinding,
    StateStore,
    StoredEvent,
    SupportsTasks,
)
from penguiflow.steering import SteeringEvent, SteeringEventType

from .state_store_doubles import RecordingStateStore

if TYPE_CHECKING:
    from collections.abc import Sequence


class InMemoryStateStore(StateStore):
    """State store that keeps events in memory."""

    def __init__(self) -> None:
        """Start with no recorded state."""
        self.events: list[StoredEvent] = []

    @override
    async def save_event(self, event: StoredEvent) -> None:
        self.events.append(event)

    @override
    async def load_history(self, trace_id: str) -> Sequence[StoredEvent]:
        return [event for event in self.events if event.trace_id == trace_id]

    @override
    async def save_remote_binding(self, binding: RemoteBinding) -> None:
        return


@pytest.mark.asyncio
async def test_inmemory_session_state_store_lists_with_filters() -> None:
    """Inmemory session state store lists with filters."""
    store = InMemorySessionStateStore(
        max_updates_per_session=0, max_steering_events_per_session=0
    )
    session_id = "s-persist"
    await store.save_update(
        StateUpdate(
            session_id=session_id,
            task_id="t1",
            update_type=UpdateType.RESULT,
            content={"ok": True},
        )
    )
    await store.save_update(
        StateUpdate(
            session_id=session_id,
            task_id="t2",
            update_type=UpdateType.ERROR,
            content={"ok": False},
        )
    )
    updates = await store.list_updates(session_id, task_id="t1")
    assert [u.task_id for u in updates] == ["t1"]

    first_id = updates[0].update_id
    await store.save_update(
        StateUpdate(
            session_id=session_id,
            task_id="t1",
            update_type=UpdateType.PROGRESS,
            content={"n": 1},
        )
    )
    updates_since = await store.list_updates(session_id, since_id=first_id)
    assert len(updates_since) == 2

    event1 = SteeringEvent(
        session_id=session_id,
        task_id="t1",
        event_type=SteeringEventType.CANCEL,
        payload={},
    )
    event2 = SteeringEvent(
        session_id=session_id,
        task_id="t2",
        event_type=SteeringEventType.PAUSE,
        payload={},
    )
    await store.save_steering(event1)
    await store.save_steering(event2)
    steering = await store.list_steering(session_id, task_id="t2")
    assert [e.task_id for e in steering] == ["t2"]

    steering_since = await store.list_steering(
        session_id, since_id=event1.event_id
    )
    assert [e.event_id for e in steering_since] == [event2.event_id]


@pytest.mark.asyncio
async def test_state_store_session_adapter_roundtrip() -> None:
    """State store session adapter roundtrip."""
    core_store = InMemoryStateStore()
    adapter = StateStoreSessionAdapter(core_store)

    snapshot = TaskContextSnapshot(
        session_id="s1", task_id="t1", llm_context={"k": "v"}
    )
    state = TaskState(
        task_id="t1",
        session_id="s1",
        status=TaskStatus.PENDING,
        task_type=TaskType.BACKGROUND,
        priority=0,
        context_snapshot=snapshot,
    )
    update = StateUpdate(
        session_id="s1",
        task_id="t1",
        update_type=UpdateType.STATUS_CHANGE,
        content={"s": "x"},
    )
    steering = SteeringEvent(
        session_id="s1",
        task_id="t1",
        event_type=SteeringEventType.REDIRECT,
        payload={"instruction": "new"},
        source="user",
    )

    await adapter.save_task(state)
    await adapter.save_update(update)
    await adapter.save_steering(steering)

    tasks = await adapter.list_tasks("s1")
    assert len(tasks) == 1
    assert tasks[0].task_id == "t1"
    assert tasks[0].context_snapshot.llm_context["k"] == "v"

    updates = await adapter.list_updates("s1", task_id="t1")
    assert len(updates) == 1
    assert updates[0].update_type == UpdateType.STATUS_CHANGE

    steering_events = await adapter.list_steering("s1")
    assert len(steering_events) == 1
    assert steering_events[0].event_type == SteeringEventType.REDIRECT

    extra = StoredEvent(
        trace_id="session:s1",
        ts=0.0,
        kind="other",
        node_name=None,
        node_id=None,
        payload={},
    )
    bad_payload = StoredEvent(
        trace_id="session:s1",
        ts=0.0,
        kind="session.update",
        node_name=None,
        node_id="t1",
        # A corrupted payload, as read back from storage.
        payload=json.loads('"x"'),
    )
    core_store.events.extend([extra, bad_payload])
    updates = await adapter.list_updates("s1")
    assert len(updates) == 1


@pytest.fixture(params=["in_memory", "adapter"])
def session_store(
    request: pytest.FixtureRequest,
) -> InMemorySessionStateStore | StateStoreSessionAdapter:
    """A fresh session store of each implementation."""
    if request.param == "in_memory":
        return InMemorySessionStateStore()
    return StateStoreSessionAdapter(InMemoryStateStore())


@pytest.mark.asyncio
async def test_update_cursor_on_other_task_applies_before_task_filter(
    session_store: InMemorySessionStateStore | StateStoreSessionAdapter,
) -> None:
    """A ``since_id`` on another task's update still moves the cursor."""
    updates = [
        StateUpdate(
            session_id="s1",
            task_id=task_id,
            update_type=UpdateType.STATUS_CHANGE,
            content={"n": index},
        )
        for index, task_id in enumerate(["a", "a", "b", "a"])
    ]
    for update in updates:
        await session_store.save_update(update)

    listed = await session_store.list_updates(
        "s1", task_id="a", since_id=updates[2].update_id
    )

    assert [u.update_id for u in listed] == [updates[3].update_id]


@pytest.mark.asyncio
async def test_steering_cursor_on_other_task_applies_before_task_filter(
    session_store: InMemorySessionStateStore | StateStoreSessionAdapter,
) -> None:
    """A steering ``since_id`` on another task still moves the cursor."""
    events = [
        SteeringEvent(
            session_id="s1",
            task_id=task_id,
            event_type=SteeringEventType.REDIRECT,
            payload={"instruction": str(index)},
            source="user",
        )
        for index, task_id in enumerate(["a", "a", "b", "a"])
    ]
    for event in events:
        await session_store.save_steering(event)

    listed = await session_store.list_steering(
        "s1", task_id="a", since_id=events[2].event_id
    )

    assert [e.event_id for e in listed] == [events[3].event_id]


class _TaskOnlyStore(RecordingStateStore):
    """Core store with ``save_task`` and no other session method."""

    def __init__(self) -> None:
        super().__init__()
        self.tasks: list[TaskState] = []

    async def save_task(self, state: TaskState) -> None:
        """Record ``state``."""
        self.tasks.append(state)


class _TasksStore(RecordingStateStore, SupportsTasks):
    """Core store with ``SupportsTasks`` but no ``SupportsSteering``."""

    def __init__(self) -> None:
        super().__init__()
        self.tasks: list[TaskState] = []
        self.updates: list[StateUpdate] = []

    @override
    async def save_task(self, state: TaskState) -> None:
        self.tasks.append(state)

    @override
    async def list_tasks(self, session_id: str) -> Sequence[TaskState]:
        return [task for task in self.tasks if task.session_id == session_id]

    @override
    async def save_update(self, update: StateUpdate) -> None:
        self.updates.append(update)

    @override
    async def list_updates(
        self,
        session_id: str,
        *,
        task_id: str | None = None,
        since_id: str | None = None,
        limit: int = 500,
    ) -> Sequence[StateUpdate]:
        selected = [
            update
            for update in self.updates
            if update.session_id == session_id
            and (task_id is None or update.task_id == task_id)
        ]
        if since_id is not None:
            ids = [update.update_id for update in selected]
            if since_id in ids:
                selected = selected[ids.index(since_id) + 1 :]
        return selected[:limit]


def test_session_rejects_store_without_steering_at_construction() -> None:
    """A core + ``SupportsTasks`` store fails when the session is built."""
    with pytest.raises(TypeError, match="save_steering") as raised:
        StreamingSession("s", state_store=_TasksStore())

    assert "list_steering" in str(raised.value)


def test_session_rejects_store_with_only_save_task() -> None:
    """A store with only ``save_task`` names every missing method."""
    with pytest.raises(TypeError) as raised:
        StreamingSession("s", state_store=_TaskOnlyStore())

    message = str(raised.value)
    for method in (
        "save_update",
        "save_steering",
        "list_tasks",
        "list_updates",
        "list_steering",
    ):
        assert method in message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "store",
    [InMemorySessionStateStore(), RecordingStateStore()],
    ids=["session-store", "core-store"],
)
async def test_session_accepts_full_and_core_stores(
    store: InMemorySessionStateStore | RecordingStateStore,
) -> None:
    """A full session store, or a core store via the adapter, works."""
    session = StreamingSession("s", state_store=store)

    await session.hydrate()
