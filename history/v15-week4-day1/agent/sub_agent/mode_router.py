from __future__ import annotations

import json
import re
from typing import Any, AsyncGenerator, Callable

from agent.main_agent.config import DEFAULT_SUB_AGENT_MODEL
from agent.sub_agent.context_builder import build_task_context

ModelCall = Callable[..., AsyncGenerator[dict[str, Any], None]]

MODE_ROUTER_SYSTEM_PROMPT = """你是 Agent Mode Router。

你的任务不是回答用户问题，而是判断当前用户请求应该交给哪种执行模式。

可选模式只有四种：

1. chat
适合普通对话、概念解释、代码解释、轻量建议。
不需要工具，不需要并行，不需要复杂规划。

2. tools
适合需要调用普通工具的任务。
例如读取文件、搜索代码、列目录、运行测试、计算、写文件、删除文件、调用 MCP。

3. fork
适合多个相互独立的调查任务。
典型场景：
- 并行分析多个模块
- 分别检查多个文件
- 从多个方向调查一个问题
- 比较多个方案
- 多个 worker 可以各自完成一部分，最后汇总

fork 的特点：
- 子任务彼此相对独立
- 主要是只读调查、搜索、分析
- 不适合直接写代码
- 不适合复杂多阶段规划

4. coordinator
适合复杂、多阶段、需要统筹的工程任务。
典型场景：
- 大型重构
- 复杂功能设计
- 先 research 再 synthesis
- 需要形成实施规格
- 需要拆解多个阶段
- 需要多个研究 worker 的结果被统一综合

coordinator 的特点：
- 有全局目标
- 有阶段顺序
- 需要综合多个调查结果
- 输出应该是实施计划、技术方案或规格说明
- 当前不直接修改代码

判断原则：
- 如果用户只是问概念，选择 chat。
- 如果用户只需要一次读取、搜索、运行命令，选择 tools。
- 如果用户要求多个独立方向并行调查，选择 fork。
- 如果用户要求复杂工程规划、多阶段研究和综合，选择 coordinator。
- 如果不确定，优先选择 tools，而不是 fork/coordinator。
- 不要因为用户提到“项目”“模块”“分析”就自动选择 fork。
- 只有当任务天然可以拆成多个独立 worker 时，才选择 fork。
- 只有当任务需要全局协调和阶段规划时，才选择 coordinator。

示例：
用户：你会些什么？
输出：
{"mode":"chat","confidence":0.95,"reason":"普通能力询问，不需要工具"}

用户：帮我搜索 CheckpointStore 在哪里定义
输出：
{"mode":"tools","confidence":0.9,"reason":"需要代码搜索工具"}

用户：并行分析 memory、tools、main_agent 三个模块的问题
输出：
{"mode":"fork","confidence":0.95,"reason":"多个模块可以由多个 worker 独立分析"}

用户：比较两种 checkpoint 实现方案的优缺点
输出：
{"mode":"fork","confidence":0.85,"reason":"方案比较适合并行调查后汇总"}

用户：帮我做一个复杂的工具系统重构，先研究现状再给实施计划
输出：
{"mode":"coordinator","confidence":0.95,"reason":"复杂多阶段工程任务，需要 research 和 synthesis"}

用户：看一下 agent/tools 目录有什么
输出：
{"mode":"tools","confidence":0.9,"reason":"只需要列目录工具"}

用户：解释一下 Runtime 是什么意思
输出：
{"mode":"chat","confidence":0.95,"reason":"概念解释，不需要工具"}

你必须只输出 JSON，不要输出解释。

输出格式：
{
  "mode": "chat | tools | fork | coordinator",
  "confidence": 0.0,
  "reason": "一句话说明为什么选择这个模式"
}
"""

FORK_TASK_BUILDER_SYSTEM_PROMPT = """你是 Fork Task Builder。

你的任务是把用户请求拆成多个可以并行执行的只读子任务。

Fork 模式适合：
- 多模块分析
- 多文件调查
- 多方向研究
- 多方案比较
- 多个相互独立的问题排查

你必须遵守：
1. 每个子任务必须相互独立。
2. 每个子任务应该可以由一个 worker 单独完成。
3. 子任务应尽量只读，不要求写文件。
4. 不要创建高度重复的任务。
5. 不要把一个线性流程拆成 fork。
6. 最多生成 4 个子任务，除非用户明确要求更多。
7. 每个任务都要有明确目标、调查范围和输出要求。

你会收到：
- user_input
- recent_context
- relevant_memory
- available_read_tools

请输出 JSON，不要输出解释。

输出格式：
{
  "shared_goal": "所有 fork worker 共同服务的总目标",
  "tasks": [
    {
      "id": "task-1",
      "title": "简短标题",
      "instruction": "详细说明这个 worker 要调查什么、可以看哪些范围、最终输出什么"
    }
  ],
  "merge_instruction": "最终如何汇总这些 worker 的结果"
}
"""

COORDINATOR_PLANNER_SYSTEM_PROMPT = """你是 Coordinator Planner。

你的任务是为复杂工程任务生成 Research + Synthesis 计划。

Coordinator 模式适合：
- 大型重构
- 多阶段工程改造
- 复杂功能设计
- 需要先研究现状，再形成实施规格
- 需要多个 research worker 分头调查，再统一综合

你必须遵守：
1. 不直接写代码。
2. 先规划 research tasks。
3. 每个 research task 应该是只读调查。
4. research task 之间可以并行。
5. synthesis 阶段负责把调查结果整理成实施规格。
6. 实施规格必须包含目标、约束、风险、步骤和验证方式。
7. 不要创建过多 worker，默认 3 到 4 个。
8. 如果任务不够复杂，应建议退回 tools 或 chat，而不是强行 coordinator。

你会收到：
- user_input
- recent_context
- project_summary
- relevant_memory

请只输出 JSON，不要输出解释。

输出格式：
{
  "task": "复杂工程任务目标",
  "research_tasks": [
    {
      "id": "research-1",
      "title": "研究任务标题",
      "instruction": "这个 worker 需要调查什么、关注哪些文件/模块/风险、输出什么"
    }
  ],
  "synthesis_requirements": [
    "需要综合哪些信息",
    "最终规格必须包含什么"
  ],
  "expected_output": "最终 coordinator 应该产出的内容类型"
}
"""

CHAT_HINTS = ("解释", "是什么意思", "是什么", "你会", "建议", "概念", "原理")
TOOL_HINTS = ("读", "看", "搜索", "查找", "列出", "运行", "测试", "写", "删除", "计算", "文件", "目录", "联网", "地图")
FORK_PATTERNS = [
    r"并行.*(分析|调查|搜索|检查|查看)",
    r"(分别|分头|同时).*(分析|调查|搜索|检查|查看|审查)",
    r"(多个|多份|多处|多方向|多模块|多文件|多子系统)",
    r"[二三四五六七八九十两0-9]+\s*个(模块|文件|方向|子系统|方案)",
    r"(方案|实现|架构).*(比较|对比)",
    r"(比较|对比).*(方案|实现|架构)",
    r"从.*(方向|角度).*(分析|调查|看|检查)",
]
COORDINATOR_PATTERNS = [
    r"(coordinator|协调器)",
    r"(复杂|大型).*(工程|任务|重构|改造|功能设计)",
    r"多阶段",
    r"(research\s*\+\s*synthesis|research.*synthesis)",
    r"(研究阶段|综合阶段|实施规格|实施方案|实施计划)",
    r"先.*(研究|调查|分析).*再.*(综合|制定|规划|生成)",
    r"(规划|拆解).*(多个阶段|多阶段|执行方案)",
]
LIST_ITEM_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_./-]*(?:\s+[A-Za-z_][A-Za-z0-9_./-]*)?")


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


async def _json_from_model(
    *,
    payload: dict[str, Any],
    system_prompt: str,
    model_call: ModelCall | None,
    model_name: str,
) -> dict[str, Any]:
    if model_call is None:
        return {}
    content = ""
    try:
        async for event in model_call(
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            system_prompt=system_prompt,
            tools=[],
            model_name=model_name,
        ):
            if event.get("type") == "assistant_delta":
                content += event.get("content", "")
    except Exception:
        return {}
    return _extract_json(content)


def _matches_any(patterns: list[str], text: str) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _fallback_route(user_input: str) -> dict[str, Any]:
    text = user_input.lower()
    if _matches_any(COORDINATOR_PATTERNS, text):
        return {"mode": "coordinator", "confidence": 0.85, "reason": "请求包含复杂多阶段工程规划或 research+synthesis 信号。"}
    if _matches_any(FORK_PATTERNS, text) or "fork" in text:
        return {"mode": "fork", "confidence": 0.82, "reason": "请求天然可以拆成多个独立调查任务。"}
    if any(hint in text for hint in TOOL_HINTS):
        return {"mode": "tools", "confidence": 0.75, "reason": "请求需要读取、搜索、运行命令或调用工具。"}
    if any(hint in text for hint in CHAT_HINTS):
        return {"mode": "chat", "confidence": 0.75, "reason": "请求是普通解释或对话。"}
    return {"mode": "tools", "confidence": 0.5, "reason": "不确定时优先选择 tools。"}


def _clean_list_item(item: str) -> str:
    cleaned = item.strip(" ，,、;；:：。.!?？（）()[]【】")
    prefixes = [
        "并行分析",
        "分别检查",
        "分别分析",
        "分头检查",
        "分头分析",
        "分析",
        "检查",
        "比较",
        "对比",
    ]
    for prefix in prefixes:
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
    suffixes = [
        "三个模块",
        "多个模块",
        "这些模块",
        "模块",
        "三个方向",
        "多个方向",
        "方向",
        "分别负责什么",
        "有什么问题",
    ]
    changed = True
    while changed:
        changed = False
        for suffix in suffixes:
            if cleaned.endswith(suffix):
                cleaned = cleaned[: -len(suffix)].strip()
                changed = True
    return cleaned.strip(" ，,、;；:：。.!?？（）()[]【】")


def _extract_list_items(text: str) -> list[str]:
    candidates: list[str] = []
    normalized = (
        text.replace("，", "、")
        .replace(",", "、")
        .replace("；", "、")
        .replace(";", "、")
        .replace(" 和 ", "、")
        .replace(" and ", "、")
    )
    for part in normalized.split("、"):
        item = _clean_list_item(part)
        if not item:
            continue
        if len(item) > 48:
            for match in LIST_ITEM_RE.findall(item):
                candidates.append(_clean_list_item(match))
            continue
        candidates.append(item)
    if len(candidates) < 2:
        candidates.extend(_clean_list_item(match) for match in LIST_ITEM_RE.findall(text))

    stopwords = {
        "fork",
        "coordinator",
        "research",
        "synthesis",
        "agent",
        "runtime",
        "Runtime",
    }
    output = []
    for item in candidates:
        if not item or item in stopwords:
            continue
        if item not in output:
            output.append(item)
    return output[:4]


def _fallback_fork_tasks(user_input: str) -> list[dict[str, str]]:
    items = _extract_list_items(user_input)
    if len(items) >= 2:
        return [
            {
                "id": f"task-{index}",
                "title": f"分析 {item}",
                "instruction": (
                    f"只读分析 {item} 的职责、关键文件、当前实现、风险和改进建议。"
                    "输出时请给出证据路径、关键发现、潜在问题和建议。"
                ),
            }
            for index, item in enumerate(items, start=1)
        ]
    return [
        {
            "id": "task-1",
            "title": "调查当前请求",
            "instruction": f"围绕用户请求进行只读调查，输出关键发现、证据和风险。用户请求：{user_input}",
        }
    ]


async def route_agent_mode(
    *,
    user_input: str,
    messages: list[dict[str, Any]],
    model_call: ModelCall | None = None,
    model_name: str = DEFAULT_SUB_AGENT_MODEL,
) -> dict[str, Any]:
    payload = {
        "user_input": user_input,
        "recent_context": build_task_context(user_input, messages)[-8:],
    }
    parsed = await _json_from_model(
        payload=payload,
        system_prompt=MODE_ROUTER_SYSTEM_PROMPT,
        model_call=model_call,
        model_name=model_name,
    )
    mode = str(parsed.get("mode") or "").strip().lower()
    if mode not in {"chat", "tools", "fork", "coordinator"}:
        return _fallback_route(user_input)
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "mode": mode,
        "confidence": max(0.0, min(1.0, confidence)),
        "reason": str(parsed.get("reason") or "Mode router selected this mode."),
    }


def _read_tools(available_tools: dict[str, dict[str, Any]]) -> list[str]:
    return [
        name
        for name, info in available_tools.items()
        if info.get("parallel_safe") and not info.get("side_effectful")
    ]


async def build_fork_task_arguments(
    *,
    user_input: str,
    messages: list[dict[str, Any]],
    available_tools: dict[str, dict[str, Any]],
    model_call: ModelCall | None = None,
    model_name: str = DEFAULT_SUB_AGENT_MODEL,
) -> dict[str, Any]:
    payload = {
        "user_input": user_input,
        "recent_context": build_task_context(user_input, messages)[-8:],
        "relevant_memory": "",
        "available_read_tools": _read_tools(available_tools),
    }
    parsed = await _json_from_model(
        payload=payload,
        system_prompt=FORK_TASK_BUILDER_SYSTEM_PROMPT,
        model_call=model_call,
        model_name=model_name,
    )
    tasks = parsed.get("tasks") if isinstance(parsed.get("tasks"), list) else []
    if not tasks:
        tasks = _fallback_fork_tasks(user_input)
    return {
        "shared_goal": str(parsed.get("shared_goal") or user_input),
        "tasks": tasks[:4],
        "merge_instruction": str(parsed.get("merge_instruction") or "汇总各 worker 的关键发现、证据、分歧和建议。"),
        "max_workers": min(max(len(tasks), 1), 4),
    }


async def build_coordinator_arguments(
    *,
    user_input: str,
    messages: list[dict[str, Any]],
    model_call: ModelCall | None = None,
    model_name: str = DEFAULT_SUB_AGENT_MODEL,
) -> dict[str, Any]:
    payload = {
        "user_input": user_input,
        "recent_context": build_task_context(user_input, messages)[-8:],
        "project_summary": "",
        "relevant_memory": "",
    }
    parsed = await _json_from_model(
        payload=payload,
        system_prompt=COORDINATOR_PLANNER_SYSTEM_PROMPT,
        model_call=model_call,
        model_name=model_name,
    )
    research_tasks = parsed.get("research_tasks") if isinstance(parsed.get("research_tasks"), list) else []
    if not research_tasks:
        research_tasks = [
            {
                "id": "research-1",
                "title": "研究当前架构",
                "instruction": (
                    "调查当前实现中与任务相关的模块、入口、状态流和工具流。"
                    f"任务：{user_input}"
                ),
            },
            {
                "id": "research-2",
                "title": "研究约束和风险",
                "instruction": (
                    "分析权限、上下文、checkpoint、MCP、记忆等可能受影响的约束。"
                    f"任务：{user_input}"
                ),
            },
            {
                "id": "research-3",
                "title": "研究验证方式",
                "instruction": (
                    "提出可以验证改动正确性的测试、命令和评测问题。"
                    f"任务：{user_input}"
                ),
            },
        ]
    return {
        "task": str(parsed.get("task") or user_input),
        "research_tasks": research_tasks[:4],
        "synthesis_requirements": parsed.get("synthesis_requirements") or [
            "目标",
            "约束",
            "风险",
            "实施步骤",
            "验证方式",
        ],
        "expected_output": str(parsed.get("expected_output") or "实施规格和技术方案"),
        "max_workers": min(max(len(research_tasks), 1), 4),
    }
