"""Registration and dispatch for agent lifecycle hooks."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
from itertools import count
from typing import Callable

from .errors import HookProtocolError
from .types import (
    ALLOWED_HOOK_ACTIONS,
    HookAction,
    HookEvent,
    HookEventName,
    HookFailure,
    HookHandler,
    HookResult,
)


def _copy_json_like(value: object) -> object:
    """Copy a Hook payload while normalizing JSON primitive subclasses."""
    if value is None:
        return None
    if isinstance(value, str):
        return str(value)
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, list):
        return [_copy_json_like(item) for item in value]
    if isinstance(value, dict):
        copied: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("hook payload keys must be strings")
            copied[str(key)] = _copy_json_like(item)
        return copied
    raise TypeError("hook payload values must be JSON-like")


@dataclass(frozen=True)
class _Registration:
    event_name: HookEventName
    handler: HookHandler
    priority: int
    sequence: int
    name: str
    timeout: float


class HookManager:
    def __init__(self, default_timeout: float = 5.0) -> None:
        if default_timeout <= 0:
            raise ValueError("default_timeout must be positive")
        self.default_timeout = default_timeout
        self._registrations: list[_Registration] = []
        self._sequence = count()

    def register(
        self,
        event_name: HookEventName,
        handler: HookHandler,
        priority: int = 100,
        name: str | None = None,
        timeout: float | None = None,
    ) -> Callable[[], None]:
        effective_timeout = self.default_timeout if timeout is None else timeout
        if effective_timeout <= 0:
            raise ValueError("timeout must be positive")
        registration = _Registration(
            event_name=event_name,
            handler=handler,
            priority=priority,
            sequence=next(self._sequence),
            name=name or getattr(handler, "__name__", handler.__class__.__name__),
            timeout=effective_timeout,
        )
        self._registrations.append(registration)

        def unregister() -> None:
            self._registrations = [
                item for item in self._registrations if item is not registration
            ]

        return unregister

    async def emit(self, event: HookEvent) -> HookResult:
        registrations = sorted(
            (
                item
                for item in self._registrations
                if item.event_name == event.name
            ),
            key=lambda item: (item.priority, item.sequence),
        )
        current_payload = dict(event.payload)
        additional_context: list[str] = []
        failures: list[HookFailure] = []

        for registration in registrations:
            try:
                # wait_for cancellation is cooperative: it cancels the handler
                # delivery task and waits for that cancellation to finish.
                result = await asyncio.wait_for(
                    self._deliver(registration, event, current_payload),
                    timeout=registration.timeout,
                )
            except asyncio.TimeoutError:
                failures.append(
                    HookFailure(
                        handler_name=registration.name,
                        error_type="TimeoutError",
                        message=(
                            f"handler timed out after {registration.timeout} seconds"
                        ),
                    )
                )
                continue
            except Exception as error:
                failures.append(
                    HookFailure(
                        handler_name=registration.name,
                        error_type=type(error).__name__,
                        message=str(error),
                    )
                )
                continue

            additional_context.extend(result.additional_context)
            failures.extend(result.failures)

            if result.action in (HookAction.MODIFY, HookAction.RETRY):
                current_payload = dict(result.updated_payload or {})

            if result.action in (HookAction.BLOCK, HookAction.RETRY):
                return HookResult(
                    action=result.action,
                    reason=result.reason,
                    updated_payload=current_payload,
                    additional_context=additional_context,
                    failures=failures,
                )

        return HookResult(
            action=HookAction.CONTINUE,
            updated_payload=current_payload,
            additional_context=additional_context,
            failures=failures,
        )

    @staticmethod
    async def _deliver(
        registration: _Registration,
        event: HookEvent,
        current_payload: dict,
    ) -> HookResult:
        copied_payload, copied_metadata = await asyncio.to_thread(
            copy.deepcopy,
            (current_payload, event.metadata),
        )
        result = await registration.handler(
            HookEvent(
                event.name,
                event.session_id,
                copied_payload,
                copied_metadata,
            )
        )
        HookManager._validate_result(event.name, result)
        copied_payload = result.updated_payload
        if copied_payload is not None:
            copied_payload = await asyncio.to_thread(
                _copy_json_like, copied_payload
            )
        return HookResult(
            action=result.action,
            reason=result.reason,
            updated_payload=copied_payload,
            additional_context=list(result.additional_context),
            failures=list(result.failures),
        )

    @staticmethod
    def _validate_result(
        event_name: HookEventName, result: object
    ) -> None:
        if not isinstance(result, HookResult):
            raise HookProtocolError("handler must return HookResult")
        if not isinstance(result.action, HookAction):
            raise HookProtocolError("result action must be HookAction")
        if result.action not in ALLOWED_HOOK_ACTIONS[event_name]:
            raise HookProtocolError(
                f"{result.action.value} is not allowed for {event_name}"
            )
        if result.action is HookAction.BLOCK and (
            not isinstance(result.reason, str) or not result.reason.strip()
        ):
            raise HookProtocolError("block requires a nonempty reason")
        if result.action in (HookAction.MODIFY, HookAction.RETRY) and (
            result.updated_payload is None
        ):
            raise HookProtocolError(
                f"{result.action.value} requires updated_payload"
            )
        if result.updated_payload is not None and not isinstance(
            result.updated_payload, dict
        ):
            raise HookProtocolError("updated_payload must be a dict")
        if not isinstance(result.additional_context, list) or not all(
            isinstance(item, str) for item in result.additional_context
        ):
            raise HookProtocolError(
                "additional_context must be a list of strings"
            )
        if not isinstance(result.failures, list) or not all(
            isinstance(item, HookFailure) for item in result.failures
        ):
            raise HookProtocolError(
                "failures must be a list of HookFailure entries"
            )
