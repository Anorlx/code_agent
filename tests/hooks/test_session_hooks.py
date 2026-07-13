import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import AsyncMock, patch

from agent.hooks import HookAction, HookEvent, HookManager, HookResult
from agent.main_agent.cli import (
    SessionEndState,
    _create_query_engine,
    _print_event,
    _shutdown_background_tasks,
    emit_session_end,
    emit_session_start,
)
from agent.main_agent.session_store import SessionRecord
from agent.main_agent.terminal_ui import TerminalUI


def session_record(*, title: str = "private title") -> SessionRecord:
    return SessionRecord(
        id="session-123",
        title=title,
        summary="summary",
        created_at=1.0,
        updated_at=2.0,
        message_count=1,
    )


class SessionStartHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_event_is_minimal_and_payload_is_deep_copied(self) -> None:
        manager = HookManager()
        history = [{"role": "user", "content": {"text": "private prompt"}}]
        seen: list[HookEvent] = []

        async def observe(event: HookEvent) -> HookResult:
            seen.append(event)
            event.payload["history"][0]["content"]["text"] = "handler mutation"
            return HookResult()

        manager.register("session.start", observe)

        payload, events = await emit_session_start(
            manager,
            session_record(),
            history,
            recovered=True,
        )

        self.assertEqual(seen[0].name, "session.start")
        self.assertEqual(seen[0].session_id, "session-123")
        self.assertEqual(seen[0].metadata, {})
        self.assertEqual(
            set(seen[0].payload),
            {"session_id", "title", "history", "recovered"},
        )
        self.assertEqual(history[0]["content"]["text"], "private prompt")
        self.assertIsNot(payload["history"], history)
        self.assertEqual(payload["history"][0]["content"]["text"], "private prompt")
        self.assertTrue(payload["recovered"])
        self.assertEqual(events[-1]["type"], "session_hook")

    async def test_modify_accepts_valid_fields_without_mutating_caller(self) -> None:
        manager = HookManager()
        history = [{"role": "user", "content": {"text": "original"}}]
        replacement = [{"role": "assistant", "content": {"text": "replacement"}}]

        async def modify(event: HookEvent) -> HookResult:
            return HookResult(
                action=HookAction.MODIFY,
                updated_payload={
                    "title": "modified title",
                    "history": replacement,
                    "recovered": False,
                },
            )

        manager.register("session.start", modify)

        payload, _ = await emit_session_start(
            manager,
            session_record(),
            history,
            recovered=True,
        )
        replacement[0]["content"]["text"] = "changed after return"

        self.assertEqual(payload["session_id"], "session-123")
        self.assertEqual(payload["title"], "modified title")
        self.assertFalse(payload["recovered"])
        self.assertEqual(payload["history"][0]["content"]["text"], "replacement")
        self.assertEqual(history[0]["content"]["text"], "original")

    async def test_invalid_modified_fields_preserve_values_and_are_opaque(self) -> None:
        manager = HookManager()
        secret = "private-session-value-8675309"
        history = [{"role": "user", "content": secret}]

        async def invalid(event: HookEvent) -> HookResult:
            return HookResult(
                action=HookAction.MODIFY,
                updated_payload={
                    "session_id": "different-session",
                    "title": [secret],
                    "history": [{"role": "user"}, secret],
                    "recovered": secret,
                },
            )

        manager.register("session.start", invalid, name="invalid payload")

        payload, events = await emit_session_start(
            manager,
            session_record(title=secret),
            history,
            recovered=True,
        )

        self.assertEqual(
            payload,
            {
                "session_id": "session-123",
                "title": secret,
                "history": history,
                "recovered": True,
            },
        )
        errors = [event for event in events if event["type"] == "hook_error"]
        self.assertEqual(len(errors), 4)
        rendered = json.dumps(errors)
        self.assertNotIn(secret, rendered)
        self.assertNotIn("different-session", rendered)
        self.assertTrue(
            all(
                set(event)
                == {"type", "event_name", "handler_name", "error_type", "message"}
                for event in errors
            )
        )

    async def test_noncopyable_invalid_field_is_isolated(self) -> None:
        class NonCopyable:
            def __deepcopy__(self, memo):
                raise TypeError("private noncopyable detail")

        manager = HookManager()

        async def invalid(event: HookEvent) -> HookResult:
            return HookResult(
                action=HookAction.MODIFY,
                updated_payload={"title": NonCopyable()},
            )

        manager.register("session.start", invalid, name="invalid payload")

        payload, events = await emit_session_start(
            manager,
            session_record(),
            [],
            recovered=False,
        )

        self.assertEqual(payload["title"], "private title")
        self.assertTrue(any(event["type"] == "hook_error" for event in events))
        self.assertNotIn("private noncopyable detail", json.dumps(events))

    async def test_string_subclass_is_normalized_before_copying(self) -> None:
        class HostileString(str):
            def __deepcopy__(self, memo):
                raise TypeError("private title copy failure")

        manager = HookManager()

        async def modify(event: HookEvent) -> HookResult:
            self.assertIs(type(event.payload["title"]), str)
            return HookResult(
                action=HookAction.MODIFY,
                updated_payload={"title": HostileString("modified title")},
            )

        manager.register("session.start", modify)

        payload, events = await emit_session_start(
            manager,
            session_record(title=HostileString("original title")),
            [],
            recovered=False,
        )

        self.assertEqual(payload["title"], "modified title")
        self.assertIs(type(payload["title"]), str)
        self.assertNotIn("private title copy failure", json.dumps(events))

    async def test_block_does_not_abort_start_or_expose_reason(self) -> None:
        manager = HookManager()
        secret = "do-not-log-block-reason"

        async def block(event: HookEvent) -> HookResult:
            return HookResult(action=HookAction.BLOCK, reason=secret)

        manager.register("session.start", block, name="policy")

        payload, events = await emit_session_start(
            manager,
            session_record(),
            [],
            recovered=False,
        )

        self.assertEqual(payload["title"], "private title")
        self.assertEqual(events[-1]["type"], "session_hook")
        self.assertTrue(any(event["type"] == "hook_blocked" for event in events))
        self.assertNotIn(secret, json.dumps(events))

    async def test_handler_exception_is_opaque_and_start_continues(self) -> None:
        manager = HookManager()
        secret = "private-title-314159"

        async def raises(event: HookEvent) -> HookResult:
            raise RuntimeError(f"failed with {event.payload}")

        manager.register("session.start", raises, name="raising handler")

        payload, events = await emit_session_start(
            manager,
            session_record(title=secret),
            [{"role": "user", "content": secret}],
            recovered=False,
        )

        error = next(event for event in events if event["type"] == "hook_error")
        self.assertEqual(error["event_name"], "session.start")
        self.assertEqual(error["handler_name"], "raising handler")
        self.assertEqual(error["error_type"], "RuntimeError")
        self.assertEqual(error["message"], "Hook handler failed during session.start.")
        self.assertNotIn(secret, json.dumps(events))
        self.assertEqual(payload["title"], secret)

    async def test_none_manager_returns_independent_unchanged_payload(self) -> None:
        history = [{"role": "user", "content": {"text": "hello"}}]

        payload, events = await emit_session_start(
            None,
            session_record(),
            history,
            recovered=False,
        )

        payload["history"][0]["content"]["text"] = "changed"
        self.assertEqual(history[0]["content"]["text"], "hello")
        self.assertEqual(events, [])


class SessionEndHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_end_payload_is_minimal_and_actions_cannot_prevent_shutdown(self) -> None:
        manager = HookManager()
        seen: list[dict[str, object]] = []

        async def modify(event: HookEvent) -> HookResult:
            seen.append(event.payload)
            return HookResult(
                action=HookAction.MODIFY,
                updated_payload={"status": "keep-running", "history": ["secret"]},
            )

        manager.register("session.end", modify)

        events = await emit_session_end(
            manager,
            session_id="session-123",
            termination_reason="user_exit",
            status="completed",
            message_count=7,
        )

        self.assertEqual(
            seen,
            [
                {
                    "session_id": "session-123",
                    "termination_reason": "user_exit",
                    "status": "completed",
                    "message_count": 7,
                }
            ],
        )
        self.assertFalse(any("history" in event for event in seen))
        self.assertEqual(events[-1], {
            "type": "session_hook",
            "event_name": "session.end",
            "status": "observed",
        })

    async def test_end_block_and_exception_are_opaque_and_return_normally(self) -> None:
        manager = HookManager()
        secret = "secret-end-reason-271828"

        async def block(event: HookEvent) -> HookResult:
            return HookResult(action=HookAction.BLOCK, reason=secret)

        async def raises(event: HookEvent) -> HookResult:
            raise ValueError(secret)

        manager.register("session.end", block, name="blocking handler")
        manager.register("session.end", raises, name="raising handler")

        blocked_events = await emit_session_end(
            manager,
            session_id="session-123",
            termination_reason="interrupted",
            status="aborted",
            message_count=3,
        )

        self.assertTrue(any(event["type"] == "hook_blocked" for event in blocked_events))
        self.assertNotIn(secret, json.dumps(blocked_events))

        other_manager = HookManager()
        other_manager.register("session.end", raises, name="raising handler")
        error_events = await emit_session_end(
            other_manager,
            session_id="session-123",
            termination_reason="interrupted",
            status="aborted",
            message_count=3,
        )
        error = next(event for event in error_events if event["type"] == "hook_error")
        self.assertEqual(error["error_type"], "ValueError")
        self.assertNotIn(secret, json.dumps(error_events))

    async def test_none_manager_is_a_noop(self) -> None:
        events = await emit_session_end(
            None,
            session_id="session-123",
            termination_reason="user_exit",
            status="completed",
            message_count=0,
        )
        self.assertEqual(events, [])


class SessionCliIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_drains_before_emitting_end_and_only_emits_once(self) -> None:
        manager = HookManager()
        order: list[str] = []
        observer = AsyncMock()

        async def flush(*args, **kwargs) -> None:
            order.append("flush")

        observer.flush.side_effect = flush

        async def on_end(event: HookEvent) -> HookResult:
            order.append("end")
            return HookResult()

        manager.register("session.end", on_end)
        state = SessionEndState()
        rendered: list[dict[str, object]] = []

        await _shutdown_background_tasks(
            observer,
            set(),
            [{"role": "user", "content": "hello"}],
            False,
            hook_manager=manager,
            session_id="session-123",
            termination_reason="user_exit",
            status="completed",
            end_state=state,
            event_sink=rendered.append,
        )
        await _shutdown_background_tasks(
            observer,
            set(),
            [{"role": "user", "content": "hello"}],
            False,
            hook_manager=manager,
            session_id="session-123",
            termination_reason="user_exit",
            status="completed",
            end_state=state,
            event_sink=rendered.append,
        )

        self.assertEqual(order, ["flush", "end", "flush"])
        self.assertEqual(
            len([event for event in rendered if event["type"] == "session_hook"]),
            1,
        )

    def test_query_engine_factory_forwards_the_same_manager(self) -> None:
        manager = HookManager()
        ui = TerminalUI(color=False)

        with patch("agent.main_agent.cli.QueryEngine") as engine_class:
            _create_query_engine(
                tools={},
                checkpoint_store=object(),
                session_id="session-123",
                max_turns=4,
                ui=ui,
                hook_manager=manager,
            )

        self.assertIs(engine_class.call_args.kwargs["hook_manager"], manager)

    def test_hook_event_rendering_uses_only_safe_structural_fields(self) -> None:
        ui = TerminalUI(color=False)
        events = [
            {
                "type": "hook_error",
                "event_name": "session.start",
                "handler_name": "handler",
                "error_type": "RuntimeError",
                "message": "secret raw failure",
                "payload": "secret payload",
            },
            {
                "type": "hook_blocked",
                "event_name": "session.start",
                "reason": "secret block reason",
            },
            {
                "type": "hook_retry",
                "name": "safe_tool_name",
                "attempt": 2,
                "reason": "secret retry reason",
            },
            {
                "type": "session_hook",
                "event_name": "session.end",
                "status": "observed",
                "payload": "secret session payload",
            },
        ]

        output = io.StringIO()
        with redirect_stdout(output):
            for event in events:
                _print_event(event, ui)

        rendered = output.getvalue()
        self.assertIn("session.start", rendered)
        self.assertIn("RuntimeError", rendered)
        self.assertIn("session.end", rendered)
        self.assertIn("safe_tool_name", rendered)
        self.assertNotIn("secret", rendered)


if __name__ == "__main__":
    unittest.main()
