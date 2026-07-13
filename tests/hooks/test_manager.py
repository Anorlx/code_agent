import asyncio
import time
import unittest

from agent.hooks import (
    HookAction,
    HookEvent,
    HookFailure,
    HookManager,
    HookResult,
    create_default_hook_manager,
)


class HookManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_action_contract_rejects_disallowed_actions(self) -> None:
        allowed = {
            "session.start": {HookAction.CONTINUE, HookAction.MODIFY},
            "session.end": {HookAction.CONTINUE},
            "prompt.before": {
                HookAction.CONTINUE,
                HookAction.MODIFY,
                HookAction.BLOCK,
            },
            "tool.before": {
                HookAction.CONTINUE,
                HookAction.MODIFY,
                HookAction.BLOCK,
            },
            "tool.after": {HookAction.CONTINUE, HookAction.MODIFY},
            "tool.error": {
                HookAction.CONTINUE,
                HookAction.MODIFY,
                HookAction.RETRY,
            },
            "context.before_compact": {
                HookAction.CONTINUE,
                HookAction.MODIFY,
                HookAction.BLOCK,
            },
            "agent.before_stop": {HookAction.CONTINUE, HookAction.BLOCK},
        }

        for event_name, allowed_actions in allowed.items():
            for action in set(HookAction) - allowed_actions:
                with self.subTest(event=event_name, action=action):
                    manager = HookManager()
                    observed: list[dict[str, object]] = []

                    async def disallowed(event: HookEvent) -> HookResult:
                        return HookResult(
                            action=action,
                            reason="policy" if action is HookAction.BLOCK else None,
                            updated_payload=(
                                {"value": "changed"}
                                if action in {HookAction.MODIFY, HookAction.RETRY}
                                else None
                            ),
                        )

                    async def later(event: HookEvent) -> HookResult:
                        observed.append(event.payload)
                        return HookResult()

                    manager.register(event_name, disallowed, name="disallowed")
                    manager.register(event_name, later, name="later")

                    result = await manager.emit(
                        HookEvent(event_name, "s1", {"value": "original"})
                    )

                    self.assertEqual(observed, [{"value": "original"}])
                    self.assertEqual(result.action, HookAction.CONTINUE)
                    self.assertEqual(result.updated_payload, {"value": "original"})
                    self.assertEqual(len(result.failures), 1)
                    self.assertEqual(
                        result.failures[0].error_type,
                        "HookProtocolError",
                    )

    async def test_handlers_run_by_priority_then_registration_order(self) -> None:
        manager = HookManager()
        calls: list[str] = []

        def handler(label: str):
            async def record(event: HookEvent) -> HookResult:
                calls.append(label)
                return HookResult()

            return record

        manager.register("session.start", handler("late"), priority=200)
        manager.register("session.start", handler("first"), priority=50)
        manager.register("session.start", handler("second"), priority=50)

        result = await manager.emit(HookEvent("session.start", "s1", {}))

        self.assertEqual(calls, ["first", "second", "late"])
        self.assertEqual(result.action, HookAction.CONTINUE)
        self.assertEqual(result.updated_payload, {})

    async def test_modify_propagates_payload_and_aggregates_context(self) -> None:
        manager = HookManager()
        seen: list[dict[str, object]] = []

        async def first(event: HookEvent) -> HookResult:
            return HookResult(
                action=HookAction.MODIFY,
                updated_payload={"value": 2},
                additional_context=["first context"],
            )

        async def second(event: HookEvent) -> HookResult:
            seen.append(event.payload)
            return HookResult(additional_context=["second context"])

        manager.register("prompt.before", first)
        manager.register("prompt.before", second)

        result = await manager.emit(
            HookEvent("prompt.before", "s1", {"value": 1})
        )

        self.assertEqual(seen, [{"value": 2}])
        self.assertEqual(result.updated_payload, {"value": 2})
        self.assertEqual(
            result.additional_context, ["first context", "second context"]
        )
        self.assertEqual(result.action, HookAction.CONTINUE)

    async def test_block_requires_reason_and_short_circuits(self) -> None:
        manager = HookManager()
        calls: list[str] = []

        async def invalid_block(event: HookEvent) -> HookResult:
            return HookResult(action=HookAction.BLOCK, reason="  ")

        async def valid_block(event: HookEvent) -> HookResult:
            calls.append("block")
            return HookResult(action=HookAction.BLOCK, reason="policy denied")

        async def later(event: HookEvent) -> HookResult:
            calls.append("later")
            return HookResult()

        manager.register("tool.before", invalid_block)
        manager.register("tool.before", valid_block)
        manager.register("tool.before", later)

        result = await manager.emit(HookEvent("tool.before", "s1", {"tool": "x"}))

        self.assertEqual(calls, ["block"])
        self.assertEqual(result.action, HookAction.BLOCK)
        self.assertEqual(result.reason, "policy denied")
        self.assertEqual(result.updated_payload, {"tool": "x"})
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.failures[0].error_type, "HookProtocolError")

    async def test_failures_are_isolated_and_dispatch_continues(self) -> None:
        manager = HookManager(default_timeout=0.01)
        calls: list[str] = []

        async def raises(event: HookEvent) -> HookResult:
            raise RuntimeError("boom")

        async def times_out(event: HookEvent) -> HookResult:
            await asyncio.sleep(0.1)
            return HookResult()

        async def invalid(event: HookEvent):
            return "not a result"

        async def succeeds(event: HookEvent) -> HookResult:
            calls.append("success")
            return HookResult()

        manager.register("tool.after", raises, name="raising handler")
        manager.register("tool.after", times_out, name="slow handler")
        manager.register("tool.after", invalid, name="invalid handler")
        manager.register("tool.after", succeeds)

        result = await manager.emit(HookEvent("tool.after", "s1", {}))

        self.assertEqual(calls, ["success"])
        self.assertEqual(
            [failure.handler_name for failure in result.failures],
            ["raising handler", "slow handler", "invalid handler"],
        )
        self.assertEqual(
            [failure.error_type for failure in result.failures],
            ["RuntimeError", "TimeoutError", "HookProtocolError"],
        )
        self.assertIn("timed out after", result.failures[1].message)
        self.assertIn("0.01", result.failures[1].message)

    async def test_retry_only_stops_tool_error_dispatch(self) -> None:
        manager = HookManager()
        tool_error_calls: list[str] = []

        async def retry(event: HookEvent) -> HookResult:
            return HookResult(
                action=HookAction.RETRY,
                reason="transient",
                updated_payload={"attempt": 2},
            )

        async def later_tool_error(event: HookEvent) -> HookResult:
            tool_error_calls.append("later")
            return HookResult()

        manager.register("tool.error", retry)
        manager.register("tool.error", later_tool_error)

        retry_result = await manager.emit(
            HookEvent("tool.error", "s1", {"attempt": 1})
        )

        self.assertEqual(retry_result.action, HookAction.RETRY)
        self.assertEqual(retry_result.updated_payload, {"attempt": 2})
        self.assertEqual(tool_error_calls, [])

        other_manager = HookManager()
        calls: list[str] = []

        async def after_invalid_retry(event: HookEvent) -> HookResult:
            calls.append("continued")
            return HookResult()

        other_manager.register("tool.after", retry)
        other_manager.register("tool.after", after_invalid_retry)
        result = await other_manager.emit(HookEvent("tool.after", "s1", {}))

        self.assertEqual(calls, ["continued"])
        self.assertEqual(result.action, HookAction.CONTINUE)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.failures[0].error_type, "HookProtocolError")

    async def test_unregister_callback_removes_exact_registration(self) -> None:
        manager = HookManager()
        calls: list[str] = []

        async def shared(event: HookEvent) -> HookResult:
            calls.append("called")
            return HookResult()

        unregister_first = manager.register("session.end", shared)
        manager.register("session.end", shared)
        unregister_first()
        unregister_first()

        await manager.emit(HookEvent("session.end", "s1", {}))

        self.assertEqual(calls, ["called"])

    async def test_each_handler_receives_top_level_copies(self) -> None:
        manager = HookManager()
        caller_payload = {"value": 1}
        caller_metadata = {"source": "caller"}
        seen: list[tuple[dict[str, object], dict[str, object]]] = []

        async def mutates(event: HookEvent) -> HookResult:
            event.payload["value"] = 99
            event.metadata["source"] = "handler"
            return HookResult()

        async def observes(event: HookEvent) -> HookResult:
            seen.append((event.payload, event.metadata))
            return HookResult()

        manager.register("agent.before_stop", mutates)
        manager.register("agent.before_stop", observes)

        result = await manager.emit(
            HookEvent("agent.before_stop", "s1", caller_payload, caller_metadata)
        )

        self.assertEqual(caller_payload, {"value": 1})
        self.assertEqual(caller_metadata, {"source": "caller"})
        self.assertEqual(seen, [({"value": 1}, {"source": "caller"})])
        self.assertEqual(result.updated_payload, {"value": 1})

    async def test_each_handler_receives_nested_copies(self) -> None:
        manager = HookManager()
        caller_payload = {"nested": {"values": [1]}}
        caller_metadata = {"nested": {"source": "caller"}}
        seen: list[tuple[dict[str, object], dict[str, object]]] = []

        async def mutates(event: HookEvent) -> HookResult:
            event.payload["nested"]["values"].append(2)
            event.metadata["nested"]["source"] = "handler"
            return HookResult()

        async def observes(event: HookEvent) -> HookResult:
            seen.append((event.payload, event.metadata))
            return HookResult()

        manager.register("session.start", mutates)
        manager.register("session.start", observes)

        result = await manager.emit(
            HookEvent("session.start", "s1", caller_payload, caller_metadata)
        )

        self.assertEqual(caller_payload, {"nested": {"values": [1]}})
        self.assertEqual(caller_metadata, {"nested": {"source": "caller"}})
        self.assertEqual(
            seen,
            [
                (
                    {"nested": {"values": [1]}},
                    {"nested": {"source": "caller"}},
                )
            ],
        )
        self.assertEqual(result.updated_payload, {"nested": {"values": [1]}})

    async def test_noncopyable_event_is_isolated_and_dispatch_continues(self) -> None:
        manager = HookManager()
        calls: list[str] = []

        class FailsFirstCopy:
            copy_attempts = 0

            def __deepcopy__(self, memo):
                type(self).copy_attempts += 1
                if type(self).copy_attempts == 1:
                    raise TypeError("private copy failure")
                return "safe copy"

        value = FailsFirstCopy()

        async def skipped(event: HookEvent) -> HookResult:
            calls.append("first")
            return HookResult()

        async def later(event: HookEvent) -> HookResult:
            calls.append("later")
            self.assertEqual(event.payload["value"], "safe copy")
            return HookResult()

        manager.register("session.start", skipped, name="first handler")
        manager.register("session.start", later, name="later handler")

        result = await manager.emit(
            HookEvent("session.start", "s1", {"value": value})
        )

        self.assertEqual(calls, ["later"])
        self.assertIs(result.updated_payload["value"], value)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.failures[0].handler_name, "first handler")
        self.assertEqual(result.failures[0].error_type, "TypeError")

    async def test_permanently_noncopyable_event_fails_each_handler(self) -> None:
        manager = HookManager()
        calls: list[str] = []

        class NonCopyable:
            def __deepcopy__(self, memo):
                raise TypeError("private copy failure")

        async def handler(event: HookEvent) -> HookResult:
            calls.append("called")
            return HookResult()

        manager.register("session.start", handler, name="first")
        manager.register("session.start", handler, name="second")
        value = NonCopyable()

        result = await manager.emit(
            HookEvent("session.start", "s1", {"value": value})
        )

        self.assertEqual(calls, [])
        self.assertIs(result.updated_payload["value"], value)
        self.assertEqual(
            [failure.handler_name for failure in result.failures],
            ["first", "second"],
        )
        self.assertTrue(
            all(failure.error_type == "TypeError" for failure in result.failures)
        )

    async def test_slow_deepcopy_is_timed_out_without_blocking_event_loop(self) -> None:
        manager = HookManager(default_timeout=0.02)
        handler_called = False
        ticked = asyncio.Event()

        class SlowCopy:
            def __deepcopy__(self, memo):
                time.sleep(0.15)
                return "copied"

        async def handler(event: HookEvent) -> HookResult:
            nonlocal handler_called
            handler_called = True
            return HookResult()

        async def tick() -> None:
            await asyncio.sleep(0.005)
            ticked.set()

        manager.register("session.start", handler, name="slow copy")
        tick_task = asyncio.create_task(tick())
        started = time.perf_counter()

        result = await manager.emit(
            HookEvent("session.start", "s1", {"value": SlowCopy()})
        )
        elapsed = time.perf_counter() - started
        await tick_task

        self.assertLess(elapsed, 0.1)
        self.assertTrue(ticked.is_set())
        self.assertFalse(handler_called)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.failures[0].error_type, "TimeoutError")

    async def test_handler_failures_from_results_are_aggregated(self) -> None:
        manager = HookManager()
        supplied = HookFailure("nested", "Warning", "reported")

        async def handler(event: HookEvent) -> HookResult:
            return HookResult(failures=[supplied])

        manager.register("context.before_compact", handler)

        result = await manager.emit(
            HookEvent("context.before_compact", "s1", {})
        )

        self.assertEqual(result.failures, [supplied])

    async def test_malformed_updated_payload_is_isolated(self) -> None:
        manager = HookManager()
        calls: list[str] = []

        async def malformed(event: HookEvent) -> HookResult:
            return HookResult(
                action=HookAction.MODIFY,
                updated_payload=42,  # type: ignore[arg-type]
            )

        async def later(event: HookEvent) -> HookResult:
            calls.append("continued")
            return HookResult()

        manager.register("prompt.before", malformed)
        manager.register("prompt.before", later)

        result = await manager.emit(HookEvent("prompt.before", "s1", {}))

        self.assertEqual(calls, ["continued"])
        self.assertEqual(result.updated_payload, {})
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.failures[0].error_type, "HookProtocolError")

    async def test_malformed_context_and_failures_are_isolated(self) -> None:
        manager = HookManager()
        calls: list[str] = []

        async def context_is_none(event: HookEvent) -> HookResult:
            return HookResult(additional_context=None)  # type: ignore[arg-type]

        async def context_has_non_string(event: HookEvent) -> HookResult:
            return HookResult(additional_context=[42])  # type: ignore[list-item]

        async def failures_is_none(event: HookEvent) -> HookResult:
            return HookResult(failures=None)  # type: ignore[arg-type]

        async def failures_has_wrong_type(event: HookEvent) -> HookResult:
            return HookResult(failures=["bad"])  # type: ignore[list-item]

        async def later(event: HookEvent) -> HookResult:
            calls.append("continued")
            return HookResult()

        manager.register("prompt.before", context_is_none)
        manager.register("prompt.before", context_has_non_string)
        manager.register("prompt.before", failures_is_none)
        manager.register("prompt.before", failures_has_wrong_type)
        manager.register("prompt.before", later)

        result = await manager.emit(HookEvent("prompt.before", "s1", {}))

        self.assertEqual(calls, ["continued"])
        self.assertEqual(len(result.failures), 4)
        self.assertTrue(
            all(
                failure.error_type == "HookProtocolError"
                for failure in result.failures
            )
        )

    async def test_cancelling_emit_cancels_active_handler(self) -> None:
        manager = HookManager(default_timeout=1)
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def waits(event: HookEvent) -> HookResult:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        manager.register("session.start", waits)
        emit_task = asyncio.create_task(
            manager.emit(HookEvent("session.start", "s1", {}))
        )
        await started.wait()

        emit_task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await emit_task
        await asyncio.wait_for(cancelled.wait(), timeout=0.1)

    def test_configuration_and_default_factory(self) -> None:
        with self.assertRaises(ValueError):
            HookManager(default_timeout=0)
        manager = HookManager()
        with self.assertRaises(ValueError):
            manager.register("session.start", self._unused_handler, timeout=-1)
        self.assertIsInstance(create_default_hook_manager(), HookManager)

    @staticmethod
    async def _unused_handler(event: HookEvent) -> HookResult:
        return HookResult()


if __name__ == "__main__":
    unittest.main()
