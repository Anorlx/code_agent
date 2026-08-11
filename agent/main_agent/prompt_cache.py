from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from agent.main_agent.token_usage import estimate_tokens


def canonicalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): canonicalize(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [canonicalize(item) for item in value]
    if isinstance(value, tuple):
        return [canonicalize(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(canonicalize(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()[:16]


def normalize_message_for_prompt(message: dict[str, Any]) -> dict[str, Any]:
    item: dict[str, Any] = {
        "role": message.get("role"),
        "content": message.get("content") or "",
    }
    if message.get("role") == "assistant" and message.get("tool_calls"):
        item["tool_calls"] = deepcopy(message["tool_calls"])
    if message.get("role") == "tool":
        item["tool_call_id"] = message.get("tool_call_id") or message.get("name")
        item["name"] = message.get("name")
    return item


def normalize_messages_for_prompt(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [normalize_message_for_prompt(message) for message in messages]


def split_current_user_message(
    messages: list[dict[str, Any]],
    user_input: str,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if not messages:
        return [], None
    last = messages[-1]
    if last.get("role") == "user" and str(last.get("content") or "") == user_input:
        return list(messages[:-1]), dict(last)
    return list(messages), None


def dynamic_context_messages(memory_context: str | None = None) -> list[dict[str, Any]]:
    dynamic: list[dict[str, Any]] = []
    memory = str(memory_context or "").strip()
    if memory:
        dynamic.append(
            {
                "role": "system",
                "content": (
                    "<dynamic-memory-context>\n"
                    "These are task-relevant long-term memory hints. Treat them as leads, not facts; "
                    "verify against the current project before relying on them.\n\n"
                    f"{memory}\n"
                    "</dynamic-memory-context>"
                ),
            }
        )
    return dynamic


def _has_content_boundary(message: dict[str, Any]) -> bool:
    content = message.get("content")
    if isinstance(content, list):
        return any(isinstance(block, dict) and block.get("type") == "text" for block in content)
    return bool(str(content or "").strip())


def stable_history_for_request(stable_history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    request_history = [deepcopy(message) for message in stable_history]
    for index in range(len(request_history) - 1, -1, -1):
        if _has_content_boundary(request_history[index]):
            request_history[index]["cache_control"] = {"type": "ephemeral"}
            break
    return request_history


def request_messages_for_prompt(
    *,
    messages: list[dict[str, Any]],
    user_input: str,
    memory_context: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    stable_history, current_user = split_current_user_message(messages, user_input)
    dynamic = dynamic_context_messages(memory_context)
    request = stable_history_for_request(stable_history)
    request.extend(dynamic)
    if current_user is not None:
        request.append(current_user)
    return request, stable_history, dynamic


@dataclass
class PromptCacheEntry:
    system_fingerprint: str
    tools_fingerprint: str
    stable_messages: list[dict[str, Any]]
    stable_prefix_tokens: int


_PROMPT_CACHE_TRACKER: dict[str, PromptCacheEntry] = {}


def evaluate_prompt_cache(
    *,
    session_id: str | None,
    system_prompt: str,
    tools: list[dict[str, Any]],
    stable_history: list[dict[str, Any]],
    dynamic_messages: list[dict[str, Any]],
    selected_tools: list[str],
) -> dict[str, Any]:
    key = session_id or "default"
    normalized_history = normalize_messages_for_prompt(stable_history)
    system_fp = fingerprint(system_prompt)
    tools_fp = fingerprint(tools)
    stable_prefix = {
        "tools": tools,
        "system_prompt": system_prompt,
        "stable_history": normalized_history,
    }
    stable_prefix_tokens = estimate_tokens(stable_prefix)
    previous = _PROMPT_CACHE_TRACKER.get(key)
    prefix_append_only = False
    stable_cache_hit = False
    reused_prefix_tokens = 0
    if previous is not None:
        prefix_append_only = normalized_history[: len(previous.stable_messages)] == previous.stable_messages
        stable_cache_hit = (
            previous.system_fingerprint == system_fp
            and previous.tools_fingerprint == tools_fp
            and prefix_append_only
        )
        if stable_cache_hit:
            reused_prefix_tokens = previous.stable_prefix_tokens

    _PROMPT_CACHE_TRACKER[key] = PromptCacheEntry(
        system_fingerprint=system_fp,
        tools_fingerprint=tools_fp,
        stable_messages=normalized_history,
        stable_prefix_tokens=stable_prefix_tokens,
    )
    return {
        "kind": "prompt_prefix_cache",
        "session_id": key,
        "system_fingerprint": system_fp,
        "tools_fingerprint": tools_fp,
        "stable_prefix_fingerprint": fingerprint(stable_prefix),
        "stable_cache_hit": stable_cache_hit,
        "prefix_append_only": prefix_append_only,
        "stable_prefix_tokens": stable_prefix_tokens,
        "reused_prefix_tokens": reused_prefix_tokens,
        "stable_message_count": len(normalized_history),
        "dynamic_message_count": len(dynamic_messages),
        "tool_count": len(tools),
        "selected_tools": list(selected_tools),
    }
