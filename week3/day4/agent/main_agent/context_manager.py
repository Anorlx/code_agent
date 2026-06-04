from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Callable

from agent.main_agent.config import (
    AUTO_COMPACT_THRESHOLD_RATIO,
    COLLAPSE_BLOCK_SPAWN_RATIO,
    COLLAPSE_COMMIT_RATIO,
    CONTEXT_BLOCKING_MARGIN,
    CONTEXT_EFFECTIVE_LIMIT,
    CONTEXT_WARNING_MARGIN,
    MICRO_COMPACT_IDLE_SECONDS,
    MICRO_COMPACT_KEEP_RECENT,
)
from agent.main_agent.token_usage import estimate_tokens

CLEARED_TOOL_RESULT = "[Old tool result content cleared]"
COLLAPSED_MESSAGE = "[Older conversation content collapsed]"
COMPACT_SUMMARY_PROMPT = """你是上下文自动压缩 Agent。
你只能输出文本，不能调用任何工具，包括 Read、Bash、Grep、Glob、WebSearch、WebFetch、Edit、Write。
你最多运行一轮。你的任务是把完整对话压缩成可供后续 Agent 继续工作的摘要。

请输出两个 XML 块：

<analysis>
先梳理对话里的重要事实、任务目标、已完成事项、未完成事项、工具结果和约束。
</analysis>

<summary>
请按以下 9 个章节输出：
1. 当前目标
2. 用户长期偏好和明确要求
3. 项目事实和重要决策
4. 已完成工作
5. 未完成工作
6. 关键文件和代码位置
7. 工具调用结果中仍然重要的信息
8. 风险、限制和阻塞
9. 下一步建议
</summary>
"""

COMPACTABLE_TOOL_NAMES = {
    "read_file",
    "read_project_file",
    "grep_project",
    "ls_project",
    "list_dir",
    "run_command",
    "write_file",
    "delete_file",
    "save_memory",
}

ModelCall = Callable[..., AsyncGenerator[dict[str, Any], None]]


@dataclass
class ContextConfig:
    effective_limit: int = CONTEXT_EFFECTIVE_LIMIT
    warning_margin: int = CONTEXT_WARNING_MARGIN
    blocking_margin: int = CONTEXT_BLOCKING_MARGIN
    auto_compact_ratio: float = AUTO_COMPACT_THRESHOLD_RATIO
    collapse_commit_ratio: float = COLLAPSE_COMMIT_RATIO
    collapse_block_spawn_ratio: float = COLLAPSE_BLOCK_SPAWN_RATIO
    micro_compact_idle_seconds: int = MICRO_COMPACT_IDLE_SECONDS
    micro_compact_keep_recent: int = MICRO_COMPACT_KEEP_RECENT
    auto_compact_model_name: str = "qwen3.5-flash"


def now_timestamp() -> float:
    return time.time()


def token_warning_state(token_count: int, config: ContextConfig) -> dict[str, Any]:
    auto_threshold = int(config.effective_limit * config.auto_compact_ratio)
    collapse_threshold = int(config.effective_limit * config.collapse_commit_ratio)
    spawn_threshold = int(config.effective_limit * config.collapse_block_spawn_ratio)
    blocking_limit = config.effective_limit - config.blocking_margin
    warning_threshold = config.effective_limit - config.warning_margin
    percent_left = max(0.0, (config.effective_limit - token_count) / config.effective_limit)
    return {
        "token_count": token_count,
        "effective_limit": config.effective_limit,
        "percent_left": round(percent_left, 4),
        "isAboveWarningThreshold": token_count >= warning_threshold,
        "isAboveErrorThreshold": token_count >= warning_threshold,
        "isAboveAutoCompactThreshold": token_count >= auto_threshold,
        "isAboveCollapseCommitThreshold": token_count >= collapse_threshold,
        "isAboveCollapseSpawnThreshold": token_count >= spawn_threshold,
        "isAtBlockingLimit": token_count >= blocking_limit,
    }


def _message_text_tokens(message: dict[str, Any]) -> int:
    return estimate_tokens(message.get("content") or "")


def _tool_name(message: dict[str, Any]) -> str:
    return str(message.get("name") or message.get("tool_name") or "")


def is_compactable_tool_result(message: dict[str, Any]) -> bool:
    return message.get("role") == "tool" and _tool_name(message) in COMPACTABLE_TOOL_NAMES


def _is_cleared(message: dict[str, Any]) -> bool:
    return message.get("content") == CLEARED_TOOL_RESULT


def snip_tool_results(
    messages: list[dict[str, Any]],
    *,
    tool_call_ids: list[str] | None = None,
    tool_names: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    ids = set(tool_call_ids or [])
    names = set(tool_names or [])
    changed = []
    freed_tokens = 0
    new_messages = []

    for index, message in enumerate(messages):
        item = dict(message)
        should_clear = item.get("role") == "tool" and not _is_cleared(item)
        if ids:
            should_clear = should_clear and str(item.get("tool_call_id", "")) in ids
        if names:
            should_clear = should_clear and _tool_name(item) in names
        if not ids and not names:
            should_clear = should_clear and is_compactable_tool_result(item)

        if should_clear:
            before = _message_text_tokens(item)
            item["content"] = CLEARED_TOOL_RESULT
            after = _message_text_tokens(item)
            freed_tokens += max(0, before - after)
            changed.append(index)
        new_messages.append(item)

    return new_messages, {
        "level": "snip",
        "changed_indexes": changed,
        "freed_tokens": freed_tokens,
    }


def _last_assistant_timestamp(messages: list[dict[str, Any]]) -> float | None:
    for message in reversed(messages):
        if message.get("role") == "assistant" and message.get("created_at"):
            try:
                return float(message["created_at"])
            except (TypeError, ValueError):
                return None
    return None


def should_micro_compact(
    messages: list[dict[str, Any]],
    *,
    current_time: float,
    config: ContextConfig,
) -> bool:
    last_assistant_at = _last_assistant_timestamp(messages)
    if last_assistant_at is None:
        return False
    return current_time - last_assistant_at >= config.micro_compact_idle_seconds


def micro_compact(
    messages: list[dict[str, Any]],
    *,
    keep_recent: int = MICRO_COMPACT_KEEP_RECENT,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    keep_recent = max(1, keep_recent)
    compactable_indexes = [
        index
        for index, message in enumerate(messages)
        if is_compactable_tool_result(message) and not _is_cleared(message)
    ]
    keep = set(compactable_indexes[-keep_recent:])
    clear_ids = [
        str(messages[index].get("tool_call_id", ""))
        for index in compactable_indexes
        if index not in keep and messages[index].get("tool_call_id")
    ]
    new_messages, stats = snip_tool_results(messages, tool_call_ids=clear_ids)
    stats["level"] = "micro_compact"
    stats["kept_recent"] = keep_recent
    return new_messages, stats


def _logical_parent_uuid(messages: list[dict[str, Any]]) -> str | None:
    if not messages:
        return None
    last = messages[-1]
    for key in ("uuid", "id", "tool_call_id"):
        if last.get(key):
            return str(last[key])
    return None


def _boundary_message(
    level: str,
    token_count: int,
    message_count: int,
    logical_parent_uuid: str | None,
) -> dict[str, Any]:
    return {
        "role": "system",
        "type": "compact_boundary",
        "uuid": str(uuid.uuid4()),
        "logicalParentUuid": logical_parent_uuid,
        "level": level,
        "content": (
            f"[CompactBoundaryMessage level={level} "
            f"tokens_before={token_count} messages={message_count}]"
        ),
        "created_at": now_timestamp(),
    }


def collapse_context(
    messages: list[dict[str, Any]],
    *,
    token_count: int,
    keep_recent_messages: int = 8,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if len(messages) <= keep_recent_messages + 1:
        return messages, {"level": "collapse", "changed": False, "freed_tokens": 0}

    keep_recent_messages = max(2, keep_recent_messages)
    old_messages = messages[:-keep_recent_messages]
    recent_messages = messages[-keep_recent_messages:]
    old_token_count = estimate_tokens(old_messages)
    compact_note = {
        "role": "system",
        "type": "collapsed_context",
        "content": (
            f"{COLLAPSED_MESSAGE}\n"
            f"Collapsed {len(old_messages)} older messages. "
            "Important long-term facts should live in memory/*.md."
        ),
        "created_at": now_timestamp(),
    }
    new_messages = [
        _boundary_message("collapse", token_count, len(messages), _logical_parent_uuid(messages)),
        compact_note,
        *recent_messages,
    ]
    freed_tokens = max(0, old_token_count - estimate_tokens([compact_note]))
    return new_messages, {
        "level": "collapse",
        "changed": True,
        "collapsed_messages": len(old_messages),
        "freed_tokens": freed_tokens,
    }


def _extract_summary(text: str) -> str:
    start = text.find("<summary>")
    end = text.rfind("</summary>")
    if start >= 0 and end > start:
        return text[start + len("<summary>") : end].strip()
    return text.strip()


async def auto_compact(
    messages: list[dict[str, Any]],
    *,
    model_call: ModelCall | None,
    model_name: str,
    token_count: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    tail_user_message = messages[-1] if messages and messages[-1].get("role") == "user" else None
    if model_call is None:
        return collapse_context(messages, token_count=token_count)

    payload = json.dumps({"messages": messages}, ensure_ascii=False)
    content = ""
    try:
        async for event in model_call(
            messages=[{"role": "user", "content": payload}],
            system_prompt=COMPACT_SUMMARY_PROMPT,
            tools=[],
            model_name=model_name,
        ):
            if event.get("type") == "assistant_delta":
                content += event.get("content", "")
    except Exception as exc:
        collapsed, stats = collapse_context(messages, token_count=token_count)
        stats["level"] = "auto_compact_fallback_collapse"
        stats["error"] = str(exc)
        return collapsed, stats

    summary = _extract_summary(content)
    if not summary:
        collapsed, stats = collapse_context(messages, token_count=token_count)
        stats["level"] = "auto_compact_empty_fallback_collapse"
        return collapsed, stats

    new_messages = [
        _boundary_message("auto_compact", token_count, len(messages), _logical_parent_uuid(messages)),
        {
            "role": "assistant",
            "type": "compact_summary",
            "content": summary,
            "created_at": now_timestamp(),
        },
    ]
    if tail_user_message is not None:
        new_messages.append(tail_user_message)
    return new_messages, {
        "level": "auto_compact",
        "changed": True,
        "summary_tokens": estimate_tokens(summary),
        "compacted_messages": len(messages),
        "freed_tokens": max(0, token_count - estimate_tokens(new_messages)),
    }


async def manage_context(
    messages: list[dict[str, Any]],
    *,
    system_prompt: str,
    model_call: ModelCall | None,
    config: ContextConfig | None = None,
    current_time: float | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    config = config or ContextConfig()
    current_time = current_time or now_timestamp()
    actions: list[dict[str, Any]] = []
    working = list(messages)
    token_count = estimate_tokens(working) + estimate_tokens(system_prompt)

    if should_micro_compact(working, current_time=current_time, config=config):
        working, stats = micro_compact(working, keep_recent=config.micro_compact_keep_recent)
        actions.append(stats)
        token_count = estimate_tokens(working) + estimate_tokens(system_prompt)

    warning = token_warning_state(token_count, config)
    if warning["isAboveCollapseCommitThreshold"] and not warning["isAboveAutoCompactThreshold"]:
        working, stats = collapse_context(working, token_count=token_count)
        actions.append(stats)
        token_count = estimate_tokens(working) + estimate_tokens(system_prompt)
        warning = token_warning_state(token_count, config)

    if warning["isAboveAutoCompactThreshold"]:
        working, stats = await auto_compact(
            working,
            model_call=model_call,
            model_name=config.auto_compact_model_name,
            token_count=token_count,
        )
        actions.append(stats)
        token_count = estimate_tokens(working) + estimate_tokens(system_prompt)
        warning = token_warning_state(token_count, config)

    return working, {
        "actions": actions,
        "token_warning": warning,
        "token_count": token_count,
        "spawn_blocked": bool(warning["isAboveCollapseSpawnThreshold"]),
    }
