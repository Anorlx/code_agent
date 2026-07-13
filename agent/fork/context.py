from __future__ import annotations

import json
from typing import Any

FORK_STARTED_MARKER = "Fork started -- processing in background"


def fork_worker_prompt(shared_goal: str, task: dict[str, Any]) -> str:
    return (
        "你是 Fork worker。你继承了 Main Agent 的上下文，但只能完成当前独立子任务。\n"
        "禁止创建新的子 Agent、禁止调用 fork/coordinator 工具、禁止向用户提问。\n"
        "优先使用只读工具调查，完成后直接输出结构化结果。\n\n"
        f"共同目标：{shared_goal}\n\n"
        "当前子任务：\n"
        f"{json.dumps(task, ensure_ascii=False, indent=2)}\n\n"
        "输出格式：\n"
        "- status: completed/failed\n"
        "- key_findings: 关键发现\n"
        "- files_or_sources: 相关文件或来源\n"
        "- risks: 风险与不确定点\n"
        "- result: 面向 Main Agent 的简明结论"
    )


def cache_safe_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    safe_messages: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "tool":
            item = dict(message)
            item["content"] = FORK_STARTED_MARKER
            safe_messages.append(item)
        else:
            safe_messages.append(dict(message))
    return safe_messages
