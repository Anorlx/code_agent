from __future__ import annotations

import json
from pathlib import Path
from typing import Any, AsyncGenerator, Callable

from agent.main_agent.config import DEFAULT_SUB_AGENT_MODEL, MEMORY_ROOT
from agent.memory_system.store import load_memory_index, write_memory

MEMORY_SUBAGENT_SYSTEM_PROMPT = """你是一个长期记忆提取 Agent。

你的职责不是解决用户问题，
而是从对话中提取：

- 用户长期偏好
- 项目关键决策
- 已验证的工作方式
- 外部系统引用

你必须严格过滤低价值信息。

--------------------------------

# 允许保存的记忆类型

1. user
用户身份、背景、长期偏好

2. feedback
用户对 Agent 行为的纠正或确认

3. project
项目中的长期决策、约束、状态

4. reference
外部系统、链接、仪表盘、文档

--------------------------------

# 禁止保存

不要保存：

- 文件结构
- API 列表
- 临时任务
- Git 历史
- 可从代码推导的信息
- Debug 过程
- 一次性对话

--------------------------------

# 判断标准

只有满足以下条件才允许保存：

- 跨会话仍然有价值
- 无法从代码重新推导
- 会改变未来 Agent 行为
- 对用户长期协作有帮助

--------------------------------

# 输出格式

如果没有值得保存的信息：

{
  "memories": []
}

如果有：

{
  "memories": [
    {
      "type": "feedback",
      "title": "...",
      "description": "...",
      "content": "..."
    }
  ]
}

--------------------------------

# 特别注意

- 宁缺毋滥
- 少而精
- 避免重复
- 不要推测
- 相对日期必须转绝对日期
"""

ModelCall = Callable[..., AsyncGenerator[dict[str, Any], None]]


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


async def extract_memories(
    messages: list[dict[str, Any]],
    memory_index: str,
    model_call: ModelCall | None = None,
    model_name: str = DEFAULT_SUB_AGENT_MODEL,
) -> list[dict[str, Any]]:
    if model_call is None:
        return []

    prompt = {
        "role": "user",
        "content": json.dumps(
            {
                "recent_conversation": messages[-12:],
                "existing_memory_index": memory_index,
            },
            ensure_ascii=False,
        ),
    }
    content = ""
    try:
        async for event in model_call(
            messages=[prompt],
            system_prompt=MEMORY_SUBAGENT_SYSTEM_PROMPT,
            tools=[],
            model_name=model_name,
        ):
            if event.get("type") == "assistant_delta":
                content += event.get("content", "")
    except Exception:
        return []

    parsed = _extract_json(content)
    memories = parsed.get("memories", [])
    if not isinstance(memories, list):
        return []
    return [memory for memory in memories if isinstance(memory, dict)]


async def run_memory_writer(
    messages: list[dict[str, Any]],
    memory_root: Path | None = None,
    model_call: ModelCall | None = None,
    model_name: str = DEFAULT_SUB_AGENT_MODEL,
    should_run: bool = True,
    main_agent_saved_memory: bool = False,
) -> dict[str, Any]:
    root = memory_root or MEMORY_ROOT
    if main_agent_saved_memory:
        return {"saved": 0, "reason": "main_agent_saved_memory", "files": []}
    if not should_run:
        return {"saved": 0, "reason": "throttled", "files": []}

    index = load_memory_index(root)
    memories = await extract_memories(
        messages=messages,
        memory_index=index,
        model_call=model_call,
        model_name=model_name,
    )
    files = []
    for memory in memories:
        try:
            files.append(write_memory(memory, root).relative_to(root).as_posix())
        except ValueError:
            continue
    return {"saved": len(files), "reason": "completed", "files": files}
