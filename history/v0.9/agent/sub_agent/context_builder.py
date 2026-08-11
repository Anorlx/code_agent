from __future__ import annotations

import json
from typing import Any

from agent.main_agent.token_usage import estimate_tokens

MAX_RECENT_MESSAGES = 6
MAX_TOOL_RESULT_CHARS = 1_200
MAX_ASSISTANT_CHARS = 1_500


def _tool_name(tool_call: dict[str, Any]) -> str | None:
    return tool_call.get("name") or tool_call.get("function", {}).get("name")


def _tool_arguments(tool_call: dict[str, Any]) -> dict[str, Any]:
    raw_args = tool_call.get("arguments") or tool_call.get("function", {}).get("arguments") or {}
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args or "{}")
        except json.JSONDecodeError:
            return {"raw": raw_args}
        return parsed if isinstance(parsed, dict) else {}
    return raw_args if isinstance(raw_args, dict) else {}


def _compact_message(message: dict[str, Any]) -> dict[str, Any]:
    item = dict(message)
    content = str(item.get("content") or "")
    if item.get("role") == "tool" and len(content) > MAX_TOOL_RESULT_CHARS:
        item["content"] = content[:MAX_TOOL_RESULT_CHARS] + "\n[Tool result truncated for sub agent context]"
    elif item.get("role") == "assistant" and len(content) > MAX_ASSISTANT_CHARS:
        item["content"] = content[:MAX_ASSISTANT_CHARS] + "\n[Assistant message truncated for sub agent context]"
    return item


def _recent_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_compact_message(message) for message in messages[-MAX_RECENT_MESSAGES:]]


def _latest_summary(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        content = str(message.get("content") or "")
        if message.get("role") == "system" and (
            "CompactBoundaryMessage" in content or "Conversation summary" in content
        ):
            return content[:2_000]
    assistant_notes = [
        str(message.get("content") or "")
        for message in messages
        if message.get("role") == "assistant" and not message.get("tool_calls") and message.get("content")
    ]
    return assistant_notes[-1][:1_500] if assistant_notes else ""


def _relevant_files(tool_calls: list[dict[str, Any]]) -> list[str]:
    files: list[str] = []
    for tool_call in tool_calls:
        arguments = _tool_arguments(tool_call)
        for key in ("path", "cwd"):
            value = arguments.get(key)
            if isinstance(value, str) and value and value not in files:
                files.append(value)
    return files


def build_task_context(
    user_input: str,
    messages: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]] | None = None,
    memory_context: str | None = None,
) -> list[dict[str, Any]]:
    calls = tool_calls or []
    task_state = {
        "current_task": user_input,
        "tool_calls": [
            {
                "name": _tool_name(tool_call),
                "arguments": _tool_arguments(tool_call),
            }
            for tool_call in calls
        ],
        "relevant_files_from_tool_args": _relevant_files(calls),
        "relevant_summary": _latest_summary(messages),
        "memory_context_excerpt": (memory_context or "")[:1_500],
    }
    return [
        {
            "role": "system",
            "content": (
                "You are a short-lived sub agent. Use only this task-specific context. "
                "The main agent keeps the global history; you receive a local working set."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(task_state, ensure_ascii=False),
        },
        *_recent_messages(messages),
    ]


def task_context_report(context_messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "message_count": len(context_messages),
        "estimated_tokens": estimate_tokens(context_messages),
    }
