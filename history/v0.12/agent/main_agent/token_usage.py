from __future__ import annotations

import json
from typing import Any


def estimate_tokens(value: Any) -> int:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False)
    return max(1, len(text) // 4)


def build_token_snapshot(
    *,
    messages: list[dict[str, Any]],
    system_prompt: str = "",
    tools: list[dict[str, Any]] | None = None,
    blocking_token_limit: int,
    output_text: str = "",
) -> dict[str, int | str]:
    message_tokens = estimate_tokens(messages)
    system_tokens = estimate_tokens(system_prompt) if system_prompt else 0
    tool_tokens = estimate_tokens(tools or []) if tools else 0
    output_tokens = estimate_tokens(output_text) if output_text else 0
    context_tokens = message_tokens + system_tokens + tool_tokens
    return {
        "kind": "estimate",
        "message_tokens": message_tokens,
        "system_tokens": system_tokens,
        "tool_tokens": tool_tokens,
        "context_tokens": context_tokens,
        "output_tokens": output_tokens,
        "remaining_tokens": max(0, blocking_token_limit - context_tokens),
        "blocking_token_limit": blocking_token_limit,
    }


def build_real_usage_snapshot(
    usage: dict[str, Any],
    *,
    blocking_token_limit: int,
) -> dict[str, Any]:
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    total_tokens = usage.get("total_tokens")
    context_tokens = prompt_tokens if isinstance(prompt_tokens, int) else total_tokens
    remaining = (
        max(0, blocking_token_limit - context_tokens)
        if isinstance(context_tokens, int)
        else None
    )
    return {
        "kind": usage.get("kind", "model_usage"),
        "model": usage.get("model", ""),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "context_tokens": context_tokens,
        "output_tokens": completion_tokens,
        "remaining_tokens": remaining,
        "blocking_token_limit": blocking_token_limit,
    }
