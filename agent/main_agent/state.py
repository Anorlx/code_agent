from __future__ import annotations

from copy import deepcopy
import time
from typing import Any


def new_state(user_input: str, history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    messages = list(history or [])
    messages.append({"role": "user", "content": user_input, "created_at": time.time()})
    return {
        "turn": 0,
        "phase": "初始化",
        "messages": messages,
        "selected_tools": [],
        "termination_reason": None,
    }


def state_event(state: dict[str, Any], phase: str, **extra: Any) -> dict[str, Any]:
    snapshot = deepcopy(state)
    snapshot["phase"] = phase
    snapshot.update(extra)
    return {"type": "state", "phase": phase, "state": snapshot}


def terminal_event(state: dict[str, Any], reason: str, message: str) -> dict[str, Any]:
    snapshot = deepcopy(state)
    snapshot["termination_reason"] = reason
    return {
        "type": "terminal",
        "reason": reason,
        "message": message,
        "state": snapshot,
    }
