from __future__ import annotations

import json
import re
from typing import Any, AsyncGenerator, Callable

from agent.main_agent.config import DEFAULT_SUB_AGENT_MODEL
from agent.mcp.registry import select_mcp_tools_for_server
from agent.sub_agent.context_builder import build_task_context
from agent.tools.registry import tool_catalog_text


TOOL_SEARCH_SYSTEM_PROMPT = """你是 tool_search。
你的任务是根据用户问题、当前上下文和工具说明，选择本轮主 agent 可以暴露给模型的工具。
只返回 JSON，例如 {"tools":["read_file","calculator"]}。如果不需要工具，返回 {"tools":[]}。
"""

MATH_EXPRESSION_RE = re.compile(r"(^|[\s(])[-+]?\d+(\.\d+)?\s*([+\-*/%]|\*\*)\s*[-+]?\d+")


def _available_tool_summary(available_tools: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "name": name,
            "category": str(info.get("category", "")),
            "mcp_server": str(info.get("mcp_server", "")),
            "mcp_tool": str(info.get("mcp_tool", "")),
            "description": str(info.get("spec", {}).get("description", "")),
        }
        for name, info in available_tools.items()
    ]


def _forced_mcp_tools(user_input: str, available_tools: dict[str, dict[str, Any]]) -> list[str]:
    text = user_input.strip().lower()
    if not text.startswith("/@"):
        return []
    token = text.split(maxsplit=1)[0]
    server_name = token[2:].strip()
    if not server_name:
        return [
            name
            for name, info in available_tools.items()
            if info.get("category") == "MCP"
        ]
    selected = select_mcp_tools_for_server(server_name, available_tools)
    if selected:
        return selected
    normalized = server_name.replace("_", "-")
    return select_mcp_tools_for_server(normalized, available_tools)


def _looks_like_math_request(text: str) -> bool:
    return any(word in text for word in ["算", "计算", "平方", "math"]) or bool(MATH_EXPRESSION_RE.search(text))


def _keyword_fallback(user_input: str, available_tools: dict[str, dict[str, Any]]) -> list[str]:
    available_names = list(available_tools)
    text = user_input.lower()
    selected: list[str] = []
    forced = _forced_mcp_tools(user_input, available_tools)
    if forced:
        return [name for name in dict.fromkeys(forced) if name in available_names]
    if _looks_like_math_request(text):
        selected.append("calculator")
    if any(word in text for word in ["读", "看", "打开", "文件", "read"]):
        selected.extend(["read_file", "list_dir", "read_project_file", "ls_project"])
    if any(word in text for word in ["写", "保存", "创建", "write"]):
        selected.append("write_file")
    if any(word in text for word in ["删", "删除", "delete", "remove", "rm"]):
        selected.append("delete_file")
    if any(word in text for word in ["列出", "目录", "项目", "结构", "ls", "list"]):
        selected.extend(["list_dir", "ls_project"])
    if any(word in text for word in ["搜索", "查找", "grep", "rg", "find"]):
        selected.append("grep_project")
    if any(word in text for word in ["运行", "执行", "命令", "测试", "python", "pytest", "unittest"]):
        selected.append("run_command")
    if any(word in text for word in ["记住", "记忆", "长期保存", "以后都", "save memory"]):
        selected.append("save_memory")
    if any(word in text for word in ["删除记忆", "忘记", "不要记了", "forget memory", "delete memory"]):
        selected.append("delete_memory")
    if any(word in text for word in ["清理记忆", "过期记忆", "遗忘", "prune memory"]):
        selected.append("prune_memories")
    if any(word in text for word in ["snip", "裁剪", "清理上下文", "清空工具结果", "释放上下文"]):
        selected.append("snip_context")
    if any(word in text for word in ["时间", "几点", "time"]):
        selected.append("current_time")
    if any(word in text for word in ["高德", "地图", "amap", "地址", "路线", "地理编码", "经纬度", "坐标", "定位", "导航"]):
        for name in select_mcp_tools_for_server("amap-maps", available_tools):
            selected.append(name)
    if any(
        word in text
        for word in [
            "tavily",
            "联网",
            "网页",
            "web",
            "搜索网络",
            "网络搜索",
            "搜索网页",
            "新闻",
            "资料",
            "抓取",
            "爬取",
            "extract",
            "crawl",
        ]
    ):
        for name in select_mcp_tools_for_server("tavily", available_tools):
            selected.append(name)
    return [name for name in dict.fromkeys(selected) if name in available_names]




def _extract_json(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        return {}
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


async def select_tools(
    user_input: str,
    messages: list[dict[str, Any]],
    available_tools: dict[str, dict[str, Any]],
    model_call: Callable[..., AsyncGenerator[dict[str, Any], None]] | None = None,
    model_name: str = DEFAULT_SUB_AGENT_MODEL,
) -> list[str]:
    names = list(available_tools)
    forced = _forced_mcp_tools(user_input, available_tools)
    if forced:
        return [name for name in dict.fromkeys(forced) if name in names]
    if model_call is None:
        return _keyword_fallback(user_input, available_tools)

    prompt = {
        "role": "user",
        "content": json.dumps(
            {
                "user_input": user_input,
                "task_context": build_task_context(user_input, messages),
                "tool_catalog": tool_catalog_text(),
                "available_tools": _available_tool_summary(available_tools),
            },
            ensure_ascii=False,
        ),
    }
    content = ""
    try:
        async for event in model_call(
            messages=[prompt],
            system_prompt=TOOL_SEARCH_SYSTEM_PROMPT,
            tools=[],
            model_name=model_name,
        ):
            if event.get("type") == "assistant_delta":
                content += event.get("content", "")
    except Exception:
        return _keyword_fallback(user_input, available_tools)

    parsed = _extract_json(content)
    selected = parsed.get("tools", [])
    if not isinstance(selected, list):
        return []
    return [name for name in selected if isinstance(name, str) and name in available_tools]
