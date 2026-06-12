from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any

from agent.main_agent.graph import run_agent
from agent.main_agent.config import PROJECT_ROOT
from agent.main_agent.logging_config import configure_agent_logging
from agent.main_agent.terminal_input import create_terminal_input, patch_stdout_context
from agent.memory_system.observer import MemoryObserver
from agent.main_agent.model_client import dashscope_stream_chat
from agent.sub_agent.memory_retrieval import format_memory_context, load_memory_context
from agent.sub_agent.permission_review import review_tool_call
from agent.sub_agent.tool_search import select_tools
from agent.tools.registry import get_tool_registry

logger = logging.getLogger(__name__)


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


def _format_token_usage(token_usage: dict[str, Any]) -> str:
    context_tokens = token_usage.get("context_tokens")
    limit = token_usage.get("blocking_token_limit")
    remaining = token_usage.get("remaining_tokens")
    output = token_usage.get("output_tokens")
    parts = []
    if context_tokens is not None and limit is not None:
        parts.append(f"ctx≈{context_tokens}/{limit}")
    if remaining is not None:
        parts.append(f"left≈{remaining}")
    if output:
        parts.append(f"out≈{output}")
    return " ".join(parts)


def _state_suffix(event: dict[str, Any]) -> str:
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
    if event.get("token_usage"):
        details.append(_format_token_usage(event["token_usage"]))
    return f" {' | '.join(details)}" if details else ""


def _print_event(event: dict[str, Any]) -> None:
    event_type = event["type"]
    logger.info("event type=%s", event_type)
    if event_type == "state":
        state = event["state"]
        suffix = _state_suffix(event)
        print(f"\n[state] turn={state['turn']} phase={event['phase']}{suffix}")
        logger.info("state turn=%s phase=%s %s", state["turn"], event["phase"], suffix)
    elif event_type == "assistant_delta":
        print(event["content"], end="", flush=True)
    elif event_type == "tool_call":
        tool_call = event["tool_call"]
        arguments = _parse_arguments(tool_call.get("arguments"))
        print(f"\n[tool_call] {tool_call.get('name')} {_tool_summary(tool_call.get('name'), arguments)}")
        logger.info("tool_call name=%s summary=%s", tool_call.get("name"), _tool_summary(tool_call.get("name"), arguments))
    elif event_type == "tool_start":
        mode = "parallel" if event.get("parallel") else "sequential"
        print(f"\n[tool_start] {event.get('name')} {event.get('summary') or 'no args'} ({mode})")
        logger.info("tool_start name=%s mode=%s summary=%s", event.get("name"), mode, event.get("summary"))
    elif event_type == "tool_review":
        review = event["review"]
        status = "allow" if review.get("allowed") else "block"
        reason = review.get("reason") or "no reason"
        risk = review.get("risk", "unknown")
        print(f"\n[tool_review] {event.get('name')} {status} risk={risk} reason={reason}")
        logger.info("tool_review name=%s status=%s risk=%s reason=%s", event.get("name"), status, risk, reason)
    elif event_type == "tool_result":
        message = event["message"]
        print(f"\n[tool_done] {message['name']} {message.get('summary') or 'done'}")
        logger.info("tool_result name=%s summary=%s", message["name"], message.get("summary"))
    elif event_type == "terminal":
        print(f"\n[terminal] {event['reason']}: {event['message']}")
        logger.info("terminal reason=%s message=%s", event["reason"], event["message"])
    elif event_type == "token_usage":
        usage = _format_token_usage(event["token_usage"])
        print(f"\n[token] {usage}")
        logger.info("token_usage %s", usage)


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
    memory_observer = MemoryObserver(model_call=dashscope_stream_chat)
    reader = create_terminal_input()
    logger.info("chat_loop started max_turns=%s tools=%s", max_turns, list(tools))
    print("agent started. 输入 exit/quit 结束。")
    print(f"tool root: {PROJECT_ROOT}")
    print(f"input mode: {reader.name}")
    while True:
        try:
            user_input = await reader.read()
        except (EOFError, KeyboardInterrupt):
            print("\n[terminal] aborted_streaming")
            await memory_observer.drain()
            logger.info("chat_loop aborted while reading input")
            return
        if user_input.lower() in {"exit", "quit"}:
            await memory_observer.drain()
            print("[terminal] completed")
            logger.info("chat_loop completed by user command")
            return
        if not user_input:
            continue

        memory_context_data = await load_memory_context(
            user_input=user_input,
            model_call=dashscope_stream_chat,
        )
        memory_context = format_memory_context(memory_context_data)
        logger.info(
            "memory_context loaded selected_files=%s",
            memory_context_data.get("selected_files", []),
        )
        last_state = None
        async for event in run_agent(
            user_input=user_input,
            history=history,
            model_call=dashscope_stream_chat,
            tool_selector=_selector,
            permission_reviewer=_permission_reviewer,
            tools=tools,
            max_turns=max_turns,
            memory_context=memory_context,
        ):
            _print_event(event)
            if "state" in event:
                last_state = event["state"]
        if last_state is not None:
            history = last_state["messages"]
            memory_observer.observe(
                history,
                main_agent_saved_memory=bool(last_state.get("main_agent_saved_memory")),
            )
            logger.info("history updated messages=%s", len(history))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the minimal async agent.")
    parser.add_argument("--max-turns", type=int, default=10)
    args = parser.parse_args()
    log_path = configure_agent_logging()
    logger.info("agent cli main started log_path=%s", log_path)
    with patch_stdout_context()():
        asyncio.run(chat_loop(args.max_turns))
