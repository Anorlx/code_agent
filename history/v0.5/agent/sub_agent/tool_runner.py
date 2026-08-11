from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncGenerator, Awaitable, Callable

from agent.sub_agent.context_builder import build_task_context, task_context_report
from agent.sub_agent.permission_review import review_tool_call

PermissionReviewer = Callable[
    [str, list[dict[str, Any]], dict[str, Any], dict[str, Any], str],
    Awaitable[dict[str, Any]],
]


def _tool_result_content(result: dict[str, Any]) -> str:
    if result.get("ok") and "content" in result:
        return str(result["content"])
    if result.get("ok"):
        return json.dumps(result, ensure_ascii=False)
    return f"ERROR: {result.get('error', 'tool failed')}"


def _tool_name(tool_call: dict[str, Any]) -> str | None:
    return tool_call.get("name") or tool_call.get("function", {}).get("name")


def _tool_arguments(tool_call: dict[str, Any]) -> dict[str, Any]:
    raw_args = tool_call.get("arguments") or tool_call.get("function", {}).get("arguments") or {}
    if isinstance(raw_args, str):
        try:
            return json.loads(raw_args or "{}")
        except json.JSONDecodeError:
            return {"raw": raw_args}
    return raw_args if isinstance(raw_args, dict) else {}


def _tool_summary(name: str | None, arguments: dict[str, Any]) -> str:
    if not arguments:
        return ""
    parts = []
    for key in ("path", "pattern", "expression", "timezone", "cwd"):
        if key in arguments:
            parts.append(f"{key}={arguments[key]}")
    if "command" in arguments:
        command = arguments["command"]
        if isinstance(command, list):
            parts.append("command=" + " ".join(str(part) for part in command))
        else:
            parts.append(f"command={command}")
    if "content" in arguments:
        parts.append(f"content={len(str(arguments['content']))} chars")
    return ", ".join(parts)


def _is_parallel_safe(tool_call: dict[str, Any], tools: dict[str, dict[str, Any]]) -> bool:
    name = _tool_name(tool_call)
    return bool(name in tools and tools[name].get("parallel_safe", False))


def _tool_batches(
    tool_calls: list[dict[str, Any]],
    tools: dict[str, dict[str, Any]],
) -> list[tuple[bool, list[dict[str, Any]]]]:
    batches: list[tuple[bool, list[dict[str, Any]]]] = []
    current_parallel: list[dict[str, Any]] = []

    for tool_call in tool_calls:
        if _is_parallel_safe(tool_call, tools):
            current_parallel.append(tool_call)
            continue

        if current_parallel:
            batches.append((True, current_parallel))
            current_parallel = []
        batches.append((False, [tool_call]))

    if current_parallel:
        batches.append((True, current_parallel))
    return batches


async def _run_tool_call(
    tool_call: dict[str, Any],
    tools: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    name = _tool_name(tool_call)
    arguments = _tool_arguments(tool_call)

    if name not in tools:
        result = {"ok": False, "error": f"Unknown tool: {name}"}
    else:
        result = await tools[name]["run"](arguments)

    return {
        "role": "tool",
        "tool_call_id": tool_call.get("id", name),
        "name": name,
        "arguments": arguments,
        "summary": _tool_summary(name, arguments),
        "content": _tool_result_content(result),
        "raw_result": result,
        "created_at": time.time(),
    }


def _blocked_tool_message(tool_call: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    name = _tool_name(tool_call)
    arguments = _tool_arguments(tool_call)
    reason = review.get("reason") or "Permission review blocked this tool call."
    result = {
        "ok": False,
        "error": f"Permission denied by permission_review: {reason}",
        "review": review,
    }
    return {
        "role": "tool",
        "tool_call_id": tool_call.get("id", name),
        "name": name,
        "arguments": arguments,
        "summary": _tool_summary(name, arguments),
        "content": _tool_result_content(result),
        "raw_result": result,
        "created_at": time.time(),
    }


async def run_tool_subagent(
    user_input: str,
    messages: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    tools: dict[str, dict[str, Any]],
    permission_reviewer: PermissionReviewer | None,
    reviewer_model_name: str,
    memory_context: str | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    context_messages = build_task_context(
        user_input=user_input,
        messages=messages,
        tool_calls=tool_calls,
        memory_context=memory_context,
    )
    yield {
        "type": "sub_context",
        "agent": "tool_runner",
        "context": task_context_report(context_messages),
    }
    for is_parallel, batch in _tool_batches(tool_calls, tools):
        approved_batch: list[dict[str, Any]] = []
        for tool_call in batch:
            name = _tool_name(tool_call)
            info = tools.get(name or "", {})
            review = None
            if permission_reviewer is not None:
                review = await permission_reviewer(
                    user_input,
                    context_messages,
                    tool_call,
                    info,
                    reviewer_model_name,
                )
            elif info.get("requires_review"):
                review = await review_tool_call(
                    user_input=user_input,
                    messages=context_messages,
                    tool_call=tool_call,
                    tool_info=info,
                    model_name=reviewer_model_name,
                )

            if review is not None:
                yield {
                    "type": "tool_review",
                    "name": name,
                    "arguments": _tool_arguments(tool_call),
                    "summary": _tool_summary(name, _tool_arguments(tool_call)),
                    "review": review,
                }
                if not review.get("allowed", False):
                    yield {"type": "tool_result", "message": _blocked_tool_message(tool_call, review)}
                    continue
            approved_batch.append(tool_call)

        if not approved_batch:
            continue

        for tool_call in approved_batch:
            name = _tool_name(tool_call)
            arguments = _tool_arguments(tool_call)
            yield {
                "type": "tool_start",
                "name": name,
                "arguments": arguments,
                "summary": _tool_summary(name, arguments),
                "parallel": is_parallel and len(approved_batch) > 1,
            }

        if is_parallel and len(approved_batch) > 1:
            results = await asyncio.gather(
                *[_run_tool_call(tool_call, tools) for tool_call in approved_batch]
            )
            for result in results:
                yield {"type": "tool_result", "message": result}
        else:
            for tool_call in approved_batch:
                result = await _run_tool_call(tool_call, tools)
                yield {"type": "tool_result", "message": result}
