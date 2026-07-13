import copy
import json
import unittest

from agent.hooks import HookAction, HookEvent, HookManager, HookResult
from agent.main_agent.tool_executor import StreamingToolExecutor
from agent.sub_agent.tool_runner import run_tool_subagent


async def collect_tool_events(
    tool_calls,
    tools,
    *,
    hook_manager=None,
    permission_reviewer=None,
    session_id="session-1",
    run_id="run-1",
):
    return [
        event
        async for event in run_tool_subagent(
            user_input="run the tool",
            messages=[],
            tool_calls=tool_calls,
            tools=tools,
            permission_reviewer=permission_reviewer,
            reviewer_model_name="reviewer",
            hook_manager=hook_manager,
            session_id=session_id,
            run_id=run_id,
        )
    ]


def number_tool(run, *, name="number", requires_review=False):
    return {
        name: {
            "run": run,
            "requires_review": requires_review,
            "permission": "allow",
            "parallel_safe": True,
            "spec": {
                "parameters": {
                    "type": "object",
                    "required": ["n"],
                    "properties": {"n": {"type": "integer"}},
                }
            },
        }
    }


def result_events(events):
    return [event for event in events if event.get("type") == "tool_result"]


class ToolHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_permission_review_precedes_before_modify_and_rebuilds_message(self):
        order = []
        calls = []
        manager = HookManager()

        async def review(*args):
            order.append("permission")
            return {"action": "allow", "allowed": True, "risk": "low"}

        async def before(event: HookEvent) -> HookResult:
            order.append("before")
            self.assertEqual(event.metadata["run_id"], "run-1")
            return HookResult(
                action=HookAction.MODIFY,
                updated_payload={**event.payload, "arguments": {"n": 2}},
            )

        async def run(arguments):
            calls.append(arguments)
            return {"ok": True, "content": f"n={arguments['n']}"}

        manager.register("tool.before", before)
        events = await collect_tool_events(
            [{"id": "call-1", "name": "number", "arguments": {"n": 1}}],
            number_tool(run, requires_review=True),
            hook_manager=manager,
            permission_reviewer=review,
        )

        message = result_events(events)[0]["message"]
        self.assertEqual(order, ["permission", "before"])
        self.assertEqual(calls, [{"n": 2}])
        self.assertEqual(message["arguments"], {"n": 2})
        self.assertEqual(message["summary"], "n=2")

    async def test_before_block_prevents_execution_and_later_hooks(self):
        calls = []
        manager = HookManager()

        async def block(event):
            calls.append("before")
            return HookResult(action=HookAction.BLOCK, reason="secret policy detail")

        async def after(event):
            calls.append("after")
            return HookResult()

        async def run(arguments):
            calls.append("runner")
            return {"ok": True}

        manager.register("tool.before", block)
        manager.register("tool.after", after)
        events = await collect_tool_events(
            [{"id": "call-1", "name": "number", "arguments": {"n": 1}}],
            number_tool(run),
            hook_manager=manager,
        )

        message = result_events(events)[0]["message"]
        self.assertEqual(calls, ["before"])
        self.assertFalse(message["raw_result"]["ok"])
        self.assertTrue(message["raw_result"]["hook_blocked"])
        self.assertEqual(message["raw_result"]["error"], "Tool blocked by lifecycle hook policy.")
        self.assertNotIn("secret policy detail", json.dumps(events))
        self.assertFalse(any(event.get("type") == "tool_start" for event in events))

    async def test_before_cannot_change_approved_tool_name(self):
        calls = []
        manager = HookManager()

        async def change_name(event):
            return HookResult(
                action=HookAction.MODIFY,
                updated_payload={**event.payload, "tool_name": "other"},
            )

        async def original(arguments):
            calls.append("original")
            return {"ok": True}

        async def other(arguments):
            calls.append("other")
            return {"ok": True}

        manager.register("tool.before", change_name)
        tools = number_tool(original)
        tools.update(number_tool(other, name="other"))
        events = await collect_tool_events(
            [{"id": "call-1", "name": "number", "arguments": {"n": 1}}],
            tools,
            hook_manager=manager,
        )

        message = result_events(events)[0]["message"]
        self.assertEqual(calls, [])
        self.assertEqual(message["name"], "number")
        self.assertTrue(message["raw_result"]["hook_blocked"])

    async def test_after_modify_replaces_success_result_and_content(self):
        manager = HookManager()

        async def after(event):
            return HookResult(
                action=HookAction.MODIFY,
                updated_payload={
                    **event.payload,
                    "result": {"ok": True, "content": "changed"},
                },
            )

        async def run(arguments):
            return {"ok": True, "content": "original"}

        manager.register("tool.after", after)
        events = await collect_tool_events(
            [{"id": "call-1", "name": "number", "arguments": {"n": 1}}],
            number_tool(run),
            hook_manager=manager,
        )

        message = result_events(events)[0]["message"]
        self.assertEqual(message["raw_result"], {"ok": True, "content": "changed"})
        self.assertEqual(message["content"], "changed")

    async def test_failed_result_runs_error_hook_and_can_be_replaced(self):
        manager = HookManager()
        seen = []

        async def error(event):
            seen.append(event.payload["result"])
            return HookResult(
                action=HookAction.MODIFY,
                updated_payload={
                    **event.payload,
                    "result": {"ok": False, "error": "replacement"},
                },
            )

        async def run(arguments):
            return {"ok": False, "error": "original"}

        manager.register("tool.error", error)
        events = await collect_tool_events(
            [{"id": "call-1", "name": "number", "arguments": {"n": 1}}],
            number_tool(run),
            hook_manager=manager,
        )

        message = result_events(events)[0]["message"]
        self.assertEqual(seen, [{"ok": False, "error": "original"}])
        self.assertEqual(message["raw_result"]["error"], "replacement")
        self.assertEqual(message["content"], "ERROR: replacement")

    async def test_runner_exception_becomes_failure_and_runs_error_hook(self):
        manager = HookManager()
        seen = []

        async def error(event):
            seen.append(event.payload["result"])
            return HookResult()

        async def run(arguments):
            raise RuntimeError("secret exception detail")

        manager.register("tool.error", error)
        with self.assertLogs("agent.sub_agent.tool_runner", level="ERROR") as logs:
            events = await collect_tool_events(
                [{"id": "call-1", "name": "number", "arguments": {"n": 1}}],
                number_tool(run),
                hook_manager=manager,
            )

        message = result_events(events)[0]["message"]
        self.assertEqual(seen, [{"ok": False, "error": "Tool execution failed."}])
        self.assertEqual(message["raw_result"], seen[0])
        self.assertNotIn("secret exception detail", json.dumps(events))
        self.assertNotIn("secret exception detail", "\n".join(logs.output))

    async def test_error_retry_runs_exactly_once_with_updated_arguments(self):
        manager = HookManager()
        calls = []

        async def retry(event):
            if event.payload["retry_attempt"] == 0:
                return HookResult(
                    action=HookAction.RETRY,
                    updated_payload={**event.payload, "arguments": {"n": 2}},
                )
            return HookResult()

        async def run(arguments):
            calls.append(arguments)
            if arguments["n"] == 1:
                return {"ok": False, "error": "transient secret"}
            return {"ok": True, "content": "recovered"}

        manager.register("tool.error", retry)
        events = await collect_tool_events(
            [{"id": "call-1", "name": "number", "arguments": {"n": 1}}],
            number_tool(run),
            hook_manager=manager,
        )

        self.assertEqual(calls, [{"n": 1}, {"n": 2}])
        retry_event = next(event for event in events if event.get("type") == "hook_retry")
        self.assertEqual(retry_event, {"type": "hook_retry", "name": "number", "attempt": 1})
        message = result_events(events)[0]["message"]
        self.assertEqual(message["content"], "recovered")
        self.assertEqual(message["arguments"], {"n": 2})

    async def test_second_retry_request_hits_limit_and_returns_final_failure(self):
        manager = HookManager()
        calls = []

        async def retry(event):
            return HookResult(
                action=HookAction.RETRY,
                updated_payload={
                    **event.payload,
                    "arguments": {"n": event.payload["arguments"]["n"] + 1},
                },
            )

        async def run(arguments):
            calls.append(arguments)
            return {"ok": False, "error": f"failed {arguments['n']} secret"}

        manager.register("tool.error", retry)
        events = await collect_tool_events(
            [{"id": "call-1", "name": "number", "arguments": {"n": 1}}],
            number_tool(run),
            hook_manager=manager,
        )

        self.assertEqual(calls, [{"n": 1}, {"n": 2}])
        message = result_events(events)[0]["message"]
        self.assertFalse(message["raw_result"]["ok"])
        self.assertEqual(message["arguments"], {"n": 2})
        self.assertEqual(message["raw_result"]["error"], "failed 2 secret")
        errors = [event for event in events if event.get("type") == "hook_error"]
        self.assertTrue(any(event["error_type"] == "RetryLimitExceeded" for event in errors))
        rendered = json.dumps(errors)
        self.assertNotIn("failed 1 secret", rendered)
        self.assertNotIn("failed 2 secret", rendered)

    async def test_handler_failure_is_opaque_and_execution_continues(self):
        manager = HookManager()

        async def raises(event):
            raise RuntimeError(f"secret payload {event.payload}")

        async def run(arguments):
            return {"ok": True, "content": "done"}

        manager.register("tool.before", raises, name="unsafe handler")
        events = await collect_tool_events(
            [{"id": "secret-id", "name": "number", "arguments": {"n": 987654}}],
            number_tool(run),
            hook_manager=manager,
        )

        error = next(event for event in events if event.get("type") == "hook_error")
        self.assertEqual(
            error,
            {
                "type": "hook_error",
                "event_name": "tool.before",
                "handler_name": "unsafe handler",
                "error_type": "RuntimeError",
                "message": "Hook handler failed during tool.before.",
            },
        )
        self.assertNotIn("987654", json.dumps(error))
        self.assertEqual(result_events(events)[0]["message"]["content"], "done")

    async def test_none_manager_preserves_behavior(self):
        async def run(arguments):
            return {"ok": True, "content": "unchanged"}

        events = await collect_tool_events(
            [{"id": "call-1", "name": "number", "arguments": {"n": 1}}],
            number_tool(run),
            hook_manager=None,
        )
        self.assertEqual(result_events(events)[0]["message"]["content"], "unchanged")
        self.assertFalse(any(event.get("type", "").startswith("hook_") for event in events))

    async def test_rebuilds_top_level_and_nested_calls_without_mutating_inputs(self):
        manager = HookManager()

        async def before(event):
            return HookResult(
                action=HookAction.MODIFY,
                updated_payload={**event.payload, "arguments": {"n": 2}},
            )

        async def run(arguments):
            return {"ok": True, "content": str(arguments["n"])}

        manager.register("tool.before", before)
        calls = [
            {"id": "top", "name": "number", "arguments": {"n": 1}},
            {
                "id": "nested",
                "type": "function",
                "function": {"name": "number", "arguments": '{"n": 1}'},
            },
        ]
        original = copy.deepcopy(calls)
        events = await collect_tool_events(calls, number_tool(run), hook_manager=manager)

        messages = [event["message"] for event in result_events(events)]
        self.assertEqual([message["tool_call_id"] for message in messages], ["top", "nested"])
        self.assertEqual([message["arguments"] for message in messages], [{"n": 2}, {"n": 2}])
        self.assertEqual(calls, original)

    async def test_streaming_executor_forwards_runtime_hook_identity_without_checkpointing_it(self):
        manager = HookManager()
        seen = []

        async def before(event):
            seen.append((event.session_id, event.metadata["run_id"]))
            return HookResult()

        async def run(arguments):
            return {"ok": True, "content": "done"}

        manager.register("tool.before", before)
        executor = StreamingToolExecutor(
            user_input="run",
            messages=[],
            tools=number_tool(run),
            permission_reviewer=None,
            permission_prompter=None,
            reviewer_model_name="reviewer",
            memory_context=None,
            runtime_context={},
            hook_manager=manager,
            session_id="session-x",
            run_id="run-x",
        )
        executor.submit({"id": "call-1", "name": "number", "arguments": {"n": 1}})
        await executor.finish()

        self.assertEqual(seen, [("session-x", "run-x")])
        self.assertIs(executor._hook_manager, manager)
        checkpoint = executor.checkpoint_tool_states()[0]
        self.assertNotIn("hook_manager", checkpoint)
        self.assertNotIn("session_id", checkpoint)
        self.assertNotIn("run_id", checkpoint)


if __name__ == "__main__":
    unittest.main()
