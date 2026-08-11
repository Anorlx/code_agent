from __future__ import annotations

from typing import Any

from agent.main_agent.config import MEMORY_ROOT
from agent.memory_system.advanced import PersonalMemorySystem
from agent.memory_system.store import delete_memory as delete_memory_file
from agent.memory_system.store import forget_stale_memories, write_memory


async def save_memory(arguments: dict[str, Any], runtime_context: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        path = write_memory(arguments, MEMORY_ROOT)
        runtime_context = runtime_context or {}
        card_id = PersonalMemorySystem().save_explicit(
            arguments,
            session_id=str(runtime_context.get("session_id") or "explicit"),
            messages=list(runtime_context.get("messages") or []),
        )
        return {
            "ok": True,
            "content": f"Saved memory card {card_id} and legacy note {path.relative_to(MEMORY_ROOT).as_posix()}",
            "path": path.relative_to(MEMORY_ROOT).as_posix(),
            "memory_id": card_id,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def delete_memory(arguments: dict[str, Any], runtime_context: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        memory_id = str(arguments.get("memory_id") or "").strip()
        if memory_id:
            deleted = PersonalMemorySystem().delete_memory(memory_id)
            if not deleted:
                return {"ok": False, "error": f"Memory card not found: {memory_id}"}
            return {"ok": True, "content": f"Marked memory card as deleted: {memory_id}", "memory_id": memory_id}
        path = delete_memory_file(str(arguments.get("path", "")), MEMORY_ROOT)
        return {
            "ok": True,
            "content": f"Deleted memory: {path.relative_to(MEMORY_ROOT).as_posix()}",
            "path": path.relative_to(MEMORY_ROOT).as_posix(),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def prune_memories(arguments: dict[str, Any], runtime_context: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        forgotten = forget_stale_memories(MEMORY_ROOT)
        card_result = PersonalMemorySystem().prune()
        return {
            "ok": True,
            "content": f"Forgot {len(forgotten)} legacy notes and expired {card_result['expired_cards']} memory cards.",
            "forgotten": forgotten,
            "cards": card_result,
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
                },
                "memory_id": {
                    "type": "string",
                    "description": "Advanced JSON Card 的 memory_id；填写后会版本化标记为 deleted。",
                },
            },
            "required": [],
        },
    }


async def search_user_memory(
    arguments: dict[str, Any],
    runtime_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    query = str(arguments.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "query is required"}
    runtime_context = runtime_context or {}
    result = PersonalMemorySystem().search(
        query,
        session_id=str(runtime_context.get("session_id") or "") or None,
        limit=max(1, min(12, int(arguments.get("limit") or 6))),
    )
    return {"ok": True, "content": result, **result}


async def list_memory_candidates(
    arguments: dict[str, Any],
    runtime_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidates = PersonalMemorySystem().list_candidates(int(arguments.get("limit") or 20))
    return {"ok": True, "content": candidates, "candidates": candidates}


async def confirm_memory_candidate(
    arguments: dict[str, Any],
    runtime_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidate_id = str(arguments.get("candidate_id") or "").strip()
    if not candidate_id:
        return {"ok": False, "error": "candidate_id is required"}
    approved = bool(arguments.get("approved"))
    confirmed = PersonalMemorySystem().confirm_candidate(candidate_id, approved)
    if not confirmed:
        return {"ok": False, "error": f"Confirmation candidate not found: {candidate_id}"}
    return {
        "ok": True,
        "content": f"Candidate {candidate_id} was {'accepted' if approved else 'rejected'}.",
        "candidate_id": candidate_id,
        "approved": approved,
    }


def list_memory_candidates_spec() -> dict[str, Any]:
    return {
        "name": "list_memory_candidates",
        "description": "列出需要用户确认的敏感或不确定候选记忆。",
        "parameters": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "最多返回多少条，默认 20。"}},
        },
    }


def confirm_memory_candidate_spec() -> dict[str, Any]:
    return {
        "name": "confirm_memory_candidate",
        "description": "接受或拒绝一条待确认候选记忆；接受后写入版本化 Advanced JSON Card。",
        "parameters": {
            "type": "object",
            "properties": {
                "candidate_id": {"type": "string", "description": "待确认候选的 ID。"},
                "approved": {"type": "boolean", "description": "是否接受该候选。"},
            },
            "required": ["candidate_id", "approved"],
        },
    }


def search_user_memory_spec() -> dict[str, Any]:
    return {
        "name": "search_user_memory",
        "description": "按当前任务检索个人长期 Cards 和带上下文的历史证据片段；需要证据时再调用，不要把所有历史塞进上下文。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要检索的事实、人物、事件或主题。"},
                "limit": {"type": "integer", "description": "每类最多返回多少条，默认 6，最大 12。"},
            },
            "required": ["query"],
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
