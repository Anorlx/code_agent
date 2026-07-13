from __future__ import annotations

import json
from typing import Any


async def tool_search(arguments: dict[str, Any], runtime_context: dict[str, Any]) -> dict[str, Any]:
    query = str(arguments.get("query") or runtime_context.get("user_input") or "").strip()
    if not query:
        return {"ok": False, "error": "Missing query."}

    from agent.sub_agent.tool_search import select_tools

    tools = runtime_context.get("tools") or {}
    if not isinstance(tools, dict):
        return {"ok": False, "error": "Runtime tools registry is missing."}

    selected = await select_tools(
        user_input=query,
        messages=list(runtime_context.get("messages") or []),
        available_tools=tools,
        model_call=runtime_context.get("model_call"),
        model_name=str(runtime_context.get("subagent_model_name") or ""),
    )
    selected = [name for name in selected if name in tools and name != "tool_search"]
    summaries = [
        {
            "name": name,
            "category": str(tools[name].get("category", "")),
            "description": str(tools[name].get("spec", {}).get("description", "")),
            "exposure": str(tools[name].get("exposure", "lazy")),
        }
        for name in selected
    ]
    return {
        "ok": True,
        "selected_tools": selected,
        "tools": summaries,
        "content": json.dumps({"selected_tools": selected, "tools": summaries}, ensure_ascii=False),
    }


def tool_search_spec() -> dict[str, Any]:
    return {
        "name": "tool_search",
        "description": "根据当前任务从完整工具目录中发现需要延迟加载的工具。只返回工具名和摘要，不直接执行工具。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "当前任务或需要发现工具的能力描述，例如 联网搜索 LangGraph 文档。",
                }
            },
            "required": ["query"],
        },
    }
