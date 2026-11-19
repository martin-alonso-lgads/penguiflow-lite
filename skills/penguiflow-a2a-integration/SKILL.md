---
name: penguiflow-a2a-integration
description: Expose a ReactPlanner agent as an A2A-spec service or call remote A2A agents from a planner — wrap the planner in `A2AService(planner, ...)`, mount it on FastAPI via `install_a2a_http`/`create_a2a_http_router`/`create_a2a_http_app`, serve the agent card at `/.well-known/agent-card.json`, handle JSON-RPC (`SendMessage`, ...) + REST + SSE + push notifications, optionally add the gRPC binding, call specialists with `A2AAgentToolset`/`A2AHttpTransport`, route across them with `AgentRegistry` + `A2ARouterToolset`, map planner pauses to `input-required` and resume them, keep conversation continuity with `RemoteBinding`s in a StateStore, and choose blocking/stream/task execution modes. Use when a user says "expose as A2A", "agent-to-agent", "call a remote agent", "agent card", "manager/specialist", "JSON-RPC", "SSE for agents", "push notifications for agents", or names `A2AService`, `RemoteBinding`, `A2AAgentToolset`, `A2ARouterToolset`.
---

# PenguiFlow A2A Integration

## When to use
- Exposing an existing `ReactPlanner` agent over the A2A protocol (HTTP+JSON, JSON-RPC, SSE, gRPC, push).
- Calling a remote A2A agent as a planner tool (specialist).
- Building a manager agent that routes to specialist A2A agents.
- Needing conversation continuity across calls to the same remote skill.

## When NOT to use
- Custom (non-A2A) remote transports → implement the `penguiflow.remote.RemoteTransport` protocol; see [[penguiflow-deployment]].
- Wiring MCP servers as planner tools → use [[penguiflow-mcp-integration]].
- Configuring the host planner itself → use [[penguiflow-reactplanner-config]].
- Persisting bindings in production → use [[penguiflow-statestore]] (this skill names the methods but the durable backend lives there).

## Hard boundaries
This skill is the A2A wire contract: agent cards, transports, conversation bindings, planner-side toolsets. Persistence lives in statestore. Generic remote execution lives in deployment. Planner config lives in reactplanner-config. Don't duplicate.

## Workflow

### 1) Decide your role: server, client, or both
- **Server (specialist)**: another agent will call you. Wrap your `ReactPlanner` in an `A2AService`, mount it on FastAPI (or gRPC), publish your agent card.
- **Client (manager)**: you will call other A2A agents from your planner. Use `A2AAgentToolset` to wrap remote skills as planner tools.
- **Hybrid**: both, common in router-and-specialist topologies.

See `references/server-binding.md` for the server path, `references/client-and-toolset.md` for the client path.

### 2) Server path — wrap the planner in an `A2AService`
```python
from penguiflow_a2a import A2AConfig, A2AService

service = A2AService(
    planner,                      # your ReactPlanner
    agent_card=agent_card,        # penguiflow_a2a.models.AgentCard
    config=A2AConfig(             # supported versions, tenant prefix, agent URL
        supported_versions=("0.3",),
        allow_v1_aliases=True,
        allow_tenant_prefix=True,
        default_tenant="default",
    ),
    tool_context={"db": db},       # optional static tool_context merged into every run
    tool_visibility=a2a_policy,    # optional ToolVisibilityPolicy (e.g. hide UI-only tools)
)
```
Each A2A task drives one planner run:
- The query is the joined text parts of the message (JSON of the data parts if there is no text).
- `tool_context` gets `tenant`, `session_id` (= A2A `contextId`), `a2a_task_id`, `a2a_context_id`, `a2a` (the request envelope) and `delivery_channel="a2a"`. Using `contextId` as `session_id` serializes runs within one conversation and runs different conversations in parallel.
- `PlannerFinish(answer_complete)` → `completed`, answer text as artifact `output-0` (full payload in `artifact.metadata["planner_payload"]`). `no_path` / `budget_exhausted` / exceptions → `failed`.
- `PlannerPause` → `input-required` with the pause `prompt` as the status message; the next `SendMessage` to that `taskId` resumes the planner with the message text as `user_input`.
- `CancelTask` cancels the run through its `SteeringInbox` → `cancelled`.
- `await service.stop()` cancels running tasks; they end `failed` ("Agent shutting down").

Full constructor, outcome mapping and agent-card schema in `references/server-binding.md`.

### 3) Mount it on FastAPI
Three options, ordered by integration depth:
- `install_a2a_http(app, service)`: the host owns the FastAPI app, A2A adds routes (recommended for existing apps).
- `create_a2a_http_router(service)`: the host composes with `app.include_router(...)`. Root routes only: no `/{tenant}` or `/v1` copies.
- `create_a2a_http_app(service)`: a standalone FastAPI app (single-purpose deployment).

All three serve `GET /.well-known/agent-card.json` (the router only with `include_agent_card=True`, the default), `GET /extendedAgentCard`, JSON-RPC at `POST /rpc`, and REST routes such as `POST /message:send`, `POST /message:stream` and `POST /tasks/{id}:cancel`. JSON-RPC methods are PascalCase: `SendMessage`, `SendStreamingMessage`, `GetTask`, `ListTasks`, `CancelTask`, `SubscribeToTask`, the push-config methods and `GetExtendedAgentCard`. `install_a2a_http` and `create_a2a_http_app` also mount `/{tenant}/...` and `/v1/...` copies when `A2AConfig` allows them. All three attach a lifespan that starts and stops the service. See `references/server-binding.md` for the full route map.

### 4) (Optional) Add SSE / push / gRPC
- **SSE**: built in for `SendStreamingMessage` / `POST /message:stream` (needs `capabilities.streaming=True`).
- **Push notifications**: `HttpPushNotificationSender(timeout_s=10.0, max_retries=2, backoff_s=0.5)` POSTs task updates to caller webhooks (needs `capabilities.push_notifications=True`). It rejects unsafe URLs (`is_safe_webhook_url`: loopback, RFC 1918, link-local, ...) at send time. Failed deliveries are logged as the `a2a_push_delivery_failed` warning.
- **gRPC**: `from penguiflow_a2a.bindings.grpc import add_a2a_grpc_service` adds the A2A protobuf service to a `grpc.aio.server`. Requires `penguiflow-lite[a2a-grpc]`.

Worked gRPC example: `examples/a2a_grpc_server/main.py`. Push/SSE deep dive: `references/transports-push-sse-grpc.md`.

### 5) Client path — wrap a remote agent as planner tools
```python
from penguiflow_a2a import A2AAgentToolset, A2AHttpTransport

toolset = A2AAgentToolset(
    agent_url="https://specialist.example.com",
    transport=A2AHttpTransport(timeout_s=30.0),
    default_timeout_s=30.0,
)
node_spec = toolset.tool(
    name="ask_specialist",
    skill="answer",
    args_model=AskArgs,
    out_model=AnswerOut,
    desc="Delegate to the specialist agent",
    streaming=False,
    execution_mode="auto",   # auto|blocking|stream|task
)
```
Add `node_spec` to your planner's tool catalog. A plain-text remote answer is wrapped into `out_model` when it has exactly one field. Builders and sinks receive the `ToolContext`; read request data from `ctx.tool_context`. Full pattern, `execution_mode` semantics, cancellation and chunk channels in `references/client-and-toolset.md`.

### 6) Conversation continuity (manager↔specialist)
Bindings are conversation-scoped and used only when `tool_context["session_id"]` is set. The toolset finds the store in `tool_context["state_store"]`, then `planner.state_store`, and calls its `find_binding(router_session_id=, agent_url=, remote_skill=)` (plus `tenant_id=`/`user_id=` when the method accepts them) and `save_remote_binding(binding)`. Every later call in the session reuses the remote `context_id`; the remote `task_id` is reused only while that task awaits input or auth (tracked in the binding metadata). The toolset never marks bindings terminal; call the store's `mark_binding_terminal(trace_id=, context_id=, task_id=)` to end a conversation. With no store or no session id, every call starts a fresh remote conversation. See [[penguiflow-statestore]] for durable backends.

### 7) Router topology — route across many specialists
`AgentRegistry` holds `RemoteAgentRecord`s (trust tier `low|standard|trusted|critical`, latency tier `interactive|standard|batch`). Fill it with `register(...)`, `register_card(...)`, `AgentRegistry.from_config(mapping)` or `load_agent_registry_config(path)`. `registry.route(AgentRouteRequest(...), policy=RouterPolicy(...))` returns scored candidates and `best(...)` the top one. `A2ARouterToolset(registry=, transport=, policy=).tool()` builds a single `delegate_to_agent` planner tool. See `references/router-and-registry.md`.

## Troubleshooting (fast checks)
- **`/.well-known/agent-card.json` 404**: you mounted `create_a2a_http_router(service, include_agent_card=False)`, or included the router under a prefix (the card is then at `<prefix>/.well-known/...`). Serve the card at the app root.
- **JSON-RPC `-32601 Method not found`**: you used slash-style names (`message/send`, `tasks/cancel`). The methods are PascalCase (`SendMessage`, `CancelTask`).
- **JSON-RPC `-32600 Request payload validation error`**: missing or null `id`, non-string `method`, or `jsonrpc != "2.0"`. The router enforces JSON-RPC 2.0 strictly.
- **Specialist loses context across calls**: no `tool_context["session_id"]`, or no store with `find_binding`/`save_remote_binding`. Put one in `tool_context["state_store"]` or on the planner (`planner.state_store`).
- **Webhook never called**: look for `a2a_push_delivery_failed` warnings on the `penguiflow_a2a` logger. `error_type=ValueError` means `is_safe_webhook_url` rejected the URL; use a public one.
- **Tenant prefix or `/v1` routes 404**: `A2AConfig.allow_tenant_prefix` / `allow_v1_aliases` is `False`, or you used `create_a2a_http_router`, which mounts root routes only. Use `install_a2a_http`.
- **Service start/stop**: the FastAPI lifespan of all three mounts starts and stops the service, and the service also starts on its first request. Without a FastAPI lifespan (gRPC only), call `await service.start()` and `await service.stop()` yourself.
- **gRPC import errors**: `pip install "penguiflow-lite[a2a-grpc]"`. `penguiflow_a2a/__init__.py` skips the gRPC exports when the extra is missing.
- **`SendStreamingMessage` sends no partial chunks**: the planner only streams its final answer with `stream_final_response=True`; otherwise the stream carries status updates and the final artifact only.
- **Task stuck in `input-required`**: the planner paused; send another `SendMessage` with the same `taskId` (and `contextId`) to resume it, or `CancelTask` it. Resume tokens live in the service's memory, so the resume must reach the same process.

## Worked examples
- `examples/a2a_grpc_server/main.py`: a `ReactPlanner` served over gRPC, plus a client call.

## References (load only as needed)
- `references/server-binding.md`: `A2AService`, `A2AConfig`, `AgentCard`, planner outcome mapping, REST and JSON-RPC route map, three binding modes.
- `references/client-and-toolset.md`: `A2AAgentToolset`, `A2AHttpTransport`, builders, execution modes, conversation bindings, remote pauses, event sinks.
- `references/transports-push-sse-grpc.md`: SSE encoding, `HttpPushNotificationSender` and SSRF guards, gRPC binding and RPCs.
- `references/router-and-registry.md`: `AgentRegistry`, `RouterPolicy`, `route()`/`best()` scoring, `A2ARouterToolset`.
