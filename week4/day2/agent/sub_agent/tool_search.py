from __future__ import annotations

import json
import re
from typing import Any, AsyncGenerator, Callable

from agent.main_agent.config import DEFAULT_SUB_AGENT_MODEL
from agent.tools.mcp.registry import select_mcp_tools_for_server
from agent.sub_agent.context_builder import build_task_context
from agent.tools.registry import tool_catalog_text
from agent.tools.skills.registry import resolve_skill_slash


TOOL_SEARCH_SYSTEM_PROMPT = """你是 tool_search。
你的任务是根据用户请求、最近上下文和工具目录，选择本轮主 Agent 可以暴露给模型的普通工具。

注意：
你不负责选择 fork_tasks 或 coordinator_plan。
如果任务需要 fork 或 coordinator，应该由 Mode Router 决定。
你这里只处理普通工具、MCP 工具和 Skills 工具。

你会收到：
- user_input：用户当前问题
- task_context：当前任务上下文摘要
- tool_catalog：工具说明
- available_tools：当前可用工具列表

选择原则：
1. 如果用户只是普通聊天、解释概念、给建议，不需要工具：返回空数组。
2. 如果用户需要读项目文件：选择 read_project_file；如果要一次读多个文件，选择 read_many_files。
3. 如果用户需要按文件名模式找文件：选择 glob_project。
4. 如果用户需要搜索代码内容：选择 grep_project。
5. 如果用户需要查看目录结构：选择 ls_project。
6. 如果用户需要查看文件大小、是否存在、修改时间：选择 file_info。
7. 如果用户需要小范围精确修改项目文件：选择 edit_file。
8. 如果用户需要读写当前项目文件：选择 read_file、write_file、list_dir、delete_file 或 edit_file。
9. 如果用户需要运行本地命令、测试、脚本：选择 run_command。
10. 如果用户需要计算：选择 calculator。
11. 如果用户需要长任务计划或进度跟踪：选择 todo_write。
12. 如果用户要求保存长期偏好、项目长期事实、协作规则：选择 save_memory。
13. 如果用户要求删除或忘记记忆：选择 delete_memory。
14. 如果用户要求清理过期记忆：选择 prune_memories。
15. 如果用户要求清理旧工具结果、释放上下文：选择 snip_context。
16. 如果用户需要地图、地址、路线、经纬度：选择 amap 相关 MCP 工具。
17. 如果用户需要联网搜索、网页抓取、新闻、外部资料：选择 tavily 相关 MCP 工具。
18. 如果用户显式输入 /verify、/debug、/simplify 等技能命令，选择对应 skill_* 工具。
19. 如果用户需要验证、调试、简化、批量处理、生成占位文案或把 prompt 变成技能，选择相关 skill_* 工具。

约束：
- 只选择当前任务真正需要的工具。
- 不要为了保险暴露所有工具。
- 不要选择不存在于 available_tools 里的工具。
- 如果可以用一个工具解决，不要选多个。
- 如果路径未知，可以选择搜索/列目录工具辅助定位。
- 对可能产生副作用的工具要谨慎，但是否允许执行由权限系统决定。

你必须只输出 JSON，不要输出解释。

输出格式：
{
  "tools": ["tool_name_1", "tool_name_2"],
  "reason": "一句话说明选择原因"
}
"""

MATH_EXPRESSION_RE = re.compile(r"(^|[\s(])[-+]?\d+(\.\d+)?\s*([+\-*/%]|\*\*)\s*[-+]?\d+")
LIST_TOKEN_RE = re.compile(r"(?<![a-zA-Z])(?:ls|list)(?![a-zA-Z])")
TIME_TOKEN_RE = re.compile(r"(?<![a-zA-Z])time(?![a-zA-Z])")


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


def _forced_skill_tool(user_input: str, available_tools: dict[str, dict[str, Any]]) -> list[str]:
    text = user_input.strip()
    if not text.startswith("/") or text.startswith("/@"):
        return []
    command = text.split(maxsplit=1)[0]
    skill = resolve_skill_slash(command)
    if skill is None:
        return []
    return [skill.tool_name] if skill.tool_name in available_tools else []


def _append_skill(selected: list[str], skill_slug: str, available_tools: dict[str, dict[str, Any]]) -> None:
    tool_name = "skill_" + skill_slug.replace("-", "_")
    if tool_name in available_tools:
        selected.append(tool_name)


def _looks_like_math_request(text: str) -> bool:
    return any(word in text for word in ["算", "计算", "平方", "math"]) or bool(MATH_EXPRESSION_RE.search(text))


def _keyword_fallback(user_input: str, available_tools: dict[str, dict[str, Any]]) -> list[str]:
    available_names = list(available_tools)
    text = user_input.lower()
    selected: list[str] = []
    forced = _forced_mcp_tools(user_input, available_tools)
    if forced:
        return [name for name in dict.fromkeys(forced) if name in available_names]
    forced_skill = _forced_skill_tool(user_input, available_tools)
    if forced_skill:
        return forced_skill
    if _looks_like_math_request(text):
        selected.append("calculator")
    if any(word in text for word in ["读", "看", "打开", "文件", "read"]):
        selected.extend(["read_file", "list_dir", "read_project_file", "ls_project"])
    if any(word in text for word in ["批量读取", "多个文件", "多文件", "read many", "many files"]):
        selected.append("read_many_files")
    if any(word in text for word in ["写", "保存", "创建", "write"]):
        selected.append("write_file")
    if any(word in text for word in ["修改", "替换", "编辑", "精确替换", "edit", "replace"]):
        selected.append("edit_file")
    if any(word in text for word in ["删", "删除", "delete", "remove", "rm"]):
        selected.append("delete_file")
    if (
        any(word in text for word in ["列出", "目录", "项目结构", "项目目录", "根目录", "文件夹", "有哪些文件"])
        or LIST_TOKEN_RE.search(text)
    ):
        selected.extend(["list_dir", "ls_project"])
    if any(word in text for word in ["glob", "通配符", "文件名模式", "**/*.py", "*.py"]):
        selected.append("glob_project")
    if any(word in text for word in ["搜索", "查找", "grep", "rg", "find"]):
        selected.append("grep_project")
    if any(word in text for word in ["是否存在", "文件信息", "文件大小", "修改时间", "元信息", "stat"]):
        selected.append("file_info")
    if (
        any(word in text for word in ["分析", "审查", "检查", "查看", "看一下"])
        and any(word in text for word in ["模块", "代码", "项目", "文件"])
    ):
        selected.extend(["ls_project", "glob_project", "read_project_file", "read_many_files", "grep_project"])
    if any(word in text for word in ["运行", "执行", "命令", "测试", "python", "pytest", "unittest"]):
        selected.append("run_command")
    if any(word in text for word in ["验证", "verify", "本地检查", "提交前", "ci 前", "ci前"]):
        _append_skill(selected, "verify", available_tools)
    if any(word in text for word in ["调试", "debug", "报错", "错误栈", "根因", "诊断"]):
        _append_skill(selected, "debug", available_tools)
    if any(word in text for word in ["简化", "simplify", "降低复杂度", "重复代码", "重构审查"]):
        _append_skill(selected, "simplify", available_tools)
    if any(word in text for word in ["skillify", "变成技能", "生成技能", "复用 prompt", "模板化"]):
        _append_skill(selected, "skillify", available_tools)
    if any(word in text for word in ["快捷键", "keybinding", "keybindings", "按键绑定"]):
        _append_skill(selected, "keybindings-help", available_tools)
    if any(word in text for word in ["配置", "settings.json", "默认模型", "权限规则", "环境变量"]):
        _append_skill(selected, "update-config", available_tools)
    if any(word in text for word in ["批量处理", "批量重命名", "批量格式化", "batch", "bulk"]):
        _append_skill(selected, "batch", available_tools)
    if any(word in text for word in ["卡住", "走出困境", "stuck", "循环"]):
        _append_skill(selected, "stuck", available_tools)
    if any(word in text for word in ["占位", "lorem", "ipsum", "假文案", "placeholder"]):
        _append_skill(selected, "lorem-ipsum", available_tools)
    if any(word in text for word in ["记住", "记忆", "长期保存", "以后都", "save memory"]):
        selected.append("save_memory")
    if any(word in text for word in ["删除记忆", "忘记", "不要记了", "forget memory", "delete memory"]):
        selected.append("delete_memory")
    if any(word in text for word in ["清理记忆", "过期记忆", "遗忘", "prune memory"]):
        selected.append("prune_memories")
    if any(word in text for word in ["snip", "裁剪", "清理上下文", "清空工具结果", "释放上下文"]):
        selected.append("snip_context")
    if any(word in text for word in ["todo", "待办", "计划", "进度", "任务列表", "任务规划"]):
        selected.append("todo_write")
    if any(word in text for word in ["时间", "几点"]) or TIME_TOKEN_RE.search(text):
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
    forced_skill = _forced_skill_tool(user_input, available_tools)
    if forced_skill:
        return forced_skill
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
    return [
        name
        for name in selected
        if isinstance(name, str)
        and name in available_tools
        and name not in {"fork_tasks", "coordinator_plan"}
    ]
