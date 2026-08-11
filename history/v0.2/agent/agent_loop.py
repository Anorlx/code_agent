from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncGenerator, Awaitable, Callable

from agent.state import new_state, state_event, terminal_event
from agent.subagents.permission_review_subagent import review_tool_call
from agent.subagents.tool_search_subagent import select_tools
from agent.tools.registry import dashscope_tool_specs, get_tool_registry

SYSTEM_PROMPT = """你是一个从零开始逐步扩展的 Python Agent。
你可以正常对话，也可以在需要时调用工具。
工具可以读取、修改当前项目，也可以在项目内运行本地命令；工具结果会在下一轮回填给你。
拿到工具结果后，请继续判断是再调用工具，还是给用户最终回复。
"""

TERMINATION_MESSAGES = {
    "completed": "模型正常回复且无工具调用。",
    "aborted_streaming": "用户在流式输出期间中断。",
    "aborted_tools": "用户在工具执行期间中断。",
    "max_turns": "达到最大循环次数。",
    "blocking_limit": "Token 数超过硬性限制。",
    "prompt_too_long": "上下文过长且恢复失败。",
    "model_error": "模型 API 调用异常。",
    "stop_hook_prevented": "Stop hook 阻止继续。",
    "hook_stopped": "工具 hook 阻止继续。",
    "image_error": "图片尺寸或格式错误。",
}


ModelCall = Callable[..., AsyncGenerator[dict[str, Any], None]]
ToolSelector = Callable[
    [str, list[dict[str, Any]], dict[str, dict[str, Any]], str],
    Awaitable[list[str]],
]
PermissionReviewer = Callable[
    [str, list[dict[str, Any]], dict[str, Any], dict[str, Any], str],
    Awaitable[dict[str, Any]],
]


def _message_token_estimate(messages: list[dict[str, Any]]) -> int:
    text = json.dumps(messages, ensure_ascii=False)
    return max(1, len(text) // 4)


def _tool_result_content(result: dict[str, Any]) -> str:
    if result.get("ok") and "content" in result:
        return str(result["content"])
    if result.get("ok"):
        return json.dumps(result, ensure_ascii=False)
    return f"ERROR: {result.get('error', 'tool failed')}"


def _assistant_message(content: str, tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _tool_arguments(tool_call: dict[str, Any]) -> dict[str, Any]:
    name = tool_call.get("name") or tool_call.get("function", {}).get("name")
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


async def _run_tool_call(
    tool_call: dict[str, Any],
    tools: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    name = tool_call.get("name") or tool_call.get("function", {}).get("name")
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
    }


def _blocked_tool_message(tool_call: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    name = _tool_name(tool_call)
    arguments = _tool_arguments(tool_call)
    reason = review.get("reason") or "Permission review blocked this tool call."
    result = {
        "ok": False,
        "error": f"Permission denied by permission_review_subagent: {reason}",
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
    }


def _tool_name(tool_call: dict[str, Any]) -> str | None:
    return tool_call.get("name") or tool_call.get("function", {}).get("name")


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


async def _run_tool_calls(
    tool_calls: list[dict[str, Any]],
    tools: dict[str, dict[str, Any]],
    user_input: str,
    messages: list[dict[str, Any]],
    permission_reviewer: PermissionReviewer | None,
    reviewer_model_name: str,
) -> AsyncGenerator[dict[str, Any], None]:
    for is_parallel, batch in _tool_batches(tool_calls, tools):
        approved_batch: list[dict[str, Any]] = []
        for tool_call in batch:
            name = _tool_name(tool_call)
            info = tools.get(name or "", {})
            review = None
            if permission_reviewer is not None:
                review = await permission_reviewer(
                    user_input,
                    messages,
                    tool_call,
                    info,
                    reviewer_model_name,
                )
            elif info.get("requires_review"):
                review = await review_tool_call(
                    user_input=user_input,
                    messages=messages,
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

        for tool_call in batch:
            if tool_call not in approved_batch:
                continue
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


async def run_agent(
    user_input: str,
    history: list[dict[str, Any]] | None,
    model_call: ModelCall,
    tool_selector: ToolSelector | None,
    tools: dict[str, dict[str, Any]] | None = None,
    max_turns: int = 10,
    blocking_token_limit: int = 120_000,
    stop_hook: Callable[[dict[str, Any]], bool] | None = None,
    main_model_name: str = "qwen3.5-122b-a10b",
    subagent_model_name: str = "qwen3.5-flash",
    permission_reviewer: PermissionReviewer | None = None,
    reviewer_model_name: str = "qwen3.5-flash",
) -> AsyncGenerator[dict[str, Any], None]:
    state = new_state(user_input, history)
    registry = tools or get_tool_registry()
    yield state_event(state, "初始化")

    try:
        while True:
            if state["turn"] >= max_turns:
                yield terminal_event(state, "max_turns", TERMINATION_MESSAGES["max_turns"])
                return

            if _message_token_estimate(state["messages"]) > blocking_token_limit:
                yield terminal_event(state, "blocking_limit", TERMINATION_MESSAGES["blocking_limit"])
                return

            state["turn"] += 1
            yield state_event(state, "预处理")

            selector = tool_selector
            if selector is None:
                selected = await select_tools(user_input, state["messages"], registry)
            else:
                selected = await selector(user_input, state["messages"], registry, subagent_model_name)
            state["selected_tools"] = selected
            tool_specs = dashscope_tool_specs(selected, registry)

            yield state_event(state, "API调用", selected_tools=selected)

            assistant_text = ""
            tool_calls: list[dict[str, Any]] = []
            try:
                async for event in model_call(
                    messages=state["messages"],
                    system_prompt=SYSTEM_PROMPT,
                    tools=tool_specs,
                    model_name=main_model_name,
                ):
                    if event.get("type") == "assistant_delta":
                        assistant_text += event.get("content", "")
                        yield event
                    elif event.get("type") == "tool_call":
                        tool_calls.append(event["tool_call"])
                        yield event
            except KeyboardInterrupt:
                yield terminal_event(state, "aborted_streaming", TERMINATION_MESSAGES["aborted_streaming"])
                return
            except Exception as exc:
                yield terminal_event(state, "model_error", f"{TERMINATION_MESSAGES['model_error']} {exc}")
                return

            state["messages"].append(_assistant_message(assistant_text, tool_calls))

            if not tool_calls:
                yield state_event(state, "终止检查")
                if stop_hook and stop_hook(state):
                    yield terminal_event(
                        state,
                        "stop_hook_prevented",
                        TERMINATION_MESSAGES["stop_hook_prevented"],
                    )
                    return
                yield terminal_event(state, "completed", TERMINATION_MESSAGES["completed"])
                return

            yield state_event(state, "工具执行", tool_calls=tool_calls)
            tool_messages: list[dict[str, Any]] = []
            try:
                async for tool_event in _run_tool_calls(
                    tool_calls,
                    registry,
                    user_input,
                    state["messages"],
                    permission_reviewer,
                    reviewer_model_name,
                ):
                    if tool_event["type"] == "tool_result":
                        tool_messages.append(tool_event["message"])
                    yield tool_event
            except KeyboardInterrupt:
                yield terminal_event(state, "aborted_tools", TERMINATION_MESSAGES["aborted_tools"])
                return

            state["messages"].extend(tool_messages)
            yield state_event(state, "结果回填", tool_results=tool_messages)

            if stop_hook and stop_hook(state):
                yield terminal_event(state, "hook_stopped", TERMINATION_MESSAGES["hook_stopped"])
                return

            await asyncio.sleep(0)
    except UnicodeError as exc:
        yield terminal_event(state, "image_error", f"{TERMINATION_MESSAGES['image_error']} {exc}")
