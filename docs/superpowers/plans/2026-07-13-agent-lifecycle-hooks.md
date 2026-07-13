# Agent Lifecycle Hooks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a typed, observable Python lifecycle Hook system to `real_agent`, connect eight stable runtime events, test the behavior, document the extension API, and publish it to the configured GitHub repository.

**Architecture:** A dependency-light `agent.hooks` package owns event/result types and ordered dispatch. Existing runtime layers receive a `HookManager` by dependency injection and translate structured decisions into their native state, tool result, context report, or terminal events. Existing permission review and synchronous `stop_hook` remain compatible.

**Tech Stack:** Python 3.10+, `asyncio`, dataclasses, LangGraph integration, standard-library `unittest`

---

## File Map

- Create `agent/hooks/types.py`: public event/action/result/failure types.
- Create `agent/hooks/errors.py`: Hook protocol validation exception.
- Create `agent/hooks/manager.py`: registration, ordering, timeout, dispatch, aggregation.
- Create `agent/hooks/builtin.py`: construct the safe empty default manager.
- Create `agent/hooks/__init__.py`: stable public exports.
- Create `tests/hooks/test_manager.py`: core protocol tests.
- Modify `agent/main_agent/query_engine.py`: `prompt.before` integration and manager injection.
- Modify `agent/main_agent/graph.py`: manager state, context integration, `agent.before_stop`, event propagation.
- Modify `agent/main_agent/context_manager.py`: `context.before_compact` integration.
- Modify `agent/main_agent/tool_executor.py`: forward Hook manager/session/run context.
- Modify `agent/sub_agent/tool_runner.py`: `tool.before`, `tool.after`, `tool.error`, bounded retry.
- Create `tests/hooks/test_prompt_hooks.py`: prompt modification/block integration.
- Create `tests/hooks/test_tool_hooks.py`: complete tool Hook pipeline tests.
- Create `tests/hooks/test_context_and_stop_hooks.py`: compact and stop tests.
- Modify `agent/main_agent/cli.py`: session lifecycle helpers and CLI rendering.
- Create `tests/hooks/test_session_hooks.py`: session helper tests.
- Create `AGENTS.md`: repository and Hook contribution instructions.
- Modify `README.md`: Hook architecture, usage, and tests.
- Modify `MAIN_README.md`: mirror the Hook documentation.

### Task 1: Core Hook Protocol and Manager

**Files:**
- Create: `tests/hooks/__init__.py`
- Create: `tests/hooks/test_manager.py`
- Create: `agent/hooks/types.py`
- Create: `agent/hooks/errors.py`
- Create: `agent/hooks/manager.py`
- Create: `agent/hooks/builtin.py`
- Create: `agent/hooks/__init__.py`

- [ ] **Step 1: Write failing manager tests**

Create `tests/hooks/test_manager.py` with `unittest.IsolatedAsyncioTestCase`. Use real async handlers and cover:

```python
from __future__ import annotations

import asyncio
import unittest

from agent.hooks import HookAction, HookEvent, HookManager, HookResult


class HookManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_handlers_run_by_priority_then_registration_order(self) -> None:
        manager = HookManager()
        calls: list[str] = []

        async def named(name: str, event: HookEvent) -> HookResult:
            calls.append(name)
            return HookResult()

        manager.register("prompt.before", lambda event: named("late", event), priority=200)
        manager.register("prompt.before", lambda event: named("first", event), priority=10)
        manager.register("prompt.before", lambda event: named("second", event), priority=10)

        await manager.emit(HookEvent("prompt.before", "s1", {"user_input": "hello"}))
        self.assertEqual(calls, ["first", "second", "late"])

    async def test_modify_flows_to_next_handler_and_context_accumulates(self) -> None:
        manager = HookManager()

        async def modify(event: HookEvent) -> HookResult:
            return HookResult(
                action=HookAction.MODIFY,
                updated_payload={"value": event.payload["value"] + 1},
                additional_context=["first"],
            )

        async def inspect(event: HookEvent) -> HookResult:
            self.assertEqual(event.payload, {"value": 2})
            return HookResult(additional_context=["second"])

        manager.register("prompt.before", modify)
        manager.register("prompt.before", inspect)
        result = await manager.emit(HookEvent("prompt.before", "s1", {"value": 1}))

        self.assertEqual(result.updated_payload, {"value": 2})
        self.assertEqual(result.additional_context, ["first", "second"])

    async def test_block_short_circuits_remaining_handlers(self) -> None:
        manager = HookManager()
        reached = False

        async def block(event: HookEvent) -> HookResult:
            return HookResult(action=HookAction.BLOCK, reason="policy")

        async def unreachable(event: HookEvent) -> HookResult:
            nonlocal reached
            reached = True
            return HookResult()

        manager.register("tool.before", block)
        manager.register("tool.before", unreachable)
        result = await manager.emit(HookEvent("tool.before", "s1", {"tool_name": "x"}))

        self.assertEqual(result.action, HookAction.BLOCK)
        self.assertEqual(result.reason, "policy")
        self.assertFalse(reached)

    async def test_exception_timeout_and_invalid_result_are_isolated(self) -> None:
        manager = HookManager(default_timeout=0.01)

        async def crash(event: HookEvent) -> HookResult:
            raise RuntimeError("boom")

        async def timeout(event: HookEvent) -> HookResult:
            await asyncio.sleep(0.1)
            return HookResult()

        async def invalid(event: HookEvent) -> HookResult:
            return HookResult(action=HookAction.MODIFY)

        manager.register("prompt.before", crash, name="crash")
        manager.register("prompt.before", timeout, name="timeout")
        manager.register("prompt.before", invalid, name="invalid")
        result = await manager.emit(HookEvent("prompt.before", "s1", {"value": 1}))

        self.assertEqual(result.action, HookAction.CONTINUE)
        self.assertEqual(result.updated_payload, {"value": 1})
        self.assertEqual([failure.handler_name for failure in result.failures], ["crash", "timeout", "invalid"])

    async def test_retry_is_only_valid_for_tool_error(self) -> None:
        manager = HookManager()

        async def retry(event: HookEvent) -> HookResult:
            return HookResult(action=HookAction.RETRY, updated_payload={"arguments": {"n": 2}})

        manager.register("prompt.before", retry)
        result = await manager.emit(HookEvent("prompt.before", "s1", {"arguments": {"n": 1}}))
        self.assertEqual(result.action, HookAction.CONTINUE)
        self.assertEqual(len(result.failures), 1)

    async def test_unregister_and_payload_copy(self) -> None:
        manager = HookManager()
        calls = 0

        async def mutate(event: HookEvent) -> HookResult:
            nonlocal calls
            calls += 1
            event.payload["top"] = "changed"
            return HookResult()

        unregister = manager.register("session.start", mutate)
        payload = {"top": "original"}
        await manager.emit(HookEvent("session.start", "s1", payload))
        unregister()
        await manager.emit(HookEvent("session.start", "s1", payload))

        self.assertEqual(payload, {"top": "original"})
        self.assertEqual(calls, 1)
```

- [ ] **Step 2: Run the manager tests and verify RED**

Run:

```bash
python -m unittest tests.hooks.test_manager -v
```

Expected: import failure because `agent.hooks` does not exist.

- [ ] **Step 3: Implement the typed protocol**

Create `agent/hooks/types.py` with:

```python
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Literal

HookEventName = Literal[
    "session.start", "session.end", "prompt.before", "tool.before",
    "tool.after", "tool.error", "context.before_compact", "agent.before_stop",
]


class HookAction(str, Enum):
    CONTINUE = "continue"
    MODIFY = "modify"
    BLOCK = "block"
    RETRY = "retry"


@dataclass(frozen=True)
class HookFailure:
    handler_name: str
    error_type: str
    message: str


@dataclass(frozen=True)
class HookEvent:
    name: HookEventName
    session_id: str
    payload: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)

    def copied(self, payload: dict[str, Any]) -> "HookEvent":
        return HookEvent(self.name, self.session_id, dict(payload), dict(self.metadata))


@dataclass(frozen=True)
class HookResult:
    action: HookAction = HookAction.CONTINUE
    reason: str | None = None
    updated_payload: dict[str, Any] | None = None
    additional_context: list[str] = field(default_factory=list)
    failures: list[HookFailure] = field(default_factory=list)


HookHandler = Callable[[HookEvent], Awaitable[HookResult]]
```

Remove unused imports during implementation. Create `HookProtocolError(ValueError)` in `errors.py`.

- [ ] **Step 4: Implement the manager minimally**

Create `agent/hooks/manager.py` with a private ordered registration dataclass. `register()` increments a sequence counter and returns a closure that removes exactly that registration. `emit()` must:

```python
current_payload = dict(event.payload)
contexts: list[str] = []
failures: list[HookFailure] = []
registrations = sorted(self._handlers.get(event.name, ()), key=lambda item: (item.priority, item.sequence))

for registration in registrations:
    try:
        result = await asyncio.wait_for(
            registration.handler(event.copied(current_payload)),
            timeout=registration.timeout or self.default_timeout,
        )
        self._validate_result(event.name, result)
    except Exception as exc:
        failures.append(HookFailure(registration.name, type(exc).__name__, str(exc)))
        continue
    contexts.extend(result.additional_context)
    if result.action in {HookAction.MODIFY, HookAction.RETRY}:
        current_payload = dict(result.updated_payload or {})
    if result.action == HookAction.BLOCK:
        return HookResult(HookAction.BLOCK, result.reason, current_payload, contexts, failures)
    if result.action == HookAction.RETRY:
        return HookResult(HookAction.RETRY, result.reason, current_payload, contexts, failures)

return HookResult(HookAction.CONTINUE, updated_payload=current_payload, additional_context=contexts, failures=failures)
```

Validation requires a non-empty reason for `block`, payload for `modify/retry`, and permits `retry` only for `tool.error`.

Create `builtin.py`:

```python
from agent.hooks.manager import HookManager


def create_default_hook_manager() -> HookManager:
    return HookManager()
```

Export the public types and factory from `agent/hooks/__init__.py`.

- [ ] **Step 5: Run manager tests and verify GREEN**

Run:

```bash
python -m unittest tests.hooks.test_manager -v
```

Expected: all manager tests pass.

- [ ] **Step 6: Commit the core**

```bash
git add agent/hooks tests/hooks/__init__.py tests/hooks/test_manager.py
git commit -m "feat: add lifecycle hook manager"
```

### Task 2: Prompt Hook Integration

**Files:**
- Create: `tests/hooks/test_prompt_hooks.py`
- Modify: `agent/main_agent/query_engine.py`
- Modify: `agent/main_agent/graph.py`

- [ ] **Step 1: Write failing prompt integration tests**

Create two `IsolatedAsyncioTestCase` tests. A modifying handler replaces `user_input` and adds context; the fake `model_call` records the messages and yields one `assistant_delta`. Assert the model receives the modified prompt/context. A blocking handler must produce a `terminal` event with reason `hook_blocked` and leave the fake model call count at zero.

Use this event collector:

```python
events = [event async for event in engine.submit_message("original")]
self.assertTrue(any(event.get("type") == "terminal" for event in events))
```

- [ ] **Step 2: Run prompt tests and verify RED**

```bash
python -m unittest tests.hooks.test_prompt_hooks -v
```

Expected: `QueryEngine` rejects the `hook_manager` argument.

- [ ] **Step 3: Inject the manager and emit `prompt.before`**

Add `hook_manager: HookManager | None = None` to `QueryEngine`, `submitMessage`, `run_agent`, `_initial_graph_state`, and `AgentGraphState`. In `QueryEngine.submit_message`, emit:

```python
result = await self.hook_manager.emit(HookEvent(
    "prompt.before",
    self.session_id or "",
    {"user_input": user_input, "memory_context": memory_context},
))
```

On block, yield a terminal event with `reason="hook_blocked"` and return. Otherwise use the final payload and append `additional_context` to `memory_context` with newline separators. Add `hook_manager` to graph state so later tasks reuse the same instance.

Emit one `hook_error` event per isolated failure without including prompt contents.

- [ ] **Step 4: Run prompt and manager tests**

```bash
python -m unittest tests.hooks.test_prompt_hooks tests.hooks.test_manager -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit prompt integration**

```bash
git add agent/main_agent/query_engine.py agent/main_agent/graph.py tests/hooks/test_prompt_hooks.py
git commit -m "feat: run hooks before agent prompts"
```

### Task 3: Tool Lifecycle Hooks and Bounded Retry

**Files:**
- Create: `tests/hooks/test_tool_hooks.py`
- Modify: `agent/sub_agent/tool_runner.py`
- Modify: `agent/main_agent/tool_executor.py`
- Modify: `agent/main_agent/graph.py`

- [ ] **Step 1: Write failing tool pipeline tests**

Build a minimal tool registry whose runner appends received arguments and returns configurable results. Exercise `run_tool_subagent` directly with a permissive reviewer. Add separate tests asserting:

- `tool.before` modifies `{"n": 1}` to `{"n": 2}` before the runner sees it;
- `tool.before` block prevents the runner and returns `raw_result["hook_blocked"] is True`;
- `tool.after` replaces a successful raw result and content is regenerated from it;
- `tool.error` modifies a failed result;
- `tool.error` retry executes exactly once with updated arguments and succeeds;
- a second retry result is not consumed, so total runner calls never exceed two;
- Hook failures produce `hook_error` events but tool execution continues.

- [ ] **Step 2: Run tool tests and verify RED**

```bash
python -m unittest tests.hooks.test_tool_hooks -v
```

Expected: `run_tool_subagent` does not accept `hook_manager` or `session_id`.

- [ ] **Step 3: Add tool Hook helpers**

Add keyword-only `hook_manager` and `session_id` parameters to `run_tool_subagent`. After permission approval, call a helper that emits `tool.before` using:

```python
{
    "tool_name": name,
    "arguments": _tool_arguments(tool_call),
    "tool_call_id": tool_call.get("id", name),
}
```

Rebuild both top-level and OpenAI-style nested tool calls without mutating the original. Convert block into a normal tool message with `{"ok": False, "hook_blocked": True, "error": reason}`.

After `_run_tool_call`, emit `tool.after` if `raw_result.get("ok") is not False`; otherwise emit `tool.error`. Regenerate `arguments`, `summary`, `content`, and `raw_result` whenever a Hook modifies payload.

For `retry`, execute `_run_tool_call` once more using updated arguments, set metadata `{"hook_retry_attempt": 1}`, and process the final attempt through `tool.after` or `tool.error` while treating any second `retry` as an isolated `HookFailure`/continue result.

- [ ] **Step 4: Forward manager through the streaming executor**

Add constructor fields to `StreamingToolExecutor`, pass them from graph state, then pass them explicitly to `run_tool_subagent`. Keep Hook objects out of serializable checkpoint tool state.

- [ ] **Step 5: Run tool tests and relevant regression tests**

```bash
python -m unittest tests.hooks.test_tool_hooks tests.hooks.test_manager -v
python -m compileall -q agent
```

Expected: tests pass and compilation exits zero.

- [ ] **Step 6: Commit tool integration**

```bash
git add agent/sub_agent/tool_runner.py agent/main_agent/tool_executor.py agent/main_agent/graph.py tests/hooks/test_tool_hooks.py
git commit -m "feat: add tool lifecycle hooks"
```

### Task 4: Context Compaction and Agent Stop Hooks

**Files:**
- Create: `tests/hooks/test_context_and_stop_hooks.py`
- Modify: `agent/main_agent/context_manager.py`
- Modify: `agent/main_agent/graph.py`

- [ ] **Step 1: Write failing context and stop tests**

Test `manage_context` with a deliberately tiny `ContextConfig` so auto compaction is selected. Assert:

- a modifying `context.before_compact` handler replaces messages passed to the fake compaction model;
- a blocking handler skips auto compaction and adds an action with `level="hook_blocked"`;
- handler additional context becomes a protected system-style message before compaction.

Exercise `_termination_check_node` with a minimal graph state. Assert `agent.before_stop` block returns `hook_stopped`; continue returns `completed`; legacy `stop_hook` still returns `stop_hook_prevented`.

- [ ] **Step 2: Run tests and verify RED**

```bash
python -m unittest tests.hooks.test_context_and_stop_hooks -v
```

Expected: missing Hook parameters/behavior.

- [ ] **Step 3: Integrate `context.before_compact`**

Extend `manage_context` with optional `hook_manager`, `session_id`, and `run_id`. Emit only immediately before `auto_compact`. Payload contains `messages`, `token_count`, and `reason="auto_compact_threshold"`. On modify, use returned messages and recompute token count/warning. Convert additional context to a protected system message. On block, append:

```python
{"level": "hook_blocked", "event": "context.before_compact", "reason": result.reason}
```

Skip the current automatic compaction attempt. Return isolated failures in the context report so graph can emit redacted `hook_error` events.

- [ ] **Step 4: Integrate `agent.before_stop` and compatibility adapter**

In `_termination_check_node`, emit `agent.before_stop` before checkpoint completion. On block, keep the existing terminal `hook_stopped` meaning. Evaluate legacy `stop_hook` after the structured result and preserve `stop_hook_prevented` exactly. Do not run before-stop hooks on hard terminal paths such as `max_turns`, model errors, or user interruption.

Remove the misleading result-backfill invocation of `stop_hook`; normal stopping is centralized in `_termination_check_node`.

- [ ] **Step 5: Run tests and compile**

```bash
python -m unittest tests.hooks.test_context_and_stop_hooks tests.hooks.test_manager -v
python -m compileall -q agent tests
```

Expected: all tests pass; compilation exits zero.

- [ ] **Step 6: Commit context and stop integration**

```bash
git add agent/main_agent/context_manager.py agent/main_agent/graph.py tests/hooks/test_context_and_stop_hooks.py
git commit -m "feat: hook context compaction and agent stop"
```

### Task 5: Session Lifecycle, CLI Observability, and AGENTS.md

**Files:**
- Create: `tests/hooks/test_session_hooks.py`
- Modify: `agent/main_agent/cli.py`
- Create: `AGENTS.md`

- [ ] **Step 1: Write failing session helper tests**

Extract independently testable async helpers rather than driving the interactive CLI. Test that `emit_session_start()` sends session id/title/recovered state, applies allowed modifications, and returns failures. Test `emit_session_end()` always returns normally even if a handler blocks or raises, while preserving observable failure data.

- [ ] **Step 2: Run session tests and verify RED**

```bash
python -m unittest tests.hooks.test_session_hooks -v
```

Expected: session Hook helpers do not exist.

- [ ] **Step 3: Implement and connect session helpers**

Create one default manager at `chat_loop` startup. Emit `session.start` after checkpoint recovery and before tool loading is presented as ready. Emit `session.end` in a shared shutdown helper used by explicit exit and input interruption. Pass the same manager to every `QueryEngine` instance.

Extend `_print_event` for `hook_blocked`, `hook_error`, and `hook_retry` without printing raw payloads.

- [ ] **Step 4: Add root AGENTS.md**

Document:

- Python 3.10+ and `python -m unittest discover` verification;
- major runtime directories and dependency direction;
- Hook events and registration through `HookManager`;
- permission review must remain before `tool.before`;
- handlers must not log secrets or mutate received payloads directly;
- external command Hooks are out of scope;
- new behavior requires a failing test first;
- preserve unrelated dirty-worktree changes.

- [ ] **Step 5: Run session and full Hook tests**

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

Expected: all Hook unit and integration tests pass.

- [ ] **Step 6: Commit session integration and contributor instructions**

```bash
git add agent/main_agent/cli.py tests/hooks/test_session_hooks.py AGENTS.md
git commit -m "feat: add session hooks and contributor guidance"
```

### Task 6: README Documentation

**Files:**
- Modify: `README.md`
- Modify: `MAIN_README.md`

- [ ] **Step 1: Add Hook documentation to README.md**

Add a “Lifecycle Hooks” section containing:

- why Hooks exist and how they differ from permission review;
- the eight supported events and their allowed actions;
- a complete async Python registration example;
- priority, block, error isolation, and one-retry semantics;
- statement that `model.before/model.after` and external command Hooks are deferred;
- test command and links to `agent/hooks/` and `AGENTS.md`.

Update the architecture tree/table so `hooks/` appears as a first-class subsystem.

- [ ] **Step 2: Mirror the same factual section to MAIN_README.md**

Keep the two current top-level documents consistent. Do not overwrite unrelated existing README edits; apply a narrow patch around stable headings.

- [ ] **Step 3: Check documentation consistency**

```bash
rg -n "session.start|prompt.before|tool.before|tool.after|tool.error|context.before_compact|agent.before_stop|session.end" README.md MAIN_README.md AGENTS.md
git diff --check -- README.md MAIN_README.md AGENTS.md
```

Expected: all eight event names appear in both README files; no whitespace errors.

- [ ] **Step 4: Commit documentation**

```bash
git add README.md MAIN_README.md
git commit -m "docs: document lifecycle hooks"
```

### Task 7: Completion Audit, Verification, and GitHub Update

**Files:**
- Inspect all feature files and current Git state.

- [ ] **Step 1: Run the complete fresh verification gate**

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
python -m compileall -q agent tests
git diff --check HEAD~5..HEAD
```

Expected: zero test failures, compile exit zero, diff check exit zero.

- [ ] **Step 2: Audit every acceptance criterion**

Verify with direct evidence:

```bash
find agent/hooks -maxdepth 1 -type f -print | sort
find . -maxdepth 1 -iname 'agents.md' -o -iname 'agent.md'
rg -n "HookManager|session.start|session.end|prompt.before|tool.before|tool.after|tool.error|context.before_compact|agent.before_stop" agent tests README.md MAIN_README.md AGENTS.md
git status --short
git log --oneline -8
```

Confirm all eight runtime integrations have tests, legacy `stop_hook` has a regression test, no external command loader exists, and unrelated worktree deletions were not staged into feature commits.

- [ ] **Step 3: Push the verified commits**

```bash
git push origin main
```

Expected: `origin/main` advances to the local verified HEAD at `github.com/Anorlx/code_agent`.

- [ ] **Step 4: Verify remote state**

```bash
git rev-parse HEAD
git ls-remote origin refs/heads/main
```

Expected: local and remote hashes match.
