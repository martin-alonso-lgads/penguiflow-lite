# Client and `A2AAgentToolset`

`A2AAgentToolset` turns skills of one remote A2A agent into typed planner tools (`NodeSpec`s). The network calls go through a `penguiflow.remote.RemoteTransport`; `A2AHttpTransport` is the HTTP+JSON one.

## Building a transport and a toolset

```python
import os

from penguiflow_a2a import A2AAgentToolset, A2AHttpTransport

transport = A2AHttpTransport(
    version="0.3",                              # sent as the A2A-Version header
    headers={"X-Caller": "manager-prod"},       # sent to every agent
    agent_headers={                             # extra headers per agent_url
        "https://specialist.example.com": {
            "Authorization": f"Bearer {os.environ['SPECIALIST_TOKEN']}",
        },
    },
    timeout_s=30.0,                             # used when the call sets no timeout
    client=None,                                # optional shared httpx.AsyncClient
)

toolset = A2AAgentToolset(
    agent_url="https://specialist.example.com",
    transport=transport,
    agent_card=None,                            # optional mapping, passed through to the transport
    default_timeout_s=30.0,
    default_metadata={"caller": "manager-prod"},
    include_tool_context_keys=("tenant", "session_id", "task_id", "user_id"),  # the default
)
```

`A2AHttpTransport` needs `penguiflow-lite[a2a-client]` (httpx). It is agent-agnostic: every request carries its `agent_url`, and the transport calls the REST routes under it (`/message:send`, `/message:stream`, `/tasks/{id}`, `/tasks/{id}:subscribe`, `/tasks/{id}:cancel`). Header values are sent as given; nothing substitutes `${VAR}`. `agent_headers` keys match the request URL with or without a trailing slash. Without `client=`, each call opens and closes its own `httpx.AsyncClient`.

`agent_card` is only copied into each `RemoteCallRequest`; `A2AHttpTransport` ignores it.

## Declaring a tool

```python
from pydantic import BaseModel

from penguiflow_a2a import A2AAgentToolset, A2AHttpTransport


class AskArgs(BaseModel):
    question: str


class AnswerOut(BaseModel):
    answer: str


toolset = A2AAgentToolset(agent_url="https://specialist.example.com", transport=A2AHttpTransport())
node_spec = toolset.tool(
    name="ask_specialist",            # planner tool name
    skill="answer",                   # remote skill, sent in the message metadata
    args_model=AskArgs,
    out_model=AnswerOut,              # a text answer fills a single-field model
    desc="Delegate to the specialist agent",
    tags=("a2a", "remote"),
    auth_scopes=(),
    side_effects="external",
    streaming=False,
    timeout_s=None,                   # falls back to default_timeout_s, then the transport's
    metadata=None,                    # static per-tool metadata
    metadata_builder=None,            # (args, ctx) -> mapping, per call
    payload_builder=None,             # (args, ctx) -> payload, per call
    cancel_on_cancel=True,            # stream mode: cancel the remote task on local cancel
    chunk_channel="answer",           # meta["channel"] of the emitted chunks
    execution_mode="auto",            # auto | blocking | stream | task
    use_subscription=True,            # task mode: follow the task over SSE before polling
    poll_interval_s=0.25,
    max_poll_attempts=120,
    remote_event_sink=None,           # task mode: sink(event, ctx)
)
```

Add `node_spec` to the planner catalog (see [[penguiflow-reactplanner-config]]).

**What is sent.** The payload is `args.model_dump(mode="json")`, or what `payload_builder` returns. A string becomes a text part, bytes a base64 data part, anything else a data part. The message metadata merges, in order: `default_metadata`, the non-`None` values of `include_tool_context_keys`, `metadata`, then `metadata_builder(args, ctx)`, plus `skill`.

**What comes back.** A plain-text answer is wrapped into `out_model` when that model has exactly one field. Any other result (a data part's payload, or a list when there are several parts) is returned as is, and the planner validates it against `out_model`.

## Execution modes

| Mode | Behavior |
|---|---|
| `auto` | `stream` when `streaming=True`, else `blocking`. |
| `blocking` | `transport.send(...)` (`POST /message:send`, `blocking: true`); returns the final result. |
| `stream` | `transport.stream(...)` (`POST /message:stream`); each text event becomes a chunk; returns the final result. |
| `task` | Needs a transport with `send_task` and `get_task` (`A2AHttpTransport` has both), else runs as `blocking`. Submits a non-blocking task, then follows `subscribe_task` (when `use_subscription=True` and supported; it falls back to polling on `RuntimeError`), or polls `get_task` every `poll_interval_s`, at most `max_poll_attempts` times. Text events become chunks. A `failed`, `cancelled` or `rejected` task raises `RuntimeError`. |

Chunks go out through `ctx.emit_chunk(..., meta={"channel": chunk_channel, "remote_agent_url": ..., "remote_task_id": ..., "remote_context_id": ..., "remote_skill": ...})`. In sessions, a tool chunk whose `meta["channel"]` is `"answer"` (the default) or `"revision"` projects as a RESULT update; any other channel projects as THINKING. See [[penguiflow-streaming]].

## Payload and metadata builders

Builders receive the validated args and the `ToolContext`. `ToolContext` has no `get()` or `[...]`; read request data from `ctx.tool_context`.

```python
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from penguiflow.planner import ToolContext


def payload_builder(args: BaseModel, ctx: ToolContext) -> object:
    return {"q": args.model_dump()["question"], "user": ctx.tool_context.get("user_id")}


def metadata_builder(args: BaseModel, ctx: ToolContext) -> Mapping[str, Any] | None:
    return {"trace_origin": "manager", "project": ctx.tool_context.get("project_id")}
```

Pass them as `toolset.tool(..., payload_builder=payload_builder, metadata_builder=metadata_builder)`. They run on every call; use them when the remote skill expects another schema than `args_model`, or to derive or redact metadata.

## Conversation continuity (`RemoteBinding`)

Bindings are conversation-scoped: one per local session, agent and skill. They are used only when `tool_context["session_id"]` is set. The toolset looks for the store in `tool_context["state_store"]`, then `planner.state_store`, and uses its `find_binding` and `save_remote_binding` methods (`SupportsConversationBindings`, see [[penguiflow-statestore]]). Without a store, or without a session id, every call is a fresh remote conversation.

On every call the toolset:
1. Calls `find_binding(router_session_id=<session_id>, agent_url=..., remote_skill=<skill>)`, adding `tenant_id=` (from `tool_context["tenant_id"]`, else `["tenant"]`) and `user_id=` when the store's method accepts them. A binding with `is_terminal=True` is ignored.
2. Reuses the binding's `context_id`, so the remote agent sees one conversation. It reuses the remote `task_id` only while the binding's metadata says the task awaits input or auth (`awaiting_remote_input` / `awaiting_remote_auth`).
3. After the call, saves a `RemoteBinding` with the new `task_id`/`context_id`, `router_session_id`, `remote_skill`, `tenant_id`, `user_id`, `last_remote_task_id`, `is_terminal=False`, and metadata `{"source": "a2a_agent_toolset", ...}`. Task mode also records `remote_task_state` and `remote_task_terminal`.

The toolset never marks a binding terminal; task state lives in the binding metadata. To end a conversation, so the next call starts a new remote context, call the store's `mark_binding_terminal(trace_id=..., context_id=..., task_id=...)` yourself.

## Remote pauses

When the remote task is `input-required` or `auth-required`, the tool saves the binding with the matching `awaiting_remote_*` flag and pauses the planner:
- `input-required` → `ctx.pause("await_input", payload)`;
- `auth-required` → `ctx.pause("approval_required", payload)`;
- payload keys: `message`, `remote_status`, `remote_task_id`, `remote_context_id`, `remote_agent_url`, `remote_skill`.

In `blocking` and `stream` modes the signal is the transport raising `RemoteTaskInputRequired` / `RemoteTaskAuthRequired` (`A2AHttpTransport` does); in `task` mode it is the task snapshot's state. After `planner.resume(...)` the run continues. With bindings in use (a session id and a store), the next call of that tool in the session sends its payload to the same remote task, which answers the remote agent's question. Pause and resume mechanics: [[penguiflow-hitl-pause-resume]].

## Remote task events

`RemoteEventSink` is `Callable[[RemoteTaskEvent, ToolContext], Any]`: it is called as `sink(event, ctx)`, may be sync or async, and its exceptions are swallowed. Sinks get events in `task` mode only: the submitted task, then each subscription event or changed poll snapshot. A sink can also come per run, from `tool_context["a2a_remote_event_sink"]` or `tool_context["remote_task_event_sink"]`.

```python
from penguiflow.planner import ToolContext
from penguiflow.remote import RemoteTaskEvent


class LogSink:
    def __init__(self) -> None:
        self.seen: list[tuple[str, str | None, bool]] = []

    async def __call__(self, event: RemoteTaskEvent, ctx: ToolContext) -> None:
        state = event.status.state.value if event.status is not None else None
        self.seen.append((event.kind, state, event.done))
```

`RemoteTaskEvent` fields: `kind` (`"task"`, `"status"` or `"artifact"`), `task`, `status`, `text`, `result`, `artifact`, `done`, `context_id`, `task_id`, `agent_url`, `meta`, `raw`.

## Transports

`RemoteTransport` (in `penguiflow.remote`) needs `send(request)`, `stream(request)` and `cancel(*, agent_url, task_id)`. The optional `SupportsRemoteTasks` protocol adds `send_task`, `get_task`, `list_tasks`, `subscribe_task` and the four push-config methods. `A2AHttpTransport` implements both. For a custom transport (gRPC, in-process), implement `RemoteTransport`, plus `send_task` and `get_task` (and optionally `subscribe_task`) if you need `task` mode.

## Cancellation propagation

In `stream` mode with `cancel_on_cancel=True` (the default), cancelling the tool call (a steering `CANCEL`, or the run being cancelled) once the stream has reported a task id calls `transport.cancel(agent_url=..., task_id=...)` (`POST /tasks/{id}:cancel`). This is best-effort, and errors are swallowed. `blocking` and `task` modes do not cancel the remote task.

## Operational defaults
- Set `default_timeout_s` on the toolset.
- Keep `execution_mode="auto"` unless you need `task` mode for long-running or HITL-prone work.
- Set `tool_context["session_id"]` and a store with conversation bindings when follow-up turns must reach the same remote conversation.
- Narrow `include_tool_context_keys`, or redact in `metadata_builder`, before sending user data to another agent.

## Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| Calls always start fresh | No `tool_context["session_id"]`, or no store with `find_binding`/`save_remote_binding` | Set both: `tool_context["state_store"]` or `ReactPlanner(state_store=...)` |
| Follow-up starts a new remote task although the remote asked a question | Binding metadata lacks `awaiting_remote_*` (custom store dropped `metadata`) | Persist `RemoteBinding.metadata` as is |
| Remote returns `input-required` and nothing resumes | The planner pause isn't resumed | Resume it — [[penguiflow-hitl-pause-resume]] |
| `task` mode behaves like `blocking` | Transport lacks `send_task`/`get_task` | Implement them, or use `blocking` |
| Sink never called | Sinks only run in `task` mode, as `sink(event, ctx)` | Use `execution_mode="task"`; accept two arguments |
| Tool output fails validation | Text answer and an `out_model` with several fields | Use a single-field `out_model`, or have the remote return data |
| Cancel doesn't reach the remote | Not `stream` mode, `cancel_on_cancel=False`, or the remote ignores cancels | Use `stream` mode, or handle cancellation locally |
