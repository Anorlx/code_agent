"""Built-in lifecycle hook manager configuration."""

from .manager import HookManager


def create_default_hook_manager() -> HookManager:
    return HookManager()
