from __future__ import annotations

from typing import Any

from agent.main_agent.config import MEMORY_ROOT
from agent.memory_system.store import write_memory


async def save_memory(arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        path = write_memory(arguments, MEMORY_ROOT)
        return {
            "ok": True,
            "content": f"Saved memory: {path.relative_to(MEMORY_ROOT).as_posix()}",
            "path": path.relative_to(MEMORY_ROOT).as_posix(),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def save_memory_spec() -> dict[str, Any]:
    return {
        "name": "save_memory",
        "description": "保存一条长期记忆到 memory 目录。只保存无法从代码/Git/文件重新推导、跨会话仍有价值的信息。",
        "parameters": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "description": "记忆类型，只能是 user、feedback、project、reference。",
                    "enum": ["user", "feedback", "project", "reference"],
                },
                "title": {"type": "string", "description": "记忆标题。"},
                "description": {"type": "string", "description": "一句话摘要，用于 MEMORY.md 索引。"},
                "content": {"type": "string", "description": "Markdown 正文，说明 Rule/Why/How to apply 等。"},
            },
            "required": ["type", "title", "description", "content"],
        },
    }
