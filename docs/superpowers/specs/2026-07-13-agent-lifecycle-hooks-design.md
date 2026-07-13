# Agent Lifecycle Hooks Design

## Purpose

Add a small, typed lifecycle Hook system to `real_agent` so extensions can observe, modify, or stop selected runtime operations without placing extension-specific logic inside the LangGraph nodes, tool runner, context manager, or CLI loop.

The first release is Python-registration only. It will not load or execute external shell commands from configuration files. This keeps Hook execution inside the existing Python trust boundary and avoids introducing command-injection, environment, and subprocess permission semantics before the core protocol is stable.

## Existing System

The repository already has several isolated callback mechanisms:

- `stop_hook` can prevent Agent completion;
- `permission_reviewer` and `permission_prompter` intercept tool execution;
- `MemoryObserver` reacts after completed turns;
- Skill definitions expose `pre_hooks` and `post_hooks`, although no bundled or file-loaded Skill currently registers concrete handlers.

These mechanisms prove that lifecycle extension points are useful, but they do not share a common event type, result protocol, ordering rule, error policy, or audit event.

The repository does not currently contain `AGENTS.md`, `Agent.md`, or another case variant. The implementation will add a root-level `AGENTS.md` describing repository conventions and Hook extension rules.

## Selected Lifecycle Events

The first release will support eight events that match stable boundaries in the current runtime:

| Event | Trigger | Supported decisions |
| --- | --- | --- |
| `session.start` | After a CLI session is selected or created | continue, modify |
| `session.end` | Before CLI background tasks are drained and the session exits | continue |
| `prompt.before` | Before a submitted user prompt enters the Agent graph | continue, modify, block |
| `tool.before` | After permission approval and before the concrete tool runner | continue, modify, block |
| `tool.after` | After a successful tool result | continue, modify |
| `tool.error` | After a failed tool result | continue, modify, retry |
| `context.before_compact` | Immediately before automatic context compaction | continue, modify, block |
| `agent.before_stop` | Before a normal Agent stop is committed | continue, block |

`model.before` and `model.after` are deliberately deferred. The current model path streams partial events and retries transient failures. Adding these events now would leave ambiguous whether handlers run per attempt or per logical model request, and whether partial output qualifies as an `after` event. They can be added later without changing the base protocol.

## Package Structure

Create a focused `agent/hooks/` package:

- `types.py` defines event names, actions, `HookEvent`, `HookResult`, and handler types.
- `manager.py` owns registration, priority ordering, execution, payload propagation, context aggregation, decision validation, and error isolation.
- `errors.py` defines protocol and execution errors.
- `builtin.py` exposes construction of the default manager. The default manager has no behavior-changing handlers.
- `__init__.py` exports the public API.

The Hook package must not import the graph, CLI, tool runner, or context manager. Runtime components depend on the Hook package, keeping dependency direction one-way.

## Public Protocol

`HookEvent` is immutable at the object level and contains:

- `name`: one of the supported event names;
- `session_id`: current session identifier, or an empty string when unavailable;
- `payload`: event-specific dictionary;
- `metadata`: request/run identifiers and non-operational annotations.

Handlers have the asynchronous shape:

```python
HookHandler = Callable[[HookEvent], Awaitable[HookResult]]
```

`HookResult` contains:

- `action`: `continue`, `modify`, `block`, or `retry`;
- `reason`: required for `block`, optional otherwise;
- `updated_payload`: required for `modify` and `retry`;
- `additional_context`: strings accumulated in handler order.

`HookManager.register(event_name, handler, priority=100, name=None)` registers a handler. Lower numeric priority runs first; equal-priority handlers preserve registration order. It returns an unregister callback so tests and embedding applications can remove handlers without mutating internal collections.

`HookManager.emit(event)` runs a snapshot of matching handlers. Payload changes are passed to later handlers. A `block` result stops the chain immediately. The returned result contains the final payload and all context accumulated before the stop.

## Decision and Failure Semantics

- `continue` preserves the current payload.
- `modify` replaces the complete payload for subsequent handlers and the caller.
- `block` stops remaining handlers and returns the supplied reason.
- `retry` is valid only for `tool.error`; elsewhere it is a protocol error handled by the manager's error policy.
- A handler timeout or exception is recorded as a `HookFailure` and does not stop the runtime by default.
- Protocol violations such as `modify` without `updated_payload` are recorded the same way.
- The manager returns failures with the aggregate result so runtime code can emit observable `hook_error` events.
- Explicit `block` is the only default mechanism that interrupts an Agent operation.

Handlers receive a shallow copy of event payload and metadata. Returned payloads are copied before propagation. This prevents accidental top-level mutation from bypassing the structured result protocol.

## Runtime Integration

### Prompt and session

`QueryEngine` accepts a `HookManager`. Before calling `run_agent`, it emits `prompt.before`. A modified payload may replace `user_input` and `memory_context`; additional Hook context is appended to memory context. A block yields a terminal event without invoking the model.

The CLI creates one default manager for the process. It emits `session.start` after session recovery/selection and `session.end` during orderly shutdown. Session events are observable and cannot block shutdown.

### Tools

`StreamingToolExecutor` and `run_tool_subagent` receive the manager through runtime context. Existing permission review remains authoritative and runs first. For approved calls:

1. Emit `tool.before` with the tool name and parsed arguments.
2. On `modify`, rebuild the tool call with the returned arguments.
3. On `block`, synthesize a blocked tool result without executing the tool.
4. Execute the tool normally.
5. Emit `tool.after` when the raw result is successful.
6. Emit `tool.error` when the raw result is unsuccessful or execution raises.

`tool.after` may replace the raw result. `tool.error` may replace the error result or request one retry with updated arguments. A per-call metadata flag prevents an unlimited retry loop: Hook-requested retry is allowed at most once.

### Context compaction

`manage_context` accepts an optional manager and session/run identifiers. It emits `context.before_compact` only when automatic compaction would actually run. A modified payload may replace messages used for compaction or add protected context. A block skips that compaction attempt and records the decision in the context report.

### Agent stop compatibility

Normal completion emits `agent.before_stop`. A block keeps the graph running, subject to the existing maximum-turn and blocking-token guards.

The existing `stop_hook` parameter remains supported. It is evaluated as a compatibility adapter after registered `agent.before_stop` handlers. Existing callers therefore retain behavior while new code uses the structured manager.

## Observability

Runtime integration emits compact events for:

- Hook start/finish decisions where useful;
- explicit Hook blocks;
- isolated Hook failures;
- Hook-requested tool retry.

The CLI renders these using its existing event-line mechanism. Payload contents are not logged wholesale because tool arguments and prompts may contain secrets.

## Testing Strategy

Tests use the standard-library `unittest` runner so the project gains no test dependency. Async behavior uses `unittest.IsolatedAsyncioTestCase`.

Unit tests cover:

- priority and stable registration order;
- unregister behavior;
- sequential payload modification;
- additional-context aggregation;
- block short-circuiting;
- invalid action/payload combinations;
- handler exception and timeout isolation;
- retry restriction to `tool.error`;
- payload copy behavior.

Integration tests cover:

- prompt modification and blocking without a model call;
- tool argument modification, blocking, success transformation, error handling, and one retry maximum;
- compaction modification and blocking;
- structured before-stop behavior and legacy `stop_hook` compatibility;
- session lifecycle emission at the smallest independently testable CLI helper boundary.

The verification gate is:

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
python -m compileall -q agent tests
```

## Documentation and Repository Delivery

- Add root `AGENTS.md` with architecture boundaries, test commands, Hook registration rules, and safety requirements.
- Update both `README.md` and `MAIN_README.md` because they currently mirror the main project documentation.
- Document supported events, decision behavior, a Python registration example, deferred model events, and how to run tests.
- Preserve unrelated existing worktree changes.
- Commit only the files required for this feature unless the user explicitly authorizes including unrelated deletions and documentation edits.
- Push the resulting commit(s) to `origin/main`, whose configured GitHub repository is `Anorlx/code_agent`.

## Acceptance Criteria

1. `agent/hooks/` exists and exposes the typed Hook protocol and manager.
2. All eight selected lifecycle events are connected to real runtime boundaries.
3. Existing permission and legacy stop behavior remain compatible.
4. Hook failures are observable and do not crash the Agent by default.
5. Hook-requested tool retry cannot loop indefinitely.
6. Automated unit and integration tests pass using the documented commands.
7. Root `AGENTS.md`, `README.md`, and `MAIN_README.md` describe the Hook system accurately.
8. Feature files are committed and pushed to `origin/main` without absorbing unrelated worktree changes.
