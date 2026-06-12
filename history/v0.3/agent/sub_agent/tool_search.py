from __future__ import annotations

import json
from typing import Any, AsyncGenerator, Callable

from agent.main_agent.config import DEFAULT_SUB_AGENT_MODEL
from agent.tools.registry import tool_catalog_text


TOOL_SEARCH_SYSTEM_PROMPT = """你是 tool_search。
你的任务是根据用户问题、当前上下文和工具说明，选择本轮主 agent 可以暴露给模型的工具。
只返回 JSON，例如 {"tools":["read_file","calculator"]}。如果不需要工具，返回 {"tools":[]}。
"""


def _keyword_fallback(user_input: str, available_names: list[str]) -> list[str]:
    text = user_input.lower()
    selected: list[str] = []
    if any(word in text for word in ["算", "计算", "+", "-", "*", "/", "平方", "math"]):
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
    if any(word in text for word in ["时间", "几点", "time"]):
        selected.append("current_time")
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
    if model_call is None:
        return _keyword_fallback(user_input, names)

    prompt = {
        "role": "user",
        "content": json.dumps(
            {
                "user_input": user_input,
                "recent_messages": messages[-8:],
                "tool_catalog": tool_catalog_text(),
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
        return _keyword_fallback(user_input, names)

    parsed = _extract_json(content)
    selected = parsed.get("tools", [])
    if not isinstance(selected, list):
        return []
    return [name for name in selected if isinstance(name, str) and name in available_tools]
