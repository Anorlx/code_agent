from __future__ import annotations

import json
import logging
import os
import asyncio
from copy import deepcopy
from typing import Any, AsyncGenerator

from agent.main_agent.config import DASHSCOPE_COMPATIBLE_BASE_URL, DASHSCOPE_CONTEXT_CACHE_TYPE
from agent.main_agent.retry import RetryConfig, is_transient_error, retry_delay

logger = logging.getLogger(__name__)
MODEL_RETRY_CONFIG = RetryConfig(attempts=3, initial_delay=0.5, max_delay=4.0)


def _truthy_env(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _dashscope_cache_control() -> dict[str, str] | None:
    if not _truthy_env("DASHSCOPE_CONTEXT_CACHE", True):
        return None
    cache_type = os.getenv("DASHSCOPE_CONTEXT_CACHE_TYPE", DASHSCOPE_CONTEXT_CACHE_TYPE).strip()
    if not cache_type:
        return None
    return {"type": cache_type}


def _system_content(text: str) -> str | list[dict[str, Any]]:
    cache_control = _dashscope_cache_control()
    if not cache_control:
        return text
    return [{"type": "text", "text": text, "cache_control": cache_control}]


def _message_content(message: dict[str, Any]) -> Any:
    content = message.get("content")
    if isinstance(content, list):
        return content
    return content or ""


def _content_with_cache_control(message: dict[str, Any]) -> Any:
    content = _message_content(message)
    requested_cache_control = message.get("cache_control")
    active_cache_control = _dashscope_cache_control()
    if not requested_cache_control or not active_cache_control:
        return content
    if isinstance(content, list):
        blocks = deepcopy(content)
        for block in reversed(blocks):
            if isinstance(block, dict) and block.get("type") == "text":
                block["cache_control"] = active_cache_control
                return blocks
        return blocks
    return [{"type": "text", "text": str(content), "cache_control": active_cache_control}]


def _normalize_messages(messages: list[dict[str, Any]], system_prompt: str) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = [
        {"role": "system", "content": _system_content(system_prompt)}
    ]
    for message in messages:
        role = message.get("role")
        item: dict[str, Any] = {"role": role, "content": _content_with_cache_control(message)}
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


def _usage_dict(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    if isinstance(usage, dict):
        data = dict(usage)
    elif hasattr(usage, "model_dump"):
        data = usage.model_dump()
    else:
        data = {
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
        }
    prompt_details = data.get("prompt_tokens_details") or data.get("input_tokens_details") or {}
    if hasattr(prompt_details, "model_dump"):
        prompt_details = prompt_details.model_dump()
    if isinstance(prompt_details, dict):
        cached_tokens = prompt_details.get("cached_tokens")
        cache_creation_tokens = (
            prompt_details.get("cache_creation_input_tokens")
            or prompt_details.get("cache_creation_tokens")
        )
        if cached_tokens is not None:
            data["cached_tokens"] = cached_tokens
        if cache_creation_tokens is not None:
            data["cache_creation_input_tokens"] = cache_creation_tokens
    return {key: value for key, value in data.items() if value is not None}


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
        "stream_options": {"include_usage": True},
    }
    if tools:
        kwargs["tools"] = tools

    attempts = max(1, MODEL_RETRY_CONFIG.attempts)
    for attempt_index in range(attempts):
        tool_call_fragments: dict[int, dict[str, Any]] = {}
        started_text_block = False
        started_tool_blocks: set[int] = set()
        emitted_model_event = False
        try:
            stream = await client.chat.completions.create(**kwargs)
            async for chunk in stream:
                usage = _usage_dict(getattr(chunk, "usage", None))
                if usage:
                    usage["kind"] = "dashscope_usage"
                    usage["model"] = model_name
                    emitted_model_event = True
                    yield {"type": "token_usage", "token_usage": usage}
                    continue

                delta = _chunk_delta(chunk)
                if delta is None:
                    continue
                content = getattr(delta, "content", None)
                if content:
                    if not started_text_block:
                        started_text_block = True
                        emitted_model_event = True
                        yield {"type": "content_block_start", "index": 0, "block": {"type": "text"}}
                    emitted_model_event = True
                    yield {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": content},
                    }
                    yield {"type": "assistant_delta", "content": content}

                for tool_call_fragment in getattr(delta, "tool_calls", None) or []:
                    index = int(getattr(tool_call_fragment, "index", 0) or 0)
                    block_index = index + 1
                    _merge_tool_call_fragment(tool_call_fragments, tool_call_fragment)
                    current = tool_call_fragments[index]
                    if index not in started_tool_blocks and current.get("name"):
                        started_tool_blocks.add(index)
                        emitted_model_event = True
                        yield {
                            "type": "content_block_start",
                            "index": block_index,
                            "block": {
                                "type": "tool_use",
                                "id": current.get("id") or current.get("name") or f"tool-{index}",
                                "name": current.get("name"),
                            },
                        }
                    function = getattr(tool_call_fragment, "function", None)
                    partial_json = getattr(function, "arguments", None) if function is not None else None
                    if partial_json:
                        emitted_model_event = True
                        yield {
                            "type": "content_block_delta",
                            "index": block_index,
                            "delta": {"type": "input_json_delta", "partial_json": partial_json},
                        }

            if started_text_block:
                emitted_model_event = True
                yield {"type": "content_block_stop", "index": 0, "block_type": "text"}
            for index in sorted(tool_call_fragments):
                tool_call = tool_call_fragments[index]
                if index in started_tool_blocks:
                    emitted_model_event = True
                    yield {"type": "content_block_stop", "index": index + 1, "block_type": "tool_use"}
                emitted_model_event = True
                yield {
                    "type": "tool_call",
                    "tool_call": {
                        "id": tool_call["id"] or tool_call["name"] or f"tool-{index}",
                        "name": tool_call["name"],
                        "arguments": tool_call["arguments"] or "{}",
                    },
                }
            return
        except Exception as exc:
            is_last = attempt_index >= attempts - 1
            if emitted_model_event or is_last or not is_transient_error(exc):
                raise
            delay = retry_delay(attempt_index, MODEL_RETRY_CONFIG)
            logger.warning(
                "dashscope stream retry attempt=%s/%s delay=%.2fs error=%s",
                attempt_index + 1,
                attempts,
                delay,
                exc,
            )
            yield {
                "type": "retry",
                "scope": "model",
                "attempt": attempt_index + 1,
                "max_attempts": attempts,
                "delay": delay,
                "reason": str(exc),
            }
            await asyncio.sleep(delay)
