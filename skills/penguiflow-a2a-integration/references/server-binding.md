# Server Binding: `A2AService` and FastAPI

## Building an `A2AService`

The constructor takes the planner positionally and **all other arguments keyword-only**:

```python
from penguiflow_a2a import A2AConfig, A2AService

service = A2AService(
    planner,                               # ReactPlanner (positional)
    agent_card=card,                       # penguiflow_a2a.models.AgentCard
    config=A2AConfig(
        supported_versions=("0.3",),
        default_version=None,              # defaults to the first supported version
        allow_v1_aliases=True,
        allow_tenant_prefix=True,
        default_tenant="default",
        agent_url=None,                    # canonical URL; enables RemoteBinding persistence
    ),
    # Optional:
    # store=InMemoryTaskStore(),            # TaskStore for task records (in-memory by default)
    # state_store=...,                      # where RemoteBindings go (default: planner.state_store)
    # tool_context={...},                   # static tool_context merged into every run
    # tool_visibility=ToolVisibilityPolicy(...),  # applied to every A2A run
    # push_sender=HttpPushNotificationSender(),   # any PushNotificationSender
    # extended_agent_card=fuller_card,
    # extended_agent_card_auth=check_headers,     # Callable[[Mapping[str, str]], bool]
)
```

The service:
- drives one `planner.run()` (or `planner.resume()`) per A2A task;
- owns a `TaskStore` (in-memory by default) for task records and their subscribers;
- starts on its first request, or when you call `await service.start()`. Starting bridges the planner's `event_callback` (chaining any callback you set) so answer chunks can be streamed;
- `await service.stop()` cancels running tasks, which end `failed` with the message "Agent shutting down", and restores the callback.

The constructor raises `ValueError` when `card.protocol_versions` doesn't include every `A2AConfig.supported_versions` entry.

`A2AConfig` knobs:
- `supported_versions`: protocol versions this service speaks. Must be non-empty. A request's `A2A-Version` header (or gRPC `a2a-version` metadata) must name one; without the header, `default_version` is used.
- `default_version`: falls back to `supported_versions[0]` when unset.
- `allow_v1_aliases`: also serve the routes under `/v1/...`.
- `allow_tenant_prefix`: also serve the routes under `/{tenant}/...` (and `/{tenant}/v1/...` with v1 aliases). Only `install_a2a_http` and `create_a2a_http_app` mount the prefixed routes.
- `default_tenant`: used when no tenant prefix is present.
- `agent_url`: canonical URL of this agent. When set and a state store is available, each task saves a `RemoteBinding(trace_id=task_id, context_id, task_id, agent_url)`. This is best-effort: failures are logged (`a2a_remote_binding_save_failed`) and the task continues.

## How a task maps to the planner

**Input.** The planner `query` is the message's text parts joined with newlines; with no text, it is the JSON of the data parts (a message with neither is rejected). Messages must have `role: "user"`. The per-run `tool_context` is:

| Key | Value |
|---|---|
| `delivery_channel` | `"a2a"` (the static `tool_context` may override it) |
| `tenant` | tenant prefix from the URL, else `A2AConfig.default_tenant` |
| `session_id` | the A2A `contextId` |
| `a2a_task_id`, `a2a_context_id`, `a2a_message_id`, `a2a_role` | identifiers of the request |
| `a2a` | `{"message", "metadata", "configuration"}` envelope of the request |
| `a2a_requested_extensions` | extension URIs the caller requested |
| `a2a_accepted_output_modes` | when the caller sent them |

Static `tool_context` entries are merged in first, so request-derived keys always win. Because `session_id` is the `contextId`, the planner serializes runs within one conversation (and keeps its short-term memory), while different conversations run in parallel. The service sets `tenant`, not `tenant_id`; add `tenant_id` yourself (for example from your auth layer) where memory or artifact isolation depends on it.

**Outcomes.**

| Planner result | Task state | Output |
|---|---|---|
| `PlannerFinish(reason="answer_complete")` | `completed` | artifact `output-0`: a text part with the answer (`raw_answer`/`answer`/...), the full payload under `artifact.metadata["planner_payload"]`; payloads without answer text become a data part |
| `PlannerFinish(reason="no_path" \| "budget_exhausted")` | `failed` | status message from `metadata["error"]`; the payload (if any) is still stored as `output-0` |
| `PlannerPause` | `input-required` | status message from the pause payload's `prompt`/`message`/`question`; metadata `pause_reason`, `pause_payload` |
| `SteeringCancelled` / cancelled run | `cancelled` (`failed` while the service is stopping) | — |
| any other exception | `failed` | status message with the error text |

**Resume.** A `SendMessage` or `SendStreamingMessage` (REST: `POST /message:send`, `POST /message:stream`) whose message carries the `taskId` of an `input-required` task resumes the planner with `planner.resume(token, user_input=<message text>)`. A message to a task that is still running, or to a terminal task, is rejected. The resume tokens are kept in the service's memory, so the resume must reach the same process.

**Cancel.** `CancelTask` (REST: `POST /tasks/{task_id}:cancel`) pushes a `SteeringEventType.CANCEL` into the run's `SteeringInbox` and cancels the run; the task ends `cancelled`. A paused (`input-required`) task can also be cancelled. Cancelling a terminal task is an error.

**Streaming.** Needs `capabilities.streaming=True` on the card. On `SendStreamingMessage`, final-answer chunks (`llm_stream_chunk` planner events with `extra["channel"] == "answer"`) are forwarded as `output-0` artifact updates: `append=True`, or `append=False` when the planner restarts its answer (new `action_seq`). When the run finishes, the final artifact replaces the streamed one (`append=False`, `last_chunk=True`). The planner only emits these chunks with `stream_final_response=True`. `SendMessage` never forwards chunks.

## Agent card schema

`A2AService` requires the full A2A `AgentCard` from `penguiflow_a2a.models` (alongside `AgentCapabilities`, `AgentInterface`, `AgentSkill`):

```python
from penguiflow_a2a.models import (
    AgentCapabilities, AgentCard, AgentInterface, AgentSkill,
)

card = AgentCard(
    protocol_versions=["0.3"],
    name="My Agent",
    description="What I do",
    version="1.0.0",
    supported_interfaces=[
        AgentInterface(url="https://agent.example.com", protocol_binding="HTTP+JSON"),
    ],
    capabilities=AgentCapabilities(
        streaming=True,
        push_notifications=True,
        extended_agent_card=False,
        state_transition_history=False,
    ),
    default_input_modes=["application/json"],
    default_output_modes=["application/json"],
    skills=[
        AgentSkill(
            id="answer",
            name="Answer",
            description="Answer a question",
            tags=["qa"],
        ),
    ],
)
```

Required non-empty lists (`Field(min_length=1)`): `protocol_versions`, `supported_interfaces`, `default_input_modes`, `default_output_modes`, `skills`, and `AgentSkill.tags`. Fields serialize as camelCase (`protocolVersions`, `defaultInputModes`, ...).

The card is published at `GET /.well-known/agent-card.json` with media type `application/a2a+json`.

**Extended card.** `GET /extendedAgentCard` (JSON-RPC `GetExtendedAgentCard`) returns `extended_agent_card=` when the public card sets `capabilities.extended_agent_card=True`; otherwise it returns an A2A error. Gate it with `extended_agent_card_auth=`, a `Callable[[Mapping[str, str]], bool]` that receives the request headers; a `False` answer gives `401`. Without it, every caller gets the extended card.

## Three binding modes

### Mode A: `install_a2a_http(app, service)`

Use when the host owns the FastAPI app.

```python
from fastapi import FastAPI

from penguiflow_a2a import install_a2a_http

app = FastAPI()
install_a2a_http(app, service, include_jsonrpc=True, include_agent_card=True)
```

It adds the agent card route to the **app**, then includes an internal router with all operation routes and a lifespan that calls `service.start()` / `service.stop()`. It also mounts copies of the operation routes under `/{tenant}`, `/v1` and `/{tenant}/v1`, as `A2AConfig` allows.

Best when:
- You already have a FastAPI app with other routes.
- You want A2A routes alongside health, metrics and business endpoints.

### Mode B: `create_a2a_http_router(service)`

Returns an `APIRouter` you mount with `app.include_router(router)`.

```python
from fastapi import FastAPI

from penguiflow_a2a import create_a2a_http_router

app = FastAPI()
router = create_a2a_http_router(service, include_agent_card=True, include_jsonrpc=True)
app.include_router(router)
```

Caveats:
- The router serves the routes at its own root only: **no** `/{tenant}` or `/v1` routes, whatever `A2AConfig` says. Use Mode A or C for those.
- The router carries a lifespan that starts and stops the service; FastAPI runs it when you include the router in an app.
- `include_agent_card=True` (the default) puts the card in the router. Under a prefix it would be served at `<prefix>/.well-known/agent-card.json`, so for prefixed mounts pass `include_agent_card=False` and serve the card at the app root.

Best when:
- You compose with `include_router(prefix=..., ...)`.
- You need to drop JSON-RPC or the agent card for a deployment.

### Mode C: `create_a2a_http_app(service)`

Use when A2A is the only thing this process serves.

```python
from penguiflow_a2a import create_a2a_http_app

app = create_a2a_http_app(service, include_docs=True)
```

Creates a `FastAPI(title=card.name, description=card.description, version=card.version)` app (with `/docs` and `/openapi.json` when `include_docs=True`) and calls `install_a2a_http` on it.

## Route map (HTTP + JSON-RPC)

With Mode A or C, `allow_tenant_prefix=True` and `allow_v1_aliases=True`, every route except the agent card is mounted at:
- `/<route>`
- `/v1/<route>`
- `/{tenant}/<route>`
- `/{tenant}/v1/<route>`

| Method | Path | Purpose |
|---|---|---|
| GET | `/.well-known/agent-card.json` | Public agent card (app root only) |
| GET | `/extendedAgentCard` | Extended card, behind `extended_agent_card_auth` |
| POST | `/rpc` | JSON-RPC 2.0 endpoint (all operations) |
| POST | `/message:send` | Send a message (`configuration.blocking` waits for the end) |
| POST | `/message:stream` | Send a message and stream the task over SSE |
| GET / POST | `/tasks/{task_id}:subscribe` | Stream a running task's events |
| POST | `/tasks/{task_id}:cancel` | Cancel a task |
| GET | `/tasks/{task_id}` | Get a task snapshot (`historyLength` query param) |
| GET | `/tasks` | List tasks (paginated) |
| POST | `/tasks/{task_id}/pushNotificationConfigs?configId=...` | Set a push config |
| GET | `/tasks/{task_id}/pushNotificationConfigs` | List push configs |
| GET / DELETE | `/tasks/{task_id}/pushNotificationConfigs/{config_id}` | Get / delete a push config |

JSON-RPC methods on `/rpc` are PascalCase: `SendMessage`, `SendStreamingMessage`, `GetTask`, `ListTasks`, `CancelTask`, `SubscribeToTask`, `SetTaskPushNotificationConfig`, `GetTaskPushNotificationConfig`, `ListTaskPushNotificationConfig`, `DeleteTaskPushNotificationConfig`, `GetExtendedAgentCard`. Slash-style names such as `message/send` get `-32601 Method not found`.

The JSON-RPC endpoint also enforces:
- a JSON body (else `-32700`);
- `jsonrpc == "2.0"`, a non-null `id` and a string `method` (else `-32600`);
- object `params` (else `-32602`).

POST bodies must be `application/json` or `application/a2a+json`. Errors come back as A2A problem-detail JSON on the REST routes and as JSON-RPC error objects on `/rpc`.

## Request and response shapes

Requests and responses use the A2A models in `penguiflow_a2a.models` (`SendMessageRequest`, `Message`, `Part`, `Task`, `TaskStatus`, `Artifact`, `StreamResponse`), serialized with camelCase aliases (`messageId`, `contextId`, `taskId`, ...).

## Error handling

The binding converts errors itself, inside each route:
- `A2AError` → A2A problem-detail JSON (REST) or a JSON-RPC error.
- Pydantic `ValidationError` → an A2A validation problem.

App-level exception handlers therefore don't see A2A errors; don't add any just for these routes.

## Lifespan and start/stop

- Mode A (`install_a2a_http`) and Mode C (`create_a2a_http_app`): the included router's lifespan starts and stops the service.
- Mode B (`create_a2a_http_router`): the router's lifespan does the same once the router is included in an app.
- Without a FastAPI lifespan (for example gRPC only), `await service.start()` before serving and `await service.stop()` on shutdown. The service also starts itself on its first request, but only `stop()` cancels running tasks and restores the planner's callback.

## Hard rules
- Serve the agent card at the app root, not inside a prefixed router.
- Treat the agent card as public; gate `/extendedAgentCard` with `extended_agent_card_auth=`.
- The binding sets `application/a2a+json` on card responses.
- Resolve tenants from the URL prefix or your own auth; `default_tenant` is a fallback, not authz.
- Hide UI-only tools from A2A callers with `tool_visibility=`; downstream agents can't see rendered components.
