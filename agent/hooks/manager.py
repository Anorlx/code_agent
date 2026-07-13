"""Registration and dispatch for agent lifecycle hooks."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from itertools import count
from typing import Callable

from .errors import HookProtocolError
from .types import (
    HookAction,
    HookEvent,
    HookEventName,
    HookFailure,
    HookHandler,
    HookResult,
)


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
                result = await asyncio.wait_for(
                    registration.handler(event.copied(current_payload)),
                    timeout=registration.timeout,
                )
                self._validate_result(event.name, result)
            except TimeoutError:
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
    def _validate_result(
        event_name: HookEventName, result: object
    ) -> None:
        if not isinstance(result, HookResult):
            raise HookProtocolError("handler must return HookResult")
        if not isinstance(result.action, HookAction):
            raise HookProtocolError("result action must be HookAction")
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
        if result.action is HookAction.RETRY and event_name != "tool.error":
            raise HookProtocolError("retry is only valid for tool.error")
