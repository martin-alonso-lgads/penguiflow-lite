# Transports: SSE, Push Notifications, gRPC

## SSE (`SendStreamingMessage`, `POST /message:stream`)

The streaming counterpart of `SendMessage`. It needs `capabilities.streaming=True` on the agent card. The router returns a FastAPI `StreamingResponse` (`text/event-stream`) that yields A2A `StreamResponse` events. On `/rpc`, each event is wrapped in a JSON-RPC result envelope.

Encoding: `penguiflow_a2a.sse.encode_stream_response(event)` produces the SSE bytes (`data: <json>\n\n`), and the router applies it per event.

What the server emits during a stream:
- The task snapshot (state, ids) first.
- Progressive answer chunks (`artifactUpdate` on `output-0`), mapped from the planner's answer-channel `llm_stream_chunk` events. These come only when the planner has `stream_final_response=True`.
- The final answer artifact, which replaces the streamed chunks (`append=false`, `lastChunk=true`).
- A final `statusUpdate` (`final: true`) with the terminal state, or `input-required` when the planner paused. The stream ends there.

`SubscribeToTask` (`GET|POST /tasks/{task_id}:subscribe`) streams a running task's later events the same way; subscribing to a terminal task is an error.

Client side: any SSE-aware HTTP client works. `A2AHttpTransport.stream(request)` returns an async iterator of `RemoteStreamEvent`s; an `append=false` artifact update replaces the text streamed so far.

### When to use SSE vs task mode
- **SSE**: the caller stays connected and consumes chunks immediately. Best for chat-style UX.
- **Task mode**: submit work, then poll or subscribe later. Best for long-running, resumable, HITL-prone work.

On the client, `A2AAgentToolset`'s `execution_mode` picks between them (see `client-and-toolset.md`).

## Push Notifications

For long-running tasks where the caller doesn't stay connected, the agent POSTs each task update to a webhook the caller registered. Push needs `capabilities.push_notifications=True` on the agent card; otherwise registering a config fails with `PushNotificationNotSupportedError`. A caller registers a webhook in `configuration.pushNotificationConfig` of its `SendMessage`, or later with `SetTaskPushNotificationConfig` (`POST /tasks/{id}/pushNotificationConfigs?configId=...`).

### `HttpPushNotificationSender`

The default `push_sender` of `A2AService`. Pass your own instance to change its settings, or any object implementing `penguiflow_a2a.push.PushNotificationSender` (`async send(config, event)`).

```python
from penguiflow_a2a.push import HttpPushNotificationSender

sender = HttpPushNotificationSender(
    timeout_s=10.0,   # per HTTP attempt
    max_retries=2,    # retries after the first attempt
    backoff_s=0.5,    # waits 0.5 s, then 1 s, ... between attempts
)
```

Each delivery is a JSON POST of the `StreamResponse` event (`Content-Type: application/a2a+json`). Headers come from the push config:
- `token` → `X-A2A-Notification-Token: <token>`;
- `authentication` → `Authorization: <schemes[0]> <credentials>` (`Bearer` and `Basic` are normalised; other schemes are sent as given).

Any failure, including a non-2xx answer, is retried with exponential backoff, up to `max_retries` times.

### SSRF defense: `is_safe_webhook_url`

Before sending, the sender checks `penguiflow_a2a.push.is_safe_webhook_url(config.url)` and raises `ValueError` without retrying when the URL is unsafe. A URL is unsafe when:
- its scheme is not `http` or `https`, or it has no host;
- the host is `localhost` or ends in `.localhost`;
- the host doesn't resolve (DNS failure);
- any resolved address is private (RFC 1918, unique-local IPv6), loopback, link-local, multicast, reserved or unspecified.

URLs are checked at send time, not when the config is registered.

### Delivery failures are logged

`A2AService` delivers each event to every config of the task concurrently. A delivery that still fails (an unsafe URL, or the last retry) is logged as the `a2a_push_delivery_failed` warning on the `penguiflow_a2a` logger, with `task_id`, `config_id`, `error_type` and the exception. The webhook URL and token are never logged. The task itself continues.

### Common push pitfalls
- **Webhook never arrives**: look for `a2a_push_delivery_failed` warnings. `error_type=ValueError` usually means `is_safe_webhook_url` rejected a private or loopback URL; use a public one. The `penguiflow_a2a` logger is separate from the `penguiflow` one, so make sure your logging config keeps its warnings.
- **Auth header missing**: the config needs `authentication` with both `schemes` and `credentials`.
- **Duplicate notifications**: the sender retries, so receivers must be idempotent (dedupe on task id plus event).
- **Tasks progress slowly**: every status and artifact update waits for its deliveries, retries included, before the run moves on. Slow webhooks slow the task, so keep receivers fast and `timeout_s` low.

## gRPC binding

Optional. Install with:

```bash
pip install "penguiflow-lite[a2a-grpc]"
```

### Wiring

```python
import grpc

from penguiflow_a2a import A2AService
from penguiflow_a2a.bindings.grpc import add_a2a_grpc_service

server = grpc.aio.server()
port = server.add_insecure_port("127.0.0.1:50051")
service = A2AService(planner, agent_card=card, config=config)
add_a2a_grpc_service(server, service)

await service.start()
await server.start()
```

On shutdown, `await server.stop(None)`, then `await service.stop()`.

### Protobuf surface

The source proto is `docs/spec/a2a.proto`. The generated modules are `penguiflow_a2a/grpc/a2a_pb2.py` and `a2a_pb2_grpc.py` (typed by their `.pyi` stubs). Service `A2AService`:

| RPC | Request | Response |
|---|---|---|
| `SendMessage` | `SendMessageRequest` | `SendMessageResponse` |
| `SendStreamingMessage` | `SendMessageRequest` | stream of `StreamResponse` |
| `GetTask` | `GetTaskRequest` | `Task` |
| `ListTasks` | `ListTasksRequest` | `ListTasksResponse` |
| `CancelTask` | `CancelTaskRequest` (`name="tasks/{task_id}"`) | `Task` |
| `SubscribeToTask` | `SubscribeToTaskRequest` | stream of `StreamResponse` |
| `SetTaskPushNotificationConfig`, `GetTaskPushNotificationConfig`, `ListTaskPushNotificationConfig`, `DeleteTaskPushNotificationConfig` | the matching `...Request` | `TaskPushNotificationConfig`, its list response, or `Empty` |
| `GetExtendedAgentCard` | `GetExtendedAgentCardRequest` | `AgentCard` |

Message primitives: `Message`, `Part`, `Role` (`ROLE_USER`, `ROLE_AGENT`), `Task`, `TaskState`.

Client side:

```python
import grpc

from penguiflow_a2a.grpc import a2a_pb2, a2a_pb2_grpc

channel = grpc.aio.insecure_channel("127.0.0.1:50051")
stub = a2a_pb2_grpc.A2AServiceStub(channel)
response = await stub.SendMessage(
    a2a_pb2.SendMessageRequest(
        message=a2a_pb2.Message(
            message_id="msg-1",
            role=a2a_pb2.Role.ROLE_USER,
            parts=[a2a_pb2.Part(text="hello grpc")],
        ),
        configuration=a2a_pb2.SendMessageConfiguration(blocking=True),
    ),
    metadata=(("a2a-version", "0.3"),),
)
print(a2a_pb2.TaskState.Name(response.task.status.state))
```

The `a2a-version` metadata picks the protocol version; it must be a supported one, and without it the service's `default_version` is used. `a2a-extensions` carries requested extension URIs.

### Runnable example

`examples/a2a_grpc_server/main.py` builds a `ReactPlanner` (offline scripted LLM and one tool), serves it over gRPC, sends a `SendMessage` request, and prints the task state and answer. Run it with `uv run python examples/a2a_grpc_server/main.py`.

### Optional import guard

`penguiflow_a2a/__init__.py` imports the gRPC binding under `contextlib.suppress(RuntimeError)`. Without the extra, the rest of the package still works, and `A2AGrpcServicer` / `add_a2a_grpc_service` are simply not exported. Importing `penguiflow_a2a.bindings.grpc` directly raises the `RuntimeError` with the install hint.

## Choosing a transport

| Need | Pick |
|---|---|
| Browser/UI client, real time | HTTP + SSE (`POST /message:stream`) |
| Server to server, simple | HTTP + JSON-RPC (`/rpc`) or REST (`POST /message:send`) |
| Long-running, caller disconnects | HTTP + push notifications + task mode |
| High throughput, low latency, polyglot | gRPC |
| Mixed deployment | Several bindings on the same `A2AService` |

One `A2AService` can be mounted on FastAPI **and** gRPC at once; both share its task store and lifecycle.
