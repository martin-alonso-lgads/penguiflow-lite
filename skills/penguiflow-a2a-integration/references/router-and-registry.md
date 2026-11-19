# Router Topology: `AgentRegistry`, `RouterPolicy`, `A2ARouterToolset`

When a manager agent has to choose among several specialist A2A agents, keep them in an `AgentRegistry` and give the planner one `A2ARouterToolset` tool. The tool picks the best agent and skill for each call, then delegates through an `A2AAgentToolset` (see `client-and-toolset.md`).

## `AgentRegistry`

An in-memory registry of `RemoteAgentRecord`s, keyed by `agent_url` with trailing slashes stripped. Registering the same URL again replaces the record.

```python
from penguiflow_a2a import AgentRegistry, RemoteAgentRecord, RemoteSkillRecord

registry = AgentRegistry()
registry.register(
    RemoteAgentRecord(
        agent_url="https://finance.example.com",
        name="Finance",
        description="Answers finance questions",
        version="1.0.0",
        skills=(
            RemoteSkillRecord(
                agent_url="https://finance.example.com",
                skill_id="answer",
                name="answer",
                description="Answer a finance question",
                tags=("finance", "revenue"),
                input_modes=("text/plain",),
                output_modes=("text/plain",),
            ),
        ),
        streaming=True,
        tenant_id="acme",
        trust_tier="trusted",        # low | standard | trusted | critical
        latency_tier="interactive",  # interactive | standard | batch
        auth_schemes=("bearer",),
    )
)
```

Or register from an agent card (a dict or a `penguiflow_a2a.models.AgentCard`). `register_card` builds one `RemoteSkillRecord` per card skill (id, name, description, tags, input/output modes, security), reads `streaming` and `pushNotifications` from the card's capabilities, and returns the record. A card without skills gets a single `default` skill.

```python
from penguiflow_a2a import AgentRegistry, fetch_agent_card


async def register_specialist(registry: AgentRegistry) -> None:
    card = await fetch_agent_card("https://legal.example.com", timeout_s=5.0)
    registry.register_card(
        agent_url="https://legal.example.com",
        card=card,
        tenant_id="acme",
        trust_tier="standard",
        latency_tier="batch",
        auth_schemes=("bearer",),
        metadata={"region": "eu-west-1"},
    )
```

`fetch_agent_card(agent_url, headers=None, timeout_s=None)` GETs `<agent_url>/.well-known/agent-card.json` and needs `penguiflow-lite[a2a-client]` (httpx).

### Declarative loading

```python
from penguiflow_a2a import AgentRegistry, load_agent_registry_config

registry = load_agent_registry_config("/etc/manager/agents.yaml")  # .json, .yaml/.yml or .toml

registry = AgentRegistry.from_config(
    {
        "agents": [
            {
                "agent_url": "https://finance.example.com",
                "card": {"name": "Finance", "skills": [{"id": "answer", "tags": ["finance"]}]},
                "tenant_id": "acme",
                "trust_tier": "trusted",
                "latency_tier": "interactive",
                "auth_schemes": ["bearer"],
            },
        ],
    }
)
```

Each entry needs a `card` object and an `agent_url` (or `agentUrl`, or the card's `url`). `tenant_id`, `trust_tier`, `latency_tier`, `auth_schemes` and `metadata` are optional, and the camelCase spellings are accepted too. A malformed entry or an unknown tier raises `RegistryConfigError`. YAML needs `pyyaml`.

## `RouterPolicy`

Production guardrails applied before scoring. All fields have defaults.

```python
from penguiflow_a2a import RouterPolicy

policy = RouterPolicy(
    allowed_agents=("https://finance.example.com", "https://legal.example.com"),
    denied_agents=(),
    max_candidates=5,
    require_same_tenant=True,
    min_trust_tier="standard",
    required_execution_mode=None,
    required_auth_schemes=("bearer",),
    fallback_agent_url="https://finance.example.com",
    fallback_skill="answer",
    timeout_s=30.0,
    poll_interval_s=0.25,
    max_poll_attempts=120,
)
```

| Field | Effect |
|---|---|
| `allowed_agents` | When non-empty, only these URLs are candidates. |
| `denied_agents` | These URLs are never candidates. |
| `require_same_tenant` | With a request tenant, agents must have exactly that `tenant_id` (agents without one are excluded). Agents whose `tenant_id` differs from the request's are excluded even without a policy. |
| `min_trust_tier` | Used when the request sets none. Excludes agents below the tier. |
| `required_execution_mode` | Used when the request sets none. Only `"stream"` filters: it excludes agents without `streaming=True`. |
| `required_auth_schemes` | Used when the request sets none. The agent must share at least one scheme; agents with no schemes are excluded. |
| `max_candidates` | Default `limit` of `route()`. |
| `fallback_agent_url`, `fallback_skill` | When nothing matches, route to this agent (and skill, else its first skill). The fallback must still pass the filters above. It scores `0.0` with reasons `("policy_fallback",)`. |
| `timeout_s`, `poll_interval_s`, `max_poll_attempts` | Used by `A2ARouterToolset` for the delegated call. |

## Routing: `route()` and `best()`

```python
from penguiflow_a2a import AgentRegistry, AgentRouteRequest, RouterPolicy


def explain_route(registry: AgentRegistry, policy: RouterPolicy) -> None:
    request = AgentRouteRequest(
        query="What was Q3 revenue?",
        skill="answer",
        input_mode="text/plain",
        output_mode="text/plain",
        tenant_id="acme",
        required_execution_mode="stream",
        auth_schemes=("bearer",),
        min_trust_tier="standard",
    )
    for candidate in registry.route(request, policy=policy):  # best first
        print(candidate.agent.agent_url, candidate.skill.skill_id, candidate.score, candidate.reasons)
    best = registry.best(request, policy=policy)  # AgentRouteCandidate | None
    print(best)
```

`route(request, *, limit=None, policy=None)` applies the filters, scores every skill of every remaining agent, and returns `AgentRouteCandidate(agent, skill, score, reasons)` objects sorted by score. It returns at most `limit`, else `policy.max_candidates`, else 5. `best(request, *, policy=None)` returns the top candidate or `None`.

Scoring, per skill:

| Signal | Score | Reason |
|---|---|---|
| `request.skill` words overlap the skill's id, name, description words and tags | +40, +10 per shared word; **no overlap skips the skill** | `skill_match:<words>` |
| Query words overlap the skill's and agent's words | +5 per word, max +30 | `query_overlap:<words>` |
| `input_mode` / `output_mode` listed by the skill | +5 each; a skill that lists modes but not this one is **skipped**; a skill that lists none is not penalised | `input_mode:<mode>`, `output_mode:<mode>` |
| `required_execution_mode="stream"` and the agent streams | +8 | `streaming` |
| `required_execution_mode="task"` | +3 | `task_capable` |
| Trust tier (`low` 0 … `critical` 3) | ×4 | `trust:<tier>` |
| Latency tier (`batch` 0, `standard` 1, `interactive` 2) | ×2 | `latency:<tier>` |

Words are lowercased alphanumeric tokens. Tags are compared as given, so keep them lowercase.

## `A2ARouterToolset`

Wraps the registry as a single planner tool. Every delegation shares one `transport`.

```python
from penguiflow_a2a import A2AHttpTransport, A2ARouterToolset, AgentRegistry, RouterPolicy

registry = AgentRegistry()
router_toolset = A2ARouterToolset(
    registry=registry,
    transport=A2AHttpTransport(timeout_s=30.0),
    default_execution_mode="auto",  # auto | blocking | stream | task
    default_timeout_s=30.0,
    policy=RouterPolicy(require_same_tenant=True),
)
node_spec = router_toolset.tool(
    name="delegate_to_agent",  # the default
    desc="Delegate the request to the best matching specialist agent.",
)
```

Add `node_spec` to the planner catalog. On each call the tool:

1. Reads the tenant from `tool_context["tenant_id"]`, else `tool_context["tenant"]`.
2. Picks the execution mode: `args.execution_mode`, else `policy.required_execution_mode`, else `default_execution_mode`.
3. Builds an `AgentRouteRequest` from the args, that tenant and mode, and the policy's auth schemes and minimum trust tier, then calls `registry.route(request, policy=policy)`. With no candidate it raises `RuntimeError("No compatible A2A specialist found")`.
4. Delegates to the top candidate through an `A2AAgentToolset` built for that agent (its card, and `policy.timeout_s` or `default_timeout_s`). The payload sent is `args.payload`, or `{"query": args.query}` when that is empty. Conversation bindings, remote pauses and stream chunks work as for `A2AAgentToolset`.

Inputs (`RouterDelegationArgs`): `query: str`, `skill: str | None`, `payload: dict[str, Any]`, `execution_mode: ExecutionMode | None`, `input_mode: str | None`, `output_mode: str | None`.

Output (`RouterDelegationResult`): `agent_url`, `skill`, `score`, `reasons: list[str]`, `result` (the remote answer as returned: text or data), and `route_metadata` with `candidate_count`, `execution_mode`, `agent_name`, `trust_tier`, `latency_tier` and `tenant_id`.

## Patterns

### Strict policy with a fallback

```python
from penguiflow_a2a import RouterPolicy

policy = RouterPolicy(
    require_same_tenant=True,
    min_trust_tier="trusted",
    fallback_agent_url="https://always-on.example.com",
    fallback_skill="answer",
)
```

If no agent passes the filters and scoring, the fallback agent handles the request, provided it passes the same filters.

### Explain before delegating

Call `registry.route(request, policy=policy)` from your own tool or UI, show the `reasons`, and delegate only after the user confirms (through a dedicated `A2AAgentToolset` tool for the chosen agent).

### Multi-skill specialists

One agent can carry several `RemoteSkillRecord`s. Each skill is scored on its own, so set `skill` in the request (or the tool args) to target one.

## Operational rules
- `agent_url` is the routing key. Use the same URL form everywhere; only trailing slashes are normalised.
- `tenant_id` is the multi-tenant boundary. Keep `require_same_tenant=True` (the default) and set `tool_context["tenant_id"]` on every run.
- Treat `RouterPolicy` as authorization input, not the only check. Don't let callers override `denied_agents`.
- Log `reasons` and `route_metadata` for every delegation; they are the audit trail.
- The registry is mutable and in-memory. Re-register agents when their cards change.
