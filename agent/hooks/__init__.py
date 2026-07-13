"""Typed lifecycle hooks for the local coding agent."""

from .builtin import create_default_hook_manager
from .errors import HookProtocolError
from .manager import HookManager
from .types import (
    HookAction,
    HookEvent,
    HookEventName,
    HookFailure,
    HookHandler,
    HookResult,
)

__all__ = [
    "HookAction",
    "HookEvent",
    "HookEventName",
    "HookFailure",
    "HookHandler",
    "HookManager",
    "HookProtocolError",
    "HookResult",
    "create_default_hook_manager",
]
