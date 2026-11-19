# SSE, WebSocket, and Session Bridges

There is no built-in SSE/WebSocket formatter; the wire format is a few lines of your own code. The pattern is always the same: the planner's synchronous `event_callback` enqueues events into a per-request `asyncio.Queue`, and the HTTP/WS handler drains it.

The recipes below assume a `make_planner(event_callback=None)` factory of your own that returns a `ReactPlanner` with `stream_final_response=True` (`PlannerTaskPipeline` calls it with no arguments and installs its own callback).

## Local helpers

```python
import json

from penguiflow.planner import PlannerEvent, PlannerFinish, PlannerPause


def sse(event: str, data: str, *, event_id: str | None = None) -> str:
    lines = [f"event: {event}"]
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.extend(f"data: {line}" for line in data.splitlines() or [""])
    return "\n".join(lines) + "\n\n"


def event_to_json(event: PlannerEvent) -> str:
    return json.dumps(event.to_payload(), default=str)


def outcome_to_json(result: PlannerFinish | PlannerPause) -> str:
    if isinstance(result, PlannerPause):
        return json.dumps(
            {
                "event": "paused",
                "reason": result.reason,
                "resume_token": result.resume_token,
                "payload": result.payload,
            },
            default=str,
        )
    return json.dumps(
        {"event": "final", "reason": result.reason, "payload": result.payload},
        default=str,
    )
```

## FastAPI SSE recipe (answer tokens + status + final)

```python
import asyncio

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from penguiflow.planner import PlannerEvent

app = FastAPI()


@app.get("/chat")
async def chat(q: str, session_id: str) -> StreamingResponse:
    queue: asyncio.Queue[PlannerEvent | None] = asyncio.Queue()
    planner = make_planner(event_callback=queue.put_nowait)  # per request

    async def gen():
        task = asyncio.create_task(
            planner.run(q, tool_context={"session_id": session_id})
        )
        task.add_done_callback(lambda _t: queue.put_nowait(None))
        try:
            while (event := await queue.get()) is not None:
                extra = event.extra
                if (
                    event.event_type == "llm_stream_chunk"
                    and extra.get("channel") == "answer"
                ):
                    seq = str(extra.get("action_seq"))
                    yield sse("chunk", extra["text"], event_id=seq)
                elif event.event_type == "tool_call_start":
                    yield sse("status", f"calling {extra.get('tool_name')}")
            result = await task
            yield sse("final", outcome_to_json(result))  # finish or pause
        finally:
            if not task.done():
                task.cancel()  # client disconnected

    return StreamingResponse(gen(), media_type="text/event-stream")
```

Browser side:

```javascript
const es = new EventSource(`/chat?q=${encodeURIComponent(q)}&session_id=s1`);
let seq = null, text = "";
es.addEventListener("chunk", (e) => { if (e.lastEventId !== seq) { seq = e.lastEventId; text = ""; } text += e.data; render(text); });
es.addEventListener("status", (e) => showStatus(e.data));
es.addEventListener("final", (e) => { renderFinal(JSON.parse(e.data)); es.close(); });
```

The `id` carries `action_seq`, so the client resets the text when the planner restarts its answer. A `final` frame whose `event` is `"paused"` carries the `resume_token` for `planner.resume(...)`.

## FastAPI WebSocket recipe

```python
import asyncio

from fastapi import FastAPI, WebSocket

from penguiflow.planner import PlannerEvent

app = FastAPI()


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    request = await websocket.receive_json()
    queue: asyncio.Queue[PlannerEvent | None] = asyncio.Queue()
    planner = make_planner(event_callback=queue.put_nowait)
    task = asyncio.create_task(
        planner.run(
            request["q"], tool_context={"session_id": request["session_id"]}
        )
    )
    task.add_done_callback(lambda _t: queue.put_nowait(None))
    while (event := await queue.get()) is not None:
        await websocket.send_text(event_to_json(event))
    result = await task
    await websocket.send_text(outcome_to_json(result))
    await websocket.close()
```

## Sharing one planner instance

`event_callback` belongs to the planner instance. If you must share one instance across concurrent requests, route inside the callback with a `contextvars.ContextVar` set in the request's task before `planner.run()` (events are emitted from that task, so the variable is visible). Otherwise prefer a planner factory.

## Session bridge (`StreamingSession`)

Sessions remove the per-request plumbing: `PlannerTaskPipeline` builds a planner per task, installs its own callback, and projects events into `StateUpdate`s that clients subscribe to.

A planner pause makes the task `PAUSED`: the session publishes a `STATUS_CHANGE` with `status == "PAUSED"`, then a `CHECKPOINT` (`kind: "approval_required"`, `resume_token`), and the task waits for an `APPROVE` or `REJECT` steering event. The stream must stop there too (after forwarding the `CHECKPOINT`, which the client needs for the token), or it hangs.

```python
import asyncio
import json
import uuid

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from penguiflow import (
    PlannerTaskPipeline,
    SessionManager,
    SteeringEvent,
    SteeringEventType,
    TaskResult,
    UpdateType,
)

app = FastAPI()
manager = SessionManager()
TERMINAL = {"COMPLETE", "FAILED", "CANCELLED"}
runs: dict[str, asyncio.Task[TaskResult]] = {}  # tasks that may be paused


@app.get("/sessions/{session_id}/chat")
async def chat(session_id: str, q: str) -> StreamingResponse:
    session = await manager.get_or_create(session_id)
    pipeline = PlannerTaskPipeline(planner_factory=make_planner)
    task_id = uuid.uuid4().hex

    async def gen():
        updates = await session.subscribe(task_ids=[task_id])  # subscribe first
        run = asyncio.create_task(
            session.run_task(pipeline, query=q, task_id=task_id)
        )
        runs[task_id] = run
        run.add_done_callback(lambda _t: runs.pop(task_id, None))
        paused = False
        async for update in updates:
            data = json.dumps(update.content, default=str)
            yield sse(update.update_type.value, data, event_id=update.update_id)
            if update.update_type == UpdateType.CHECKPOINT and paused:
                return  # carries the resume_token; the task waits for approval
            if update.update_type != UpdateType.STATUS_CHANGE:
                continue
            status = update.content.get("status")
            paused = status == "PAUSED"
            if status in TERMINAL:
                break
        await asyncio.wait({run})  # FAILED/CANCELLED were already sent
        if not run.cancelled():
            run.exception()  # mark the error as retrieved

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/sessions/{session_id}/tasks/{task_id}/approve")
async def approve(session_id: str, task_id: str, resume_token: str) -> dict:
    session = await manager.get_or_create(session_id)
    accepted = await session.steer(
        SteeringEvent(
            session_id=session_id,
            task_id=task_id,
            event_type=SteeringEventType.APPROVE,
            payload={"resume_token": resume_token, "decision": "approve"},
        )
    )
    return {"accepted": accepted}
```

After approving, the client re-subscribes (`session.subscribe(task_ids=[task_id], since_id=<last update_id>)`) to follow the resumed run. `REJECT` with the same token ends the task as cancelled.

- Answer chunks (LLM `channel="answer"`, tool chunks with `meta={"channel": "answer"}`) arrive as `RESULT` updates, other channels as `THINKING`, tool calls as `TOOL_CALL`, step progress as `PROGRESS`.
- `subscribe(task_ids=[...], since_id=...)` replays persisted updates for reconnecting clients.
- For full duplex (updates out, steering in), implement the `Transport` protocol (`send(update)`, `receive() -> SteeringEvent | None`, `close()`) and call `await session.connect(transport)`.

## Choosing a bridge

| Need | Use |
|---|---|
| One request, one stream (chat endpoint) | per-request queue + planner factory |
| Reconnect / replay, background tasks, steering from the client | `StreamingSession` + `subscribe()` / `connect(transport)` |
| Another agent is the client | `A2AService` streaming — see [[penguiflow-a2a-integration]] |

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| SSE frames merge in the browser | missing blank line terminator | each frame must end with `\n\n` |
| `EventSource` reconnects forever | server closed without a terminal event | always send a `final` (or `error`) event before closing |
| Client receives another user's tokens | shared planner + single callback | planner per request, or contextvar routing |
| Handler hangs after the run ends | no sentinel enqueued | `task.add_done_callback(lambda _t: queue.put_nowait(None))` |
| Session stream hangs mid-run | task is `PAUSED` waiting for approval | stop after the `CHECKPOINT` that follows `PAUSED`; approve with its `resume_token` |
| Planner keeps running after disconnect | task not cancelled | cancel the run task in `finally` |
