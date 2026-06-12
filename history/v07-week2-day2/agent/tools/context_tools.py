from __future__ import annotations

from typing import Any


async def snip_context(arguments: dict[str, Any]) -> dict[str, Any]:
    tool_call_ids = arguments.get("tool_call_ids") or []
    tool_names = arguments.get("tool_names") or []
    if not isinstance(tool_call_ids, list):
        tool_call_ids = []
    if not isinstance(tool_names, list):
        tool_names = []
    return {
        "ok": True,
        "content": "Snip request recorded. Matching old tool results will be cleared from context.",
        "tool_call_ids": [str(item) for item in tool_call_ids],
        "tool_names": [str(item) for item in tool_names],
    }


def snip_context_spec() -> dict[str, Any]:
    return {
        "name": "snip_context",
        "description": "请求清理旧工具结果内容以释放上下文空间。不会删除消息，只会把匹配工具结果替换为清理标记。",
        "parameters": {
            "type": "object",
            "properties": {
                "tool_call_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要清理的 tool_call_id 列表。为空时可按 tool_names 清理。",
                },
                "tool_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要清理的工具名列表，例如 read_project_file、grep_project。",
                },
            },
        },
    }
