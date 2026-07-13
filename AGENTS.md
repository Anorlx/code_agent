# Repository guidance

## Development

- Use Python 3.10 or newer. Install the project with `pip install -e .`.
- Runtime dependencies include LangGraph and `jsonschema`.
- Use strict test-driven development for behavior changes: add a failing test, confirm the expected failure, implement the smallest change, and rerun it.
- Preserve unrelated dirty worktree changes. Use `apply_patch` for hand-written edits.
- Keep `README.md` and `MAIN_README.md` consistent whenever user-facing behavior or documentation changes.

## Architecture

- `agent/hooks/` owns the independent hook types, validation, registration, and dispatch layer. It must not import the agent runtime.
- `agent/main_agent/` owns the interactive runtime, graph, query engine, sessions, checkpoints, and CLI integration.
- `agent/sub_agent/` owns focused helper agents and tool execution workflows; `agent/tools/` owns tool and MCP registries and implementations.
- Dependencies flow one way: runtime modules may import `agent.hooks`; `agent.hooks` must remain runtime-independent.
- Tests mirror behavior under `tests/`, with lifecycle coverage in `tests/hooks/`.

## Lifecycle hooks

The eight supported events are:

- `session.start`: observe startup or `MODIFY` the in-memory title, history, and recovered flag. `BLOCK` and `RETRY` never prevent startup.
- `session.end`: observe the termination reason, status, and message count. All actions are observation-only and never prevent shutdown.
- `prompt.before`: `CONTINUE`, `MODIFY` the validated prompt payload, or `BLOCK` the prompt.
- `tool.before`: runs after permission approval; `CONTINUE`, schema-valid same-tool argument `MODIFY`, or `BLOCK` execution.
- `tool.after`: observe or `MODIFY` the validated tool result payload.
- `tool.error`: observe or `MODIFY` a failure, or request the single bounded `RETRY` allowed by the runtime.
- `context.before_compact`: observe, `MODIFY` the validated message list, add protected context, or `BLOCK` automatic compaction.
- `agent.before_stop`: observe or `BLOCK` stopping; added context is protected. Payload modifications are not a general state mutation API.

`CONTINUE` is the default. `MODIFY` requires a dictionary payload, `BLOCK` requires a nonempty reason, and `RETRY` is valid only for `tool.error`; runtime integrations may impose stricter event-specific limits described above. Handler timeouts, exceptions, and invalid results are isolated and reported opaquely.

Permission review is authoritative and occurs before `tool.before`. Python hook handlers are trusted extensions, but payload schemas, immutable tool names, and retry limits still apply. External command hooks are out of scope. JSON Schema validation must not retrieve external references.

Public hook events and logs must never include prompts, tool arguments, tool results, block/retry reasons, credentials, secrets, or raw exception messages. Emit only safe structural fields such as event name, handler name, error type, status, and bounded counters.

## Verification

Run the complete suite and bytecode compilation before committing:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
.venv/bin/python -m compileall agent tests
```
