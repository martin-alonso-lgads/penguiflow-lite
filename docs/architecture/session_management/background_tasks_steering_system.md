# Background Tasks and Steering System Architecture

## Overview

The Penguiflow ReactPlanner incorporates a sophisticated background tasks and steering system that enables asynchronous task execution, real-time control, and dynamic workflow management. This system allows the planner to spawn background tasks (subagents or tool jobs) while maintaining the ability to steer, pause, resume, and cancel ongoing operations.

## System Components

### 1. ReactPlanner Core

The `ReactPlanner` serves as the central orchestrator for both foreground and background operations:

- **Primary Role**: Executes the ReAct (Reasoning + Acting) loop for foreground tasks
- **Background Task Orchestration**: Spawns and manages background tasks via the `fork()` method
- **State Management**: Maintains trajectory, constraints, and memory across both foreground and background tasks
- **Event Handling**: Processes planner events and coordinates with the steering system

### 2. Background Tasks Configuration

The `BackgroundTasksConfig` model holds the planner's background options and the defaults for new task groups. It rejects unknown fields. Limits and proactive reporting are configured elsewhere:

#### Core Enablement
- `enabled`: Adds background task guidance to the system prompt (it does not turn the `tasks.*` tools on or off)
- `include_prompt_guidance`: Whether to inject background task guidance into system prompts
- `allow_tool_background`: Allows tools with `extra["background"]["enabled"]` to spawn async tasks

#### Execution Modes (per task)
- Mode (`subagent` or `job`), merge strategy (`human_gated`, `append`, `replace`), context depth (`full`, `summary`, `none`) and cancel propagation are `tasks.spawn` arguments (`mode`, `merge_strategy`, `context_depth`, `propagate_on_cancel`), or a tool's `extra["background"]` (`mode`, `default_merge_strategy`)

#### Resource Limits (`SessionLimits`, on `SessionManager(limits=...)`)
- `max_tasks_per_session`: Maximum non-terminal tasks per session (default: 8)
- `max_background_tasks`: Maximum non-terminal background tasks per session (default: 4)
- `max_concurrent_tasks`: Maximum tasks running at once (default: 3)
- `max_task_runtime_s`: Task timeout in seconds (default: 900)
- `steering_queue_size`: Maximum queued steering events per task (default: 200)

#### Task Groups (RFC_TASK_GROUPS)
- `default_group_merge_strategy`: Merge strategy of new task groups (default: APPEND)
- `default_group_report`: Report strategy of new task groups (default: all)
- `auto_seal_groups_on_foreground_yield`, `retain_turn_timeout_s`, `background_continuation_max_hops`, `background_continuation_cooldown_s`: Sealing and retained-turn waits

#### Proactive Reporting
- Configured with `setup_proactive_reporting()` or `StreamingSession.configure_proactive_reporting()` (`enabled`, `strategies`, `max_queued`, `timeout_s`, `max_hops`, `fallback_notification`)

### 3. Steering System

The `SteeringInbox` provides bidirectional control between the planner and external systems:

#### Event Types
- `INJECT_CONTEXT`: Injects additional context into the planning process
- `REDIRECT`: Redirects the planner's current goal or instruction
- `CANCEL`: Terminates the current planning session
- `PAUSE`: Pauses the planning process
- `RESUME`: Resumes a paused planning session
- `APPROVE`/`REJECT`: Approves or rejects pending actions
- `USER_MESSAGE`: Delivers user messages to the planner
- `PRIORITIZE`: Adjusts task priority

#### Inbox Operations
- `push(event)`: Queues a steering event
- `drain()`: Retrieves all queued events without blocking
- `has_event()`: Checks for queued events without consuming them
- `next()`: Waits for the next event (blocking)

#### Cancellation State
- `cancelled`: Property indicating if a cancel event has been received
- `cancel_reason`: Reason for cancellation
- `cancel_event`: asyncio.Event for cancellation coordination

### 4. Task Service Architecture

The `TaskService` protocol defines the interface for managing background tasks:

#### Core Interface
- `spawn()`: Creates a new background subagent task
- `spawn_tool_job()`: Creates a new background tool job
- `list()`: Lists tasks in a session
- `get()`: Gets details for a specific task
- `cancel()`: Cancels a running task
- `prioritize()`: Adjusts task priority

#### Task Group Operations
- `seal_group()`: Seals a group to prevent new tasks from joining
- `cancel_group()`: Cancels an entire task group
- `apply_group()`: Applies or rejects group results
- `list_groups()`: Lists all task groups in a session
- `get_group()`: Gets details for a specific task group

### 5. Session Management

The `StreamingSession` manages the lifecycle of tasks and provides the execution environment:

#### Task Execution
- `spawn_task()`: Creates and starts a new task
- `_execute_task()`: Runs a task with proper error handling and resource management
- `run_task()`: Synchronously executes a task and returns results

#### Context Management
- `update_context()`: Updates the session's context
- `get_context()`: Retrieves the current session context
- `apply_context_patch()`: Applies changes to the session context with various merge strategies

#### Group Management
- `resolve_or_create_group()`: Finds or creates a task group
- `add_task_to_group()`: Adds a task to a group
- `seal_group()`: Seals a group to prevent new tasks
- `wait_for_group_completion()`: Waits for a group to complete

### 6. Background Task Execution

#### Task Spawning Process
1. **Action Recognition**: When the planner encounters a `task.subagent` or `task.tool` action
2. **Validation**: Checks the spawn guard and the session's `SessionLimits`
3. **Context Snapshot**: Creates a snapshot of the current session context
4. **Planner Forking**: Creates a new `ReactPlanner` instance using the `fork()` method
5. **Task Creation**: Registers the task with the session manager
6. **Asynchronous Execution**: Runs the background task concurrently with the main planner

#### Task Lifecycle
- **PENDING**: Task created but not yet started
- **QUEUED**: Task waiting for available resources
- **RUNNING**: Task is actively executing
- **PAUSED**: Task is temporarily suspended
- **COMPLETE**: Task finished successfully
- **FAILED**: Task encountered an error
- **CANCELLED**: Task was cancelled

### 7. Task Groups and Coordination

#### Group Management
- **Group Creation**: Tasks can be assigned to named groups for coordinated execution
- **Sealing Mechanism**: Groups can be sealed to indicate no more tasks will be added
- **Completion Strategies**: 
  - `all`: All tasks in group must complete
  - `any`: Any task completion triggers group completion
  - `none`: Group completion is manual

#### Merge Strategies
- **HUMAN_GATED**: Results require human approval before merging
- **APPEND**: Results are merged as soon as the task completes (latest result per task)
- **REPLACE**: Currently the same as APPEND

Merged results are shown to the foreground LLM as a read-only `<read_only_background_results>` system message until acknowledged.

## Data Flow and Interactions

### 1. Background Task Initiation Flow
```
[ReactPlanner] 
    ↓ (detects task.subagent/task.tool action)
[Action Processing] 
    ↓ (checks the spawn guard and SessionLimits)
[Context Snapshot] 
    ↓ (captures current session state)
[Fork Planner] 
    ↓ (creates new ReactPlanner instance)
[Background Task Execution] 
    ↓ (runs concurrently with main planner)
[Result Merging] ← (based on merge strategy)
```

### 2. Steering Control Flow
```
[External System/UI] 
    ↓ (steering event)
[SteeringInbox] 
    ↓ (queues event)
[ReactPlanner] 
    ↓ (polls for events during execution)
[Event Handler] 
    ↓ (processes event type)
[Appropriate Action] (cancel, pause, redirect, etc.)
```

### 3. Task Coordination Flow
```
[Main ReactPlanner] 
    ↓ (spawns background tasks)
[Background Task 1] ←→ [Background Task 2] (optional coordination)
    ↓ (completes)
[Result Aggregation] 
    ↓ (applies merge strategy)
[Main ReactPlanner Continues] (with merged results)
```

### 4. Task Group Flow
```
[ReactPlanner] 
    ↓ (spawns tasks with group parameters)
[Session Manager] 
    ↓ (creates/resolves task group)
[Task 1] ←→ [Task 2] ←→ [Task 3] (group members)
    ↓ (all complete)
[Group Completion] 
    ↓ (triggers based on strategy)
[Result Synthesis] ← (aggregated results)
```

## Key Design Patterns

### 1. Fork-and-Forget Pattern
- Background tasks are executed using forked planner instances
- Each task gets its own isolated execution context
- Results are merged back to the main planner upon completion

### 2. Event-Driven Architecture
- Steering events are processed asynchronously
- Planner periodically checks for new events
- Events can modify execution flow without blocking

### 3. Resource Management
- Hard limits on concurrent tasks prevent resource exhaustion
- Timeout mechanisms ensure tasks don't run indefinitely
- Queue limits prevent memory bloat from pending events

### 4. Context Isolation
- Background tasks operate with limited context to prevent interference
- Memory keys ensure proper isolation between tasks
- Tool policies can restrict available tools for background execution

### 5. Proactive Reporting
- Completed background tasks can generate proactive reports
- Configurable strategies for auto-merging results
- Notification systems for completed tasks

## Integration Points

### 1. With ReactPlanner
- Background tasks are initiated through special action types (`task.subagent`, `task.tool`)
- Steering events are passed through the `steering` parameter
- Results are merged back into the main planner's trajectory

### 2. With Memory System
- Background tasks can inherit memory context from the parent planner
- Memory keys ensure proper isolation between concurrent tasks
- Short-term memory can be shared or isolated based on configuration

### 3. With Tool System
- Tools can be marked as background-capable
- Tool policies control which tools can be used in background tasks
- Resource tracking continues across background task boundaries

## Error Handling and Resilience

### 1. Task Failure Recovery
- Failed background tasks don't terminate the main planner
- Configurable retry mechanisms for failed tasks
- Graceful degradation when background tasks fail

### 2. Cancellation Propagation
- Cancellation signals propagate from parent to child tasks
- Configurable propagation modes (cascade vs isolate)
- Cleanup routines ensure resources are released

### 3. Timeout Management
- Individual task timeouts prevent indefinite execution
- Overall session timeouts ensure bounded execution
- Configurable timeout values based on task type

## Security and Access Control

### 1. Tool Policy Enforcement
- Background tasks respect the same tool policies as foreground tasks
- Whitelist/blacklist mechanisms control available tools
- Tag-based access control for fine-grained permissions

### 2. Context Injection Protection
- Steering events are validated against expected schemas
- Payload sanitization prevents injection attacks
- Depth and size limits prevent resource exhaustion

### 3. Spawn Guards
- Custom validation logic before task creation
- Cost estimation and approval requirements
- Rate limiting and quota enforcement

## Performance Considerations

### 1. Concurrency Limits
- Configurable limits on concurrent background tasks
- Semaphore-based resource allocation
- Backpressure mechanisms prevent overload

### 2. Memory Management
- Efficient context copying for background tasks
- Automatic cleanup of completed task contexts
- Memory limits prevent unbounded growth

### 3. Event Processing
- Non-blocking event polling
- Batch processing of multiple events
- Efficient queue management for steering events

## Diagrams

### System Architecture
```
┌─────────────────────────────────────────────────────────────────┐
│                    External Systems                           │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐           │
│  │   UI/Client │  │ Monitoring  │  │   Control   │           │
│  │             │  │   System    │  │   Panel     │           │
│  └─────────────┘  └─────────────┘  └─────────────┘           │
└─────────────────────────┬─────────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                   Steering System                             │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │              SteeringInbox                            │   │
│  │  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐    │   │
│  │  │   CANCEL    │ │   PAUSE     │ │  REDIRECT   │    │   │
│  │  │   EVENT     │ │   EVENT     │ │   EVENT     │    │   │
│  │  └─────────────┘ └─────────────┘ └─────────────┘    │   │
│  │  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐    │   │
│  │  │ INJECT_CTX  │ │ USER_MSG    │ │ APPROVE     │    │   │
│  │  │   EVENT     │ │   EVENT     │ │   EVENT     │    │   │
│  │  └─────────────┘ └─────────────┘ └─────────────┘    │   │
│  └─────────────────────────────────────────────────────────┘   │
└─────────────────────────┬─────────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                    ReactPlanner                               │
│  ┌─────────────────┐  ┌─────────────────────────────────────┐ │
│  │  Foreground     │  │     Background Task Orchestrator    │ │
│  │  Main Loop      │  │                                     │ │
│  │                 │  │  ┌─────────────┐ ┌─────────────┐   │ │
│  │  ┌───────────┐  │  │  │   Task 1    │ │   Task 2    │   │ │
│  │  │ Planning  │  │  │  │             │ │             │   │ │
│  │  │  Loop     │  │  │  │ ReactPlanner│ │ ReactPlanner│   │ │
│  │  └───────────┘  │  │  │   (fork)    │ │   (fork)    │   │ │
│  │                 │  │  └─────────────┘ └─────────────┘   │ │
│  │  ┌───────────┐  │  │                                     │ │
│  │  │  Memory   │  │  │  ┌─────────────────────────────────┐ │ │
│  │  │  Manager  │  │  │  │       Task Coordination         │ │ │
│  │  └───────────┘  │  │  │                                 │ │ │
│  │                 │  │  │  Merge Strategy Management      │ │ │
│  │  ┌───────────┐  │  │  │  Result Aggregation            │ │ │
│  │  │  Event    │  │  │  │  Resource Management           │ │ │
│  │  │ Callback  │  │  │  └─────────────────────────────────┘ │ │
│  │  └───────────┘  │  └─────────────────────────────────────┘ │
│  └─────────────────┘                                          │
└─────────────────────────────────────────────────────────────────┘
```

### Task Execution Flow
```
┌─────────────────┐
│   Main Planner  │
└─────────┬───────┘
          │
    ┌─────▼─────┐
    │ Detect BG │
    │ Task Req  │
    └─────┬─────┘
          │
    ┌─────▼─────┐
    │ Validate  │
    │  Config   │
    └─────┬─────┘
          │
    ┌─────▼─────┐
    │ Context   │
    │ Snapshot  │
    └─────┬─────┘
          │
    ┌─────▼─────┐
    │ Fork New  │
    │ Planner   │
    └─────┬─────┘
          │
    ┌─────▼─────┐
    │ Execute   │
    │ Background│
    │ Task      │
    └─────┬─────┘
          │
    ┌─────▼─────┐
    │ Complete  │
    │ & Merge   │
    └─────┬─────┘
          │
    ┌─────▼─────┐
    │ Return to │
    │ Main Loop │
    └───────────┘
```

### Task Group Flow
```
┌─────────────────┐
│   Main Planner  │
└─────────┬───────┘
          │
    ┌─────▼─────┐
    │ Spawn     │
    │ Grouped   │
    │ Tasks     │
    └─────┬─────┘
          │
    ┌─────▼─────┐
    │ Group     │
    │ Manager   │
    └─────┬─────┘
          │
    ┌─────▼─────┐
    │ Task 1    │
    │ Running   │
    └─────┬─────┘
          │
    ┌─────▼─────┐
    │ Task 2    │
    │ Running   │
    └─────┬─────┘
          │
    ┌─────▼─────┐
    │ Task 3    │
    │ Running   │
    └─────┬─────┘
          │
    ┌─────▼─────┐
    │ All       │
    │ Complete  │
    └─────┬─────┘
          │
    ┌─────▼─────┐
    │ Aggregate │
    │ Results   │
    └─────┬─────┘
          │
    ┌─────▼─────┐
    │ Apply     │
    │ Strategy  │
    └───────────┘
```

### Steering Event Flow
```
┌─────────────────┐
│ External Input  │
│ (UI, API, etc.) │
└─────────┬───────┘
          │
    ┌─────▼─────┐
    │ Sanitize  │
    │ & Validate│
    └─────┬─────┘
          │
    ┌─────▼─────┐
    │ Queue in  │
    │ Steering  │
    │ Inbox     │
    └─────┬─────┘
          │
    ┌─────▼─────┐
    │ Planner   │
    │ Polls for │
    │ Events    │
    └─────┬─────┘
          │
    ┌─────▼─────┐
    │ Process   │
    │ Event &   │
    │ Modify    │
    │ Behavior  │
    └───────────┘
```

## Configuration Examples

### Basic Background Task Setup
```python
from penguiflow.planner import ReactPlanner, BackgroundTasksConfig
from penguiflow.sessions import SessionLimits, SessionManager

config = BackgroundTasksConfig(
    enabled=True,
    default_group_merge_strategy="HUMAN_GATED",
)
sessions = SessionManager(
    limits=SessionLimits(max_concurrent_tasks=3, max_task_runtime_s=1800),
)

planner = ReactPlanner(
    llm="gpt-4",
    background_tasks=config,
    # ... other config
)
```

### Advanced Steering Configuration
```python
from penguiflow.steering import SteeringInbox

# Create steering inbox
steering_inbox = SteeringInbox(maxsize=50)

# Pass to planner
result = await planner.run(
    query="Analyze data and generate report",
    steering=steering_inbox
)

# From another thread/process, send steering events
await steering_inbox.push(SteeringEvent(
    event_type=SteeringEventType.REDIRECT,
    payload={"instruction": "Focus on financial aspects only"}
))
```

### Task Group Configuration
```python
# Spawn tasks in a group
await task_service.spawn(
    session_id="session-123",
    query="Research market trends",
    group="research-group",
    group_sealed=True,  # Seal group immediately
    group_merge_strategy=MergeStrategy.APPEND,
    group_report="all"  # Report when all tasks complete
)
```

## Best Practices

### 1. Resource Management
- Set appropriate limits based on available system resources
- Monitor concurrent task counts to prevent overload
- Use timeouts to prevent runaway tasks

### 2. Security
- Carefully configure tool policies for background tasks
- Validate all steering event payloads
- Implement proper authentication for steering interfaces

### 3. Error Handling
- Implement retry logic for critical background tasks
- Provide fallback mechanisms when background tasks fail
- Log all steering events for audit purposes

### 4. Performance
- Use efficient merge strategies to minimize context overhead
- Limit the complexity of background tasks
- Monitor memory usage during long-running sessions

### 5. Task Group Management
- Use groups to coordinate related tasks
- Seal groups appropriately to control execution flow
- Choose merge strategies based on use case requirements

This architecture provides a robust foundation for building intelligent, responsive AI agents that can handle complex workflows with real-time control and background processing capabilities.