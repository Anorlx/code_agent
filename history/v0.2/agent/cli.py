from __future__ import annotations

import argparse
import asyncio
from typing import Any

from agent.agent_loop import run_agent
from agent.config import PROJECT_ROOT
from agent.input import create_terminal_input, patch_stdout_context
from agent.models.main_agent import dashscope_stream_chat
from agent.subagents.permission_review_subagent import review_tool_call
from agent.subagents.tool_search_subagent import select_tools
from agent.tools.registry import get_tool_registry


def _parse_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            import json

            parsed = json.loads(arguments or "{}")
        except Exception:
            return {"raw": arguments}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _tool_summary(name: str | None, arguments: dict[str, Any]) -> str:
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
    return ", ".join(parts) or "no args"


def _print_event(event: dict[str, Any]) -> None:
    event_type = event["type"]
    if event_type == "state":
        state = event["state"]
        details = []
        if event.get("selected_tools"):
            details.append("tools=" + ",".join(event["selected_tools"]))
        if event.get("tool_calls"):
            details.append("calls=" + ",".join(call.get("name", "?") for call in event["tool_calls"]))
        if event.get("tool_results"):
            summaries = [
                result.get("summary") or result.get("name", "?")
                for result in event["tool_results"]
            ]
            details.append("reviewed=" + "; ".join(summary for summary in summaries if summary))
        suffix = f" {' | '.join(details)}" if details else ""
        print(f"\n[state] turn={state['turn']} phase={event['phase']}{suffix}")
    elif event_type == "assistant_delta":
        print(event["content"], end="", flush=True)
    elif event_type == "tool_call":
        tool_call = event["tool_call"]
        arguments = _parse_arguments(tool_call.get("arguments"))
        print(f"\n[tool_call] {tool_call.get('name')} {_tool_summary(tool_call.get('name'), arguments)}")
    elif event_type == "tool_start":
        mode = "parallel" if event.get("parallel") else "sequential"
        print(f"\n[tool_start] {event.get('name')} {event.get('summary') or 'no args'} ({mode})")
    elif event_type == "tool_review":
        review = event["review"]
        status = "allow" if review.get("allowed") else "block"
        reason = review.get("reason") or "no reason"
        risk = review.get("risk", "unknown")
        print(f"\n[tool_review] {event.get('name')} {status} risk={risk} reason={reason}")
    elif event_type == "tool_result":
        message = event["message"]
        print(f"\n[tool_done] {message['name']} {message.get('summary') or 'done'}")
    elif event_type == "terminal":
        print(f"\n[terminal] {event['reason']}: {event['message']}")


async def _selector(
    user_input: str,
    messages: list[dict[str, Any]],
    available_tools: dict[str, dict[str, Any]],
    model_name: str,
) -> list[str]:
    return await select_tools(
        user_input=user_input,
        messages=messages,
        available_tools=available_tools,
        model_call=dashscope_stream_chat,
        model_name=model_name,
    )


async def _permission_reviewer(
    user_input: str,
    messages: list[dict[str, Any]],
    tool_call: dict[str, Any],
    tool_info: dict[str, Any],
    model_name: str,
) -> dict[str, Any]:
    return await review_tool_call(
        user_input=user_input,
        messages=messages,
        tool_call=tool_call,
        tool_info=tool_info,
        model_call=dashscope_stream_chat,
        model_name=model_name,
    )


async def chat_loop(max_turns: int) -> None:
    history: list[dict[str, Any]] = []
    tools = get_tool_registry()
    reader = create_terminal_input()
    print("agent started. 输入 exit/quit 结束。")
    print(f"tool root: {PROJECT_ROOT}")
    print(f"input mode: {reader.name}")
    while True:
        try:
            user_input = await reader.read()
        except (EOFError, KeyboardInterrupt):
            print("\n[terminal] aborted_streaming")
            return
        if user_input.lower() in {"exit", "quit"}:
            print("[terminal] completed")
            return
        if not user_input:
            continue

        last_state = None
        async for event in run_agent(
            user_input=user_input,
            history=history,
            model_call=dashscope_stream_chat,
            tool_selector=_selector,
            permission_reviewer=_permission_reviewer,
            tools=tools,
            max_turns=max_turns,
        ):
            _print_event(event)
            if "state" in event:
                last_state = event["state"]
        if last_state is not None:
            history = last_state["messages"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the minimal async agent.")
    parser.add_argument("--max-turns", type=int, default=10)
    args = parser.parse_args()
    with patch_stdout_context()():
        asyncio.run(chat_loop(args.max_turns))
