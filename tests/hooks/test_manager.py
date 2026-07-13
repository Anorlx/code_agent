import asyncio
import unittest

from agent.hooks import (
    HookAction,
    HookEvent,
    HookFailure,
    HookManager,
    HookProtocolError,
    HookResult,
    create_default_hook_manager,
)


class HookManagerTests(unittest.IsolatedAsyncioTestCase):
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
