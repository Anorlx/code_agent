from __future__ import annotations

from typing import Any

from agent.main_agent.config import MEMORY_ROOT
from agent.memory_system.store import delete_memory as delete_memory_file
from agent.memory_system.store import forget_stale_memories, write_memory


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


async def delete_memory(arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        path = delete_memory_file(str(arguments.get("path", "")), MEMORY_ROOT)
        return {
            "ok": True,
            "content": f"Deleted memory: {path.relative_to(MEMORY_ROOT).as_posix()}",
            "path": path.relative_to(MEMORY_ROOT).as_posix(),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def prune_memories(arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        forgotten = forget_stale_memories(MEMORY_ROOT)
        return {
            "ok": True,
            "content": f"Forgot {len(forgotten)} stale memories.",
            "forgotten": forgotten,
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
                "scope": {
                    "type": "string",
                    "description": "记忆作用域，user-global 或 project-local。",
                    "enum": ["user-global", "project-local"],
                },
                "confidence": {
                    "type": "string",
                    "description": "记忆置信度，high 或 medium。",
                    "enum": ["high", "medium"],
                },
                "ttl_days": {
                    "type": "integer",
                    "description": "可选 TTL 天数。为空时使用类型默认策略。",
                },
                "salience": {
                    "type": "number",
                    "description": "可选显著性分数，0 到 1。",
                },
                "replace_path": {
                    "type": "string",
                    "description": "如果新记忆覆盖旧记忆，填写旧记忆相对 memory/ 的路径。",
                },
            },
            "required": ["type", "title", "description", "content"],
        },
    }


def delete_memory_spec() -> dict[str, Any]:
    return {
        "name": "delete_memory",
        "description": "显式删除一条长期记忆。只能删除 memory/ 下的具体记忆 Markdown 文件，不能删除索引。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "相对 memory/ 的记忆文件路径，例如 feedback/pre-commit-lint.md。",
                }
            },
            "required": ["path"],
        },
    }


def prune_memories_spec() -> dict[str, Any]:
    return {
        "name": "prune_memories",
        "description": "根据 TTL、使用频率和显著性衰减清理过期或低价值长期记忆，并更新 MEMORY.md。",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    }
