import json
import unittest
from unittest.mock import patch

from agent.hooks import HookAction, HookEvent, HookManager, HookResult
from agent.main_agent.graph import _checkpoint_payload, _initial_graph_state, _visible_state
from agent.main_agent.query_engine import QueryEngine


class RecordingModelCall:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    async def __call__(self, **request):
        self.requests.append(request)
        system_prompt = str(request.get("system_prompt") or "")
        if "Agent Mode Router" in system_prompt:
            yield {
                "type": "assistant_delta",
                "content": '{"mode":"chat","confidence":1,"reason":"test"}',
            }
        else:
            yield {"type": "assistant_delta", "content": "done"}


async def collect_events(engine: QueryEngine, user_input: str, memory_context=None):
    return [
        event
        async for event in engine.submit_message(
            user_input,
            memory_context=memory_context,
        )
    ]


class PromptHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_modify_changes_prompt_and_memory_before_every_model_call(self) -> None:
        manager = HookManager()
        model_call = RecordingModelCall()

        async def modify(event: HookEvent) -> HookResult:
            self.assertEqual(
                event.payload,
                {"user_input": "original prompt", "memory_context": "original memory"},
            )
            return HookResult(
                action=HookAction.MODIFY,
                updated_payload={
                    "user_input": "modified prompt",
                    "memory_context": "modified memory",
                },
                additional_context=["extra one", "extra two"],
            )

        manager.register("prompt.before", modify)
        engine = QueryEngine(
            model_call=model_call,
            tools={"unused": {}},
            hook_manager=manager,
            session_id="session-1",
        )

        events = await collect_events(engine, "original prompt", "original memory")

        self.assertEqual(events[-1]["reason"], "completed")
        self.assertGreaterEqual(len(model_call.requests), 2)
        rendered_requests = "\n".join(
            str(message.get("content") or "")
            for request in model_call.requests
            for message in request["messages"]
        )
        self.assertIn("modified prompt", rendered_requests)
        self.assertIn("modified memory\nextra one\nextra two", rendered_requests)
        self.assertNotIn("original prompt", rendered_requests)
        self.assertNotIn("original memory", rendered_requests)

    async def test_block_returns_terminal_event_without_calling_model(self) -> None:
        manager = HookManager()
        model_call = RecordingModelCall()

        async def block(event: HookEvent) -> HookResult:
            return HookResult(action=HookAction.BLOCK, reason="prompt rejected by policy")

        manager.register("prompt.before", block)
        engine = QueryEngine(model_call=model_call, hook_manager=manager)

        events = await collect_events(engine, "blocked prompt")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "terminal")
        self.assertEqual(events[0]["reason"], "hook_blocked")
        self.assertIn("prompt rejected by policy", events[0]["message"])
        self.assertEqual(model_call.requests, [])

    async def test_handler_failure_is_redacted_and_model_continues(self) -> None:
        manager = HookManager()
        model_call = RecordingModelCall()
        secret_prompt = "prompt-secret-314159"
        secret_memory = "memory-secret-271828"

        async def raises(event: HookEvent) -> HookResult:
            raise RuntimeError(f"cannot process {event.payload}")

        manager.register("prompt.before", raises, name="unsafe handler")
        engine = QueryEngine(model_call=model_call, hook_manager=manager)

        events = await collect_events(engine, secret_prompt, secret_memory)

        errors = [event for event in events if event.get("type") == "hook_error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(
            set(errors[0]),
            {"type", "event_name", "handler_name", "error_type", "message"},
        )
        self.assertEqual(errors[0]["event_name"], "prompt.before")
        self.assertEqual(errors[0]["handler_name"], "unsafe handler")
        self.assertEqual(errors[0]["error_type"], "RuntimeError")
        self.assertIn("cannot process", errors[0]["message"])
        rendered_error = json.dumps(errors[0], ensure_ascii=False)
        self.assertNotIn(secret_prompt, rendered_error)
        self.assertNotIn(secret_memory, rendered_error)
        self.assertGreater(len(model_call.requests), 0)
        self.assertEqual(events[-1]["reason"], "completed")

    async def test_failure_redacts_values_from_modified_payload(self) -> None:
        manager = HookManager()
        model_call = RecordingModelCall()
        original_prompt = "original-prompt-8675309"
        original_memory = "original-memory-112358"
        modified_prompt = "modified-prompt-424242"
        modified_memory = "modified-memory-161803"

        async def modify(event: HookEvent) -> HookResult:
            return HookResult(
                action=HookAction.MODIFY,
                updated_payload={
                    "user_input": modified_prompt,
                    "memory_context": modified_memory,
                },
            )

        async def raises(event: HookEvent) -> HookResult:
            raise RuntimeError(
                f"failed for {event.payload['user_input']} and "
                f"{event.payload['memory_context']}"
            )

        manager.register("prompt.before", modify)
        manager.register("prompt.before", raises, name="later handler")
        engine = QueryEngine(model_call=model_call, hook_manager=manager)

        events = await collect_events(engine, original_prompt, original_memory)

        error = next(event for event in events if event.get("type") == "hook_error")
        rendered_error = json.dumps(error, ensure_ascii=False)
        for sensitive in (
            original_prompt,
            original_memory,
            modified_prompt,
            modified_memory,
        ):
            self.assertNotIn(sensitive, rendered_error)
        rendered_requests = json.dumps(model_call.requests, ensure_ascii=False)
        self.assertIn(modified_prompt, rendered_requests)
        self.assertIn(modified_memory, rendered_requests)
        self.assertEqual(events[-1]["reason"], "completed")

    async def test_block_terminal_message_redacts_original_and_modified_values(self) -> None:
        manager = HookManager()
        model_call = RecordingModelCall()
        original_prompt = "original-block-prompt-123"
        original_memory = "original-block-memory-456"
        modified_prompt = "modified-block-prompt-789"
        modified_memory = "modified-block-memory-012"

        async def modify(event: HookEvent) -> HookResult:
            return HookResult(
                action=HookAction.MODIFY,
                updated_payload={
                    "user_input": modified_prompt,
                    "memory_context": modified_memory,
                },
            )

        async def block(event: HookEvent) -> HookResult:
            return HookResult(
                action=HookAction.BLOCK,
                reason=(
                    f"policy denied {original_prompt} {original_memory} "
                    f"{event.payload['user_input']} {event.payload['memory_context']}"
                ),
            )

        manager.register("prompt.before", modify)
        manager.register("prompt.before", block)
        engine = QueryEngine(model_call=model_call, hook_manager=manager)

        events = await collect_events(engine, original_prompt, original_memory)

        self.assertEqual(len(events), 1)
        terminal = events[0]
        self.assertEqual(terminal["reason"], "hook_blocked")
        self.assertIn("policy denied", terminal["message"])
        for sensitive in (
            original_prompt,
            original_memory,
            modified_prompt,
            modified_memory,
        ):
            self.assertNotIn(sensitive, terminal["message"])
        self.assertEqual(model_call.requests, [])

    async def test_invalid_modified_field_types_preserve_prior_values(self) -> None:
        manager = HookManager()
        model_call = RecordingModelCall()
        original_prompt = "valid prompt"
        original_memory = "valid memory"

        async def invalid_types(event: HookEvent) -> HookResult:
            return HookResult(
                action=HookAction.MODIFY,
                updated_payload={
                    "user_input": 123,
                    "memory_context": ["invalid"],
                },
            )

        manager.register("prompt.before", invalid_types, name="bad schema")
        engine = QueryEngine(model_call=model_call, hook_manager=manager)

        events = await collect_events(engine, original_prompt, original_memory)

        errors = [event for event in events if event.get("type") == "hook_error"]
        self.assertEqual(len(errors), 2)
        messages = "\n".join(str(event["message"]) for event in errors)
        self.assertIn("user_input", messages)
        self.assertIn("int", messages)
        self.assertIn("memory_context", messages)
        self.assertIn("list", messages)
        rendered_requests = json.dumps(model_call.requests, ensure_ascii=False)
        self.assertIn(original_prompt, rendered_requests)
        self.assertIn(original_memory, rendered_requests)
        self.assertEqual(events[-1]["reason"], "completed")

    async def test_none_hook_manager_preserves_existing_behavior(self) -> None:
        model_call = RecordingModelCall()
        engine = QueryEngine(model_call=model_call, hook_manager=None)

        events = await collect_events(engine, "hello", "remember this")

        self.assertGreater(len(model_call.requests), 0)
        self.assertEqual(events[-1]["reason"], "completed")
        self.assertFalse(any(event.get("type") == "hook_error" for event in events))

    async def test_hook_manager_is_forwarded_and_runtime_state_only(self) -> None:
        manager = HookManager()
        model_call = RecordingModelCall()
        captured: dict[str, object] = {}

        async def capturing_run_agent(**kwargs):
            captured.update(kwargs)
            yield {"type": "terminal", "reason": "captured", "message": "captured"}

        engine = QueryEngine(model_call=model_call, hook_manager=manager)
        with patch("agent.main_agent.query_engine.run_agent", capturing_run_agent):
            events = await collect_events(engine, "hello")

        self.assertEqual(events[-1]["reason"], "captured")
        self.assertIs(captured["hook_manager"], manager)

        state = _initial_graph_state(
            user_input="hello",
            history=None,
            model_call=model_call,
            tool_selector=None,
            tools={"unused": {}},
            max_turns=1,
            blocking_token_limit=1_000,
            stop_hook=None,
            main_model_name="main",
            subagent_model_name="sub",
            permission_reviewer=None,
            permission_prompter=None,
            reviewer_model_name="reviewer",
            hook_manager=manager,
        )

        self.assertIs(state["hook_manager"], manager)
        self.assertNotIn("hook_manager", _visible_state(state))
        self.assertNotIn("hook_manager", _checkpoint_payload(state))


if __name__ == "__main__":
    unittest.main()
