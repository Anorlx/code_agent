"""Public types for agent lifecycle hooks."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Literal


HookEventName = Literal[
    "session.start",
    "session.end",
    "prompt.before",
    "tool.before",
    "tool.after",
    "tool.error",
    "context.before_compact",
    "agent.before_stop",
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

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", dict(self.payload))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def copied(self, payload: dict[str, Any]) -> HookEvent:
        """Return an event with independent nested payload and metadata."""
        return HookEvent(
            name=self.name,
            session_id=self.session_id,
            payload=deepcopy(payload),
            metadata=deepcopy(self.metadata),
        )


@dataclass(frozen=True)
class HookResult:
    action: HookAction = HookAction.CONTINUE
    reason: str | None = None
    updated_payload: dict[str, Any] | None = None
    additional_context: list[str] = field(default_factory=list)
    failures: list[HookFailure] = field(default_factory=list)


HookHandler = Callable[[HookEvent], Awaitable[HookResult]]
