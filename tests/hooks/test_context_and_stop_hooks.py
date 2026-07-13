import json
import unittest
from unittest.mock import AsyncMock, patch

from langgraph.graph import END

from agent.hooks import HookAction, HookEvent, HookManager, HookResult
from agent.main_agent.context_manager import ContextConfig, manage_context
from agent.main_agent.graph import (
    _checkpoint_payload,
    _initial_graph_state,
    _preprocess_node,
    _result_backfill_node,
    _route_after_termination_check,
    _termination_check_node,
    _visible_state,
)


class RecordingCompactionModel:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    async def __call__(self, **request):
        self.requests.append(request)
        yield {"type": "assistant_delta", "content": "<summary>compacted</summary>"}


class RecordingCheckpointStore:
    def __init__(self) -> None:
        self.completed: list[str] = []
        self.aborted: list[tuple[str, str]] = []

    async def mark_completed(self, run_id: str) -> None:
        self.completed.append(run_id)

    async def mark_aborted(self, run_id: str, reason: str) -> None:
        self.aborted.append((run_id, reason))


def tiny_config() -> ContextConfig:
    return ContextConfig(
        effective_limit=100,
        warning_margin=10,
        blocking_margin=5,
        auto_compact_ratio=0.5,
        collapse_commit_ratio=0.8,
        collapse_block_spawn_ratio=0.95,
        micro_compact_idle_seconds=10_000,
        micro_compact_keep_recent=2,
        auto_compact_model_name="compact-test",
    )


def long_messages() -> list[dict[str, object]]:
    return [{"role": "user", "content": "original-secret-" + "x" * 400}]


def graph_state(**overrides):
    async def unused_model(**request):
        if False:
            yield request

    state = _initial_graph_state(
        user_input="hello",
        history=None,
        model_call=unused_model,
        tool_selector=None,
        tools={},
        max_turns=3,
        blocking_token_limit=10_000,
        stop_hook=None,
        main_model_name="main",
        subagent_model_name="sub",
        permission_reviewer=None,
        permission_prompter=None,
        reviewer_model_name="reviewer",
        session_id="session-1",
    )
    state.update({"turn": 1, "run_id": "run-1", **overrides})
    return state


class ContextBeforeCompactHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_modify_replaces_messages_recomputes_warning_and_reaches_model(self) -> None:
        manager = HookManager()
        model = RecordingCompactionModel()
        original = long_messages()
        modified = [{"role": "user", "content": "modified-" + "y" * 300}]
        seen: list[HookEvent] = []

        async def modify(event: HookEvent) -> HookResult:
            seen.append(event)
            return HookResult(
                action=HookAction.MODIFY,
                updated_payload={"messages": modified},
            )

        manager.register("context.before_compact", modify)
        result, report = await manage_context(
            original,
            system_prompt="system",
            model_call=model,
            config=tiny_config(),
            hook_manager=manager,
            session_id="session-1",
            run_id="run-1",
        )

        self.assertEqual(seen[0].session_id, "session-1")
        self.assertEqual(seen[0].metadata, {"run_id": "run-1"})
        self.assertEqual(seen[0].payload["reason"], "auto_compact_threshold")
        self.assertEqual(seen[0].payload["messages"], original)
        self.assertIsInstance(seen[0].payload["token_count"], int)
        payload = json.loads(model.requests[0]["messages"][0]["content"])
        self.assertEqual(payload["messages"], modified)
        self.assertEqual(original, long_messages())
        self.assertEqual(result[1]["type"], "compact_summary")
        self.assertEqual(report["actions"][-1]["level"], "auto_compact")

    async def test_modify_below_threshold_skips_compaction(self) -> None:
        manager = HookManager()
        model = RecordingCompactionModel()
        modified = [{"role": "user", "content": "small"}]

        async def modify(event: HookEvent) -> HookResult:
            return HookResult(
                action=HookAction.MODIFY,
                updated_payload={"messages": modified},
            )

        manager.register("context.before_compact", modify)
        result, report = await manage_context(
            long_messages(),
            system_prompt="s",
            model_call=model,
            config=tiny_config(),
            hook_manager=manager,
        )

        self.assertEqual(result, modified)
        self.assertEqual(model.requests, [])
        self.assertFalse(report["token_warning"]["isAboveAutoCompactThreshold"])
        self.assertFalse(any(a.get("level") == "auto_compact" for a in report["actions"]))

    async def test_block_skips_only_compaction_and_redacts_reason(self) -> None:
        manager = HookManager()
        model = RecordingCompactionModel()
        messages = long_messages()
        secret = "policy-secret-8675309"

        async def block(event: HookEvent) -> HookResult:
            return HookResult(action=HookAction.BLOCK, reason=secret)

        manager.register("context.before_compact", block)
        result, report = await manage_context(
            messages,
            system_prompt="s",
            model_call=model,
            config=tiny_config(),
            hook_manager=manager,
        )

        self.assertEqual(result, messages)
        self.assertEqual(model.requests, [])
        self.assertTrue(report["token_warning"]["isAboveAutoCompactThreshold"])
        self.assertIn(
            {
                "level": "hook_blocked",
                "event": "context.before_compact",
                "reason": "Hook policy blocked automatic compaction.",
            },
            report["actions"],
        )
        self.assertNotIn(secret, json.dumps(report))

    async def test_additional_context_is_a_protected_system_message_for_model(self) -> None:
        manager = HookManager()
        model = RecordingCompactionModel()

        async def add_context(event: HookEvent) -> HookResult:
            return HookResult(additional_context=["retain this fact"])

        manager.register("context.before_compact", add_context)
        await manage_context(
            long_messages(),
            system_prompt="s",
            model_call=model,
            config=tiny_config(),
            hook_manager=manager,
        )

        payload = json.loads(model.requests[0]["messages"][0]["content"])
        protected = [
            message
            for message in payload["messages"]
            if message.get("type") == "hook_context"
        ]
        self.assertEqual(len(protected), 1)
        self.assertEqual(protected[0]["role"], "system")
        self.assertIs(protected[0]["protected"], True)
        self.assertEqual(protected[0]["content"], "retain this fact")

    async def test_invalid_modified_messages_preserve_input_and_report_safe_failure(self) -> None:
        manager = HookManager()
        model = RecordingCompactionModel()
        messages = long_messages()

        async def invalid(event: HookEvent) -> HookResult:
            return HookResult(
                action=HookAction.MODIFY,
                updated_payload={"messages": ["secret-invalid-entry"]},
            )

        manager.register("context.before_compact", invalid, name="bad schema")
        _, report = await manage_context(
            messages,
            system_prompt="s",
            model_call=model,
            config=tiny_config(),
            hook_manager=manager,
        )

        payload = json.loads(model.requests[0]["messages"][0]["content"])
        self.assertEqual(payload["messages"], messages)
        self.assertEqual(
            report["hook_failures"],
            [
                {
                    "event": "context.before_compact",
                    "handler_name": "context.before_compact payload",
                    "error_type": "HookPayloadError",
                    "message": "Hook handler failed during context.before_compact.",
                }
            ],
        )
        self.assertNotIn("secret-invalid-entry", json.dumps(report))

    async def test_handler_failure_is_serializable_and_opaque(self) -> None:
        manager = HookManager()
        secret = "handler-secret-314159"

        async def raises(event: HookEvent) -> HookResult:
            raise RuntimeError(secret)

        manager.register("context.before_compact", raises, name="unsafe handler")
        _, report = await manage_context(
            long_messages(),
            system_prompt="s",
            model_call=RecordingCompactionModel(),
            config=tiny_config(),
            hook_manager=manager,
        )

        json.dumps(report)
        self.assertEqual(report["hook_failures"][0]["event"], "context.before_compact")
        self.assertEqual(report["hook_failures"][0]["handler_name"], "unsafe handler")
        self.assertEqual(report["hook_failures"][0]["error_type"], "RuntimeError")
        self.assertEqual(
            report["hook_failures"][0]["message"],
            "Hook handler failed during context.before_compact.",
        )
        self.assertNotIn(secret, json.dumps(report))

    async def test_hook_mutation_cannot_change_caller_owned_nested_messages(self) -> None:
        manager = HookManager()
        messages = [
            {
                "role": "assistant",
                "content": "x" * 400,
                "tool_calls": [{"id": "original-id"}],
            }
        ]

        async def mutates(event: HookEvent) -> HookResult:
            event.payload["messages"][0]["tool_calls"][0]["id"] = "mutated-id"
            return HookResult(action=HookAction.BLOCK, reason="test")

        manager.register("context.before_compact", mutates)
        await manage_context(
            messages,
            system_prompt="s",
            model_call=RecordingCompactionModel(),
            config=tiny_config(),
            hook_manager=manager,
        )

        self.assertEqual(messages[0]["tool_calls"][0]["id"], "original-id")

    async def test_none_manager_preserves_compaction_behavior(self) -> None:
        model = RecordingCompactionModel()
        result, report = await manage_context(
            long_messages(),
            system_prompt="s",
            model_call=model,
            config=tiny_config(),
        )

        self.assertEqual(len(model.requests), 1)
        self.assertEqual(result[1]["type"], "compact_summary")
        self.assertNotIn("hook_failures", report)

    async def test_graph_forwards_hook_context_and_emits_redacted_failures(self) -> None:
        manager = HookManager()
        events: list[dict[str, object]] = []
        state = graph_state(hook_manager=manager, event_sink=events.append)
        safe_report = {
            "actions": [],
            "token_warning": {"isAboveCollapseSpawnThreshold": False},
            "token_count": 1,
            "spawn_blocked": False,
            "hook_failures": [
                {
                    "event": "context.before_compact",
                    "handler_name": "unsafe handler",
                    "error_type": "RuntimeError",
                    "message": "Hook handler failed during context.before_compact.",
                }
            ],
        }

        with (
            patch(
                "agent.main_agent.graph.manage_context",
                AsyncMock(return_value=(state["messages"], safe_report)),
            ) as mocked_manage,
            patch(
                "agent.main_agent.graph.route_agent_mode",
                AsyncMock(return_value={"mode": "chat", "reason": "test"}),
            ),
        ):
            await _preprocess_node(state)

        kwargs = mocked_manage.await_args.kwargs
        self.assertIs(kwargs["hook_manager"], manager)
        self.assertEqual(kwargs["session_id"], "session-1")
        self.assertEqual(kwargs["run_id"], "run-1")
        error = next(event for event in events if event.get("type") == "hook_error")
        self.assertEqual(
            error,
            {
                "type": "hook_error",
                "event_name": "context.before_compact",
                "handler_name": "unsafe handler",
                "error_type": "RuntimeError",
                "message": "Hook handler failed during context.before_compact.",
            },
        )


class AgentBeforeStopHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_block_continues_to_preprocess_without_completing_checkpoint(self) -> None:
        manager = HookManager()
        store = RecordingCheckpointStore()
        events: list[dict[str, object]] = []
        state = graph_state(
            hook_manager=manager,
            checkpoint_store=store,
            event_sink=events.append,
        )
        secret = "stop-policy-secret-271828"

        async def block(event: HookEvent) -> HookResult:
            self.assertEqual(event.session_id, "session-1")
            self.assertEqual(event.metadata, {"run_id": "run-1"})
            self.assertEqual(event.payload, _visible_state(state))
            event.payload["phase"] = "mutated"
            return HookResult(
                action=HookAction.BLOCK,
                reason=secret,
                additional_context=["try another approach"],
            )

        manager.register("agent.before_stop", block)
        update = await _termination_check_node(state)

        self.assertEqual(state["phase"], "初始化")
        self.assertIsNone(update["termination_reason"])
        self.assertEqual(update["phase"], "stop_blocked")
        self.assertEqual(update["messages"][-1]["type"], "hook_context")
        self.assertIs(update["messages"][-1]["protected"], True)
        self.assertEqual(store.completed, [])
        self.assertEqual(store.aborted, [])
        self.assertEqual(_route_after_termination_check(update), "preprocess")
        blocked = next(event for event in events if event.get("type") == "hook_blocked")
        self.assertEqual(
            blocked,
            {
                "type": "hook_blocked",
                "event_name": "agent.before_stop",
                "message": "Hook policy blocked agent stopping.",
            },
        )
        self.assertNotIn(secret, json.dumps(events))

    async def test_continue_completes_and_marks_checkpoint(self) -> None:
        manager = HookManager()
        store = RecordingCheckpointStore()
        seen: list[str] = []

        async def continues(event: HookEvent) -> HookResult:
            seen.append(event.name)
            return HookResult()

        manager.register("agent.before_stop", continues)
        update = await _termination_check_node(
            graph_state(hook_manager=manager, checkpoint_store=store)
        )

        self.assertEqual(seen, ["agent.before_stop"])
        self.assertEqual(update["termination_reason"], "completed")
        self.assertEqual(store.completed, ["run-1"])
        self.assertEqual(_route_after_termination_check(update), END)

    async def test_failure_is_opaque_and_defaults_to_completion(self) -> None:
        manager = HookManager()
        store = RecordingCheckpointStore()
        events: list[dict[str, object]] = []
        secret = "stop-failure-secret-161803"

        async def raises(event: HookEvent) -> HookResult:
            raise ValueError(secret)

        manager.register("agent.before_stop", raises, name="unsafe stopper")
        update = await _termination_check_node(
            graph_state(
                hook_manager=manager,
                checkpoint_store=store,
                event_sink=events.append,
            )
        )

        self.assertEqual(update["termination_reason"], "completed")
        self.assertEqual(store.completed, ["run-1"])
        error = next(event for event in events if event.get("type") == "hook_error")
        self.assertEqual(
            error,
            {
                "type": "hook_error",
                "event_name": "agent.before_stop",
                "handler_name": "unsafe stopper",
                "error_type": "ValueError",
                "message": "Hook handler failed during agent.before_stop.",
            },
        )
        self.assertNotIn(secret, json.dumps(events))

    async def test_legacy_stop_hook_runs_after_structured_hook_with_exact_terminal_behavior(self) -> None:
        manager = HookManager()
        store = RecordingCheckpointStore()
        order: list[str] = []

        async def structured(event: HookEvent) -> HookResult:
            order.append("structured")
            return HookResult()

        def legacy(visible_state: dict[str, object]) -> bool:
            order.append("legacy")
            return True

        manager.register("agent.before_stop", structured)
        update = await _termination_check_node(
            graph_state(
                hook_manager=manager,
                stop_hook=legacy,
                checkpoint_store=store,
            )
        )

        self.assertEqual(order, ["structured", "legacy"])
        self.assertEqual(update["termination_reason"], "stop_hook_prevented")
        self.assertEqual(update["terminal_message"], "Stop hook 阻止继续。")
        self.assertEqual(store.completed, [])
        self.assertEqual(store.aborted, [("run-1", "stop_hook_prevented")])

    async def test_result_backfill_does_not_call_legacy_stop_hook(self) -> None:
        calls: list[dict[str, object]] = []

        def legacy(visible_state: dict[str, object]) -> bool:
            calls.append(visible_state)
            return True

        update = await _result_backfill_node(
            graph_state(stop_hook=legacy, tool_results=[])
        )

        self.assertEqual(calls, [])
        self.assertEqual(update["phase"], "结果回填")
        self.assertNotIn("termination_reason", update)

    async def test_hard_max_turns_path_does_not_emit_before_stop(self) -> None:
        manager = HookManager()
        calls: list[str] = []

        async def before_stop(event: HookEvent) -> HookResult:
            calls.append(event.name)
            return HookResult()

        manager.register("agent.before_stop", before_stop)
        update = await _preprocess_node(
            graph_state(hook_manager=manager, turn=3, max_turns=3)
        )

        self.assertEqual(update["termination_reason"], "max_turns")
        self.assertEqual(calls, [])

    def test_hook_manager_is_not_serialized(self) -> None:
        manager = HookManager()
        state = graph_state(hook_manager=manager)

        self.assertNotIn("hook_manager", _visible_state(state))
        self.assertNotIn("hook_manager", _checkpoint_payload(state))


if __name__ == "__main__":
    unittest.main()
