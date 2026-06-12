from __future__ import annotations

import json
from typing import Any, AsyncGenerator, Callable

from agent.main_agent.config import DEFAULT_SUB_AGENT_MODEL

SESSION_SUMMARY_SYSTEM_PROMPT = """你是 session_summarizer。
你的任务是把一个会话压缩成启动时可选择的简短简介。
只返回 JSON，不要解释。

格式：
{
  "title": "不超过 16 个中文字的标题",
  "summary": "一句话说明这个会话主要做了什么，不超过 45 个中文字"
}

要求：
- 不保存敏感信息、密钥、完整路径或大段代码
- 只描述会话目标和进展
- 可以参考 MEMORY.md 索引，但不要把长期记忆正文复制进摘要
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


def fallback_session_summary(messages: list[dict[str, Any]]) -> dict[str, str]:
    user_messages = [str(message.get("content") or "") for message in messages if message.get("role") == "user"]
    last_user = user_messages[-1].strip() if user_messages else "新会话"
    compact = " ".join(last_user.split())
    title = compact[:16] or "新会话"
    summary = f"最近在讨论：{compact[:38]}" if compact else "还没有明确主题。"
    return {"title": title, "summary": summary}


async def summarize_session(
    messages: list[dict[str, Any]],
    memory_index: str,
    model_call: ModelCall | None = None,
    model_name: str = DEFAULT_SUB_AGENT_MODEL,
) -> dict[str, str]:
    if not messages:
        return {"title": "新会话", "summary": "还没有开始对话。"}
    if model_call is None:
        return fallback_session_summary(messages)

    prompt = {
        "role": "user",
        "content": json.dumps(
            {
                "recent_conversation": messages[-16:],
                "memory_index": memory_index,
            },
            ensure_ascii=False,
        ),
    }
    content = ""
    try:
        async for event in model_call(
            messages=[prompt],
            system_prompt=SESSION_SUMMARY_SYSTEM_PROMPT,
            tools=[],
            model_name=model_name,
        ):
            if event.get("type") == "assistant_delta":
                content += event.get("content", "")
    except Exception:
        return fallback_session_summary(messages)

    parsed = _extract_json(content)
    title = str(parsed.get("title") or "").strip()
    summary = str(parsed.get("summary") or "").strip()
    if not title or not summary:
        return fallback_session_summary(messages)
    return {"title": title[:32], "summary": summary[:120]}
