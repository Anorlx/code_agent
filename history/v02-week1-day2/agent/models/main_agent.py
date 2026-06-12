from __future__ import annotations

import json
import os
from typing import Any, AsyncGenerator

from agent.config import DASHSCOPE_COMPATIBLE_BASE_URL


def _normalize_messages(messages: list[dict[str, Any]], system_prompt: str) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    for message in messages:
        role = message.get("role")
        item: dict[str, Any] = {"role": role, "content": message.get("content") or ""}
        if role == "assistant" and message.get("tool_calls"):
            item["tool_calls"] = [_to_openai_tool_call(call) for call in message["tool_calls"]]
        if role == "tool":
            item["tool_call_id"] = message.get("tool_call_id") or message.get("name")
            item["name"] = message.get("name")
        normalized.append(item)
    return normalized


def _to_openai_tool_call(tool_call: dict[str, Any]) -> dict[str, Any]:
    name = tool_call.get("name") or tool_call.get("function", {}).get("name")
    arguments = tool_call.get("arguments") or tool_call.get("function", {}).get("arguments") or "{}"
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False)
    return {
        "id": str(tool_call.get("id") or name),
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _chunk_delta(chunk: Any) -> Any:
    if not chunk.choices:
        return None
    return chunk.choices[0].delta


def _merge_tool_call_fragment(bucket: dict[int, dict[str, Any]], fragment: Any) -> None:
    index = int(getattr(fragment, "index", 0) or 0)
    current = bucket.setdefault(
        index,
        {"id": None, "name": None, "arguments": "", "type": "function"},
    )
    if getattr(fragment, "id", None):
        current["id"] = fragment.id
    function = getattr(fragment, "function", None)
    if function is None:
        return
    if getattr(function, "name", None):
        current["name"] = function.name
    if getattr(function, "arguments", None):
        current["arguments"] += function.arguments


async def dashscope_stream_chat(
    messages: list[dict[str, Any]],
    system_prompt: str,
    tools: list[dict[str, Any]],
    model_name: str,
) -> AsyncGenerator[dict[str, Any], None]:
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:
        raise RuntimeError("openai package is not installed in this conda env.") from exc

    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("DASHSCOPE_API_KEY is not set.")

    client = AsyncOpenAI(
        api_key=api_key,
        base_url=os.getenv("DASHSCOPE_BASE_URL", DASHSCOPE_COMPATIBLE_BASE_URL),
    )
    kwargs: dict[str, Any] = {
        "model": model_name,
        "messages": _normalize_messages(messages, system_prompt),
        "stream": True,
    }
    if tools:
        kwargs["tools"] = tools

    tool_call_fragments: dict[int, dict[str, Any]] = {}
    stream = await client.chat.completions.create(**kwargs)
    async for chunk in stream:
        delta = _chunk_delta(chunk)
        if delta is None:
            continue
        content = getattr(delta, "content", None)
        if content:
            yield {"type": "assistant_delta", "content": content}

        for tool_call_fragment in getattr(delta, "tool_calls", None) or []:
            _merge_tool_call_fragment(tool_call_fragments, tool_call_fragment)

    for index in sorted(tool_call_fragments):
        tool_call = tool_call_fragments[index]
        yield {
            "type": "tool_call",
            "tool_call": {
                "id": tool_call["id"] or tool_call["name"] or f"tool-{index}",
                "name": tool_call["name"],
                "arguments": tool_call["arguments"] or "{}",
            },
        }
