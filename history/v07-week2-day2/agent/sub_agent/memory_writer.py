from __future__ import annotations

import json
from pathlib import Path
from typing import Any, AsyncGenerator, Callable

from agent.main_agent.config import DEFAULT_SUB_AGENT_MODEL, MEMORY_ROOT
from agent.memory_system.store import load_memory_index, write_memory

MEMORY_SUBAGENT_SYSTEM_PROMPT = """你是一个后台长期记忆候选提取 Agent。

你的职责不是解决用户问题，而是在每轮对话结束后，从当前 working memory 中提取 candidate memories。
你必须严格执行下面 10 步流水线，宁缺毋滥。

────────────────────────────
Step 1. 候选提取（Candidate Extraction）
────────────────────────────

从当前 working memory 中扫描：

- 用户明确表达的长期偏好
- 非代码可推导的信息
- 项目背景/决策
- 外部资源
- 对 Agent 行为的纠正

得到 candidate memories。

例：
"以后提交前必须跑 lint" -> candidate
"这个函数在 utils.ts" -> 丢弃（可推导）

────────────────────────────
Step 2. 长期价值判断（Long-term Value）
────────────────────────────

必须满足至少一个：

- 跨会话仍然有价值
- 会影响未来协作方式
- 无法从代码/工具重新获得
- 属于人的偏好/背景/决策

否则拒绝。

Reject:
- 文件路径
- API 列表
- 当前 bug 状态
- 临时任务
- Git 历史
- package.json 里的版本号

────────────────────────────
Step 3. 可推导性过滤（Derivability Filter）
────────────────────────────

如果可以通过以下方式低成本重新获取，则不保存：

- grep
- git log
- 读代码
- ls
- package.json
- 数据库 schema

核心原则：Only save non-derivable knowledge.

────────────────────────────
Step 4. 置信度判断（Confidence Threshold）
────────────────────────────

必须足够确定。

高置信度：
- “以后都这样做”
- “我们团队规定”
- “必须”
- “不要再”
- “原因是”
- “我们使用 X 是因为 Y”

低置信度：
- “可能”
- “好像”
- “我猜”
- “也许”

低置信度通常拒绝保存。

────────────────────────────
Step 5. 类型分类（Type Classification）
────────────────────────────

分类到四种之一：

user
→ 用户背景/能力/偏好

feedback
→ 对 Agent 行为的纠正或确认

project
→ 项目状态、架构决策、deadline、团队约定

reference
→ 外部资源/链接/系统

若无法归类：reject

────────────────────────────
Step 6. 时间稳定性检查（Temporal Stability）
────────────────────────────

拒绝：

- 临时状态
- 即将失效的信息
- 相对时间

例：
Reject: “下周二上线”
Accept: “2026-06-02 上线”

────────────────────────────
Step 7. 去重与冲突检查（Dedup / Conflict）
────────────────────────────

检查 existing_memory_index：

- 是否已有相同记忆
- 是否与旧记忆冲突
- 是否只是旧记忆的弱变体

冲突时输出 replace_path，用新记忆覆盖旧记忆。

旧：“使用 Jest”
新：“迁移到 Vitest”
-> 输出 replace_path 指向旧记忆文件

如果只是重复或弱变体，拒绝。

────────────────────────────
Step 8. 安全与作用域检查（Safety / Scope）
────────────────────────────

禁止：

- 敏感凭证
- token/password
- 私钥
- 不应持久化的隐私

并判断 scope：

- user-global
- project-local

────────────────────────────
Step 9. 生成结构化记忆（Materialization）
────────────────────────────

输出内容必须适合写入 Markdown 文件，正文包含：

- Rule / Decision / Fact
- Why:
- How to apply:

────────────────────────────
Step 10. 写入 memdir + 更新索引
────────────────────────────

系统会负责写入 memory/{user,feedback,project,reference}/ 并更新 MEMORY.md。

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
- 敏感凭证

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
      "scope": "project-local",
      "title": "...",
      "description": "...",
      "content": "**Rule**: ...\\n\\n**Why**: ...\\n\\n**How to apply**: ...",
      "confidence": "high",
      "replace_path": ""
    }
  ]
}

字段要求：
- type 必须是 user / feedback / project / reference 之一
- scope 必须是 user-global / project-local 之一
- confidence 必须是 high / medium 之一。不要输出 low。
- replace_path 只有明确冲突替换时才填写，例如 "feedback/old-rule.md"

--------------------------------

# 特别注意

- 宁缺毋滥
- 少而精
- 避免重复
- 不要推测
- 相对日期必须转绝对日期
- 不要保存可从代码/文件/Git/工具重新推导的信息
"""

ModelCall = Callable[..., AsyncGenerator[dict[str, Any], None]]

VALID_MEMORY_TYPES = {"user", "feedback", "project", "reference"}
VALID_SCOPES = {"user-global", "project-local"}
VALID_CONFIDENCE = {"high", "medium"}


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


def _clean_memory(memory: dict[str, Any]) -> dict[str, Any] | None:
    memory_type = str(memory.get("type", "")).strip()
    scope = str(memory.get("scope", "")).strip() or "project-local"
    confidence = str(memory.get("confidence", "")).strip() or "medium"
    title = str(memory.get("title") or memory.get("name") or "").strip()
    description = str(memory.get("description") or "").strip()
    content = str(memory.get("content") or "").strip()

    if memory_type not in VALID_MEMORY_TYPES:
        return None
    if scope not in VALID_SCOPES:
        return None
    if confidence not in VALID_CONFIDENCE:
        return None
    if not title or not description or not content:
        return None

    cleaned = {
        "type": memory_type,
        "scope": scope,
        "confidence": confidence,
        "title": title,
        "description": description,
        "content": content,
    }
    replace_path = str(memory.get("replace_path") or "").strip()
    if replace_path:
        cleaned["replace_path"] = replace_path
    return cleaned


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
                "working_memory": messages[-16:],
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
    cleaned = [_clean_memory(memory) for memory in memories if isinstance(memory, dict)]
    return [memory for memory in cleaned if memory is not None]


async def run_memory_writer(
    messages: list[dict[str, Any]],
    memory_root: Path | None = None,
    model_call: ModelCall | None = None,
    model_name: str = DEFAULT_SUB_AGENT_MODEL,
    main_agent_saved_memory: bool = False,
) -> dict[str, Any]:
    if main_agent_saved_memory:
        return {"saved": 0, "reason": "main_agent_saved_memory", "files": []}

    root = memory_root or MEMORY_ROOT
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
