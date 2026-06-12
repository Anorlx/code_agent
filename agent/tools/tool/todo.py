from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent.main_agent.config import AGENT_DATA_ROOT


TODO_STATUSES = {"pending", "in_progress", "completed"}


def _todo_path(session_id: str | None = None) -> Path:
    safe_id = "".join(char for char in (session_id or "default") if char.isalnum() or char in {"-", "_"}) or "default"
    return AGENT_DATA_ROOT / "todos" / f"{safe_id}.json"


def _normalize_items(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError("items must be an array.")
    items: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("each todo item must be an object.")
        todo_id = str(item.get("id", "")).strip()
        content = str(item.get("content", "")).strip()
        status = str(item.get("status", "")).strip()
        if not todo_id or not content or not status:
            raise ValueError("each todo item requires id, content and status.")
        if status not in TODO_STATUSES:
            raise ValueError(f"invalid todo status: {status}")
        if todo_id in seen_ids:
            raise ValueError(f"duplicate todo id: {todo_id}")
        seen_ids.add(todo_id)
        items.append({"id": todo_id, "content": content, "status": status})
    return items


async def todo_write(arguments: dict[str, Any], runtime_context: dict[str, Any]) -> dict[str, Any]:
    try:
        items = _normalize_items(arguments.get("items"))
        path = _todo_path(str(runtime_context.get("session_id") or "default"))
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "session_id": runtime_context.get("session_id") or "default",
            "items": items,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        content = "\n".join(f"- [{item['status']}] {item['id']}: {item['content']}" for item in items)
        return {"ok": True, "path": path.as_posix(), "items": items, "content": content}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def todo_write_spec() -> dict[str, Any]:
    return {
        "name": "todo_write",
        "description": "写入或更新当前任务的 todo 列表，用于长任务规划和进度跟踪。",
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "content": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                            },
                        },
                        "required": ["id", "content", "status"],
                    },
                }
            },
            "required": ["items"],
        },
    }
