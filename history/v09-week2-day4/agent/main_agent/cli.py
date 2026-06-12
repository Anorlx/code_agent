from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from typing import Any

from agent.main_agent.graph import run_agent
from agent.main_agent.config import SESSION_DB_PATH
from agent.main_agent.logging_config import configure_agent_logging
from agent.main_agent.session_store import SessionRecord, SessionStore
from agent.main_agent.terminal_input import create_terminal_input, patch_stdout_context
from agent.main_agent.terminal_ui import TerminalUI
from agent.memory_system.observer import MemoryObserver
from agent.memory_system.store import load_memory_index
from agent.mcp.config import load_mcp_servers
from agent.mcp.registry import get_mcp_tool_registry
from agent.main_agent.model_client import dashscope_stream_chat
from agent.sub_agent.memory_retrieval import format_memory_context, load_memory_context
from agent.sub_agent.permission_review import review_tool_call
from agent.sub_agent.session_summarizer import summarize_session
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
    for key in (
        "path",
        "pattern",
        "expression",
        "timezone",
        "cwd",
        "address",
        "city",
        "keywords",
        "origin",
        "destination",
    ):
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
    if not parts:
        for key, value in list(arguments.items())[:3]:
            parts.append(f"{key}={value}")
    return ", ".join(parts) or "no args"


def _format_token_usage(token_usage: dict[str, Any]) -> str:
    kind = token_usage.get("kind")
    if kind and kind != "estimate":
        prompt = token_usage.get("prompt_tokens")
        completion = token_usage.get("completion_tokens")
        total = token_usage.get("total_tokens")
        remaining = token_usage.get("remaining_tokens")
        parts = [str(kind)]
        if prompt is not None:
            parts.append(f"in={prompt}")
        if completion is not None:
            parts.append(f"out={completion}")
        if total is not None:
            parts.append(f"total={total}")
        if remaining is not None:
            parts.append(f"left≈{remaining}")
        return " ".join(parts)

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
        details.append("tools " + ",".join(event["selected_tools"]))
    if event.get("tool_calls"):
        details.append("calls " + ",".join(call.get("name", "?") for call in event["tool_calls"]))
    if event.get("tool_results"):
        summaries = [
            result.get("summary") or result.get("name", "?")
            for result in event["tool_results"]
        ]
        details.append("done " + "; ".join(summary for summary in summaries if summary))
    if event.get("token_usage"):
        details.append(_format_token_usage(event["token_usage"]))
    if event.get("context_report", {}).get("actions"):
        levels = [action.get("level", "?") for action in event["context_report"]["actions"]]
        details.append("ctx " + ",".join(levels))
    return " | ".join(details)


def _format_context_actions(report: dict[str, Any]) -> str:
    actions = report.get("actions") or []
    parts = []
    for action in actions:
        level = action.get("level", "?")
        freed = action.get("freed_tokens", 0)
        parts.append(f"{level} freed≈{freed}")
    return "; ".join(parts) or "no action"


def _close_assistant_line(ui: TerminalUI) -> None:
    line_break = ui.ensure_line_break()
    if line_break:
        print(line_break, end="")


def _print_event(event: dict[str, Any], ui: TerminalUI) -> None:
    event_type = event["type"]
    logger.info("event type=%s", event_type)
    if event_type == "state":
        _close_assistant_line(ui)
        state = event["state"]
        suffix = _state_suffix(event)
        print(ui.state_line(state["turn"], event["phase"], suffix))
        logger.info("state turn=%s phase=%s %s", state["turn"], event["phase"], suffix)
    elif event_type == "assistant_delta":
        prefix = ui.assistant_start()
        if prefix:
            print(prefix, end="", flush=True)
        print(event["content"], end="", flush=True)
    elif event_type == "tool_call":
        _close_assistant_line(ui)
        tool_call = event["tool_call"]
        arguments = _parse_arguments(tool_call.get("arguments"))
        print(ui.event_line("tool call", f"{tool_call.get('name')} {_tool_summary(tool_call.get('name'), arguments)}"))
        logger.info("tool_call name=%s summary=%s", tool_call.get("name"), _tool_summary(tool_call.get("name"), arguments))
    elif event_type == "tool_start":
        _close_assistant_line(ui)
        mode = "parallel" if event.get("parallel") else "sequential"
        print(ui.event_line("tool run", f"{event.get('name')} {event.get('summary') or 'no args'} ({mode})", "yellow"))
        logger.info("tool_start name=%s mode=%s summary=%s", event.get("name"), mode, event.get("summary"))
    elif event_type == "tool_review":
        _close_assistant_line(ui)
        review = event["review"]
        status = str(review.get("action") or ("allow" if review.get("allowed") else "block"))
        reason = review.get("reason") or "no reason"
        risk = review.get("risk", "unknown")
        stage = review.get("stage", "permission")
        color = "green" if review.get("allowed") else ("yellow" if status == "ask" else "red")
        print(ui.event_line("review", f"{event.get('name')} {status} stage={stage} risk={risk} reason={reason}", color))
        logger.info("tool_review name=%s status=%s risk=%s reason=%s", event.get("name"), status, risk, reason)
    elif event_type == "permission_decision":
        _close_assistant_line(ui)
        review = event["review"]
        status = "allow" if review.get("allowed") else "deny"
        reason = review.get("reason") or "no reason"
        color = "green" if review.get("allowed") else "red"
        print(ui.event_line("permission", f"{event.get('name')} {status} reason={reason}", color))
        logger.info("permission_decision name=%s status=%s reason=%s", event.get("name"), status, reason)
    elif event_type == "tool_result":
        _close_assistant_line(ui)
        message = event["message"]
        print(ui.event_line("tool done", f"{message['name']} {message.get('summary') or 'done'}", "green"))
        logger.info("tool_result name=%s summary=%s", message["name"], message.get("summary"))
    elif event_type == "terminal":
        _close_assistant_line(ui)
        print(ui.event_line("terminal", f"{event['reason']}: {event['message']}", "red"))
        logger.info("terminal reason=%s message=%s", event["reason"], event["message"])
    elif event_type == "token_usage":
        _close_assistant_line(ui)
        usage = _format_token_usage(event["token_usage"])
        print(ui.event_line("token", usage, "blue"))
        logger.info("token_usage %s", usage)
    elif event_type == "context_management":
        _close_assistant_line(ui)
        summary = _format_context_actions(event.get("context_report", {}))
        print(ui.event_line("context", summary, "blue"))
        logger.info("context_management %s", summary)
    elif event_type == "sub_context":
        _close_assistant_line(ui)
        context = event.get("context", {})
        text = f"{event.get('agent')} messages={context.get('message_count')} ctx≈{context.get('estimated_tokens')}"
        print(ui.event_line("sub ctx", text, "blue"))
        logger.info("sub_context agent=%s context=%s", event.get("agent"), context)


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


def _make_permission_prompter(ui: TerminalUI):
    async def prompt(request: dict[str, Any]) -> dict[str, Any]:
        tool_name = request.get("tool_name") or "unknown"
        summary = request.get("summary") or "no args"
        review = request.get("review") or {}
        print(
            ui.panel(
                [
                    f"tool     {tool_name}",
                    f"summary  {summary}",
                    f"stage    {review.get('stage', 'permission')}",
                    f"risk     {review.get('risk', 'unknown')}",
                    f"reason   {review.get('reason', 'no reason')}",
                    "",
                    "选择: y=本次允许  n=拒绝",
                ],
                title="permission required",
            )
        )
        reader = create_terminal_input("\npermission> ")
        while True:
            choice = (await reader.read()).strip().lower()
            if choice in {"y", "yes", "allow", "a"}:
                return {
                    "action": "allow",
                    "allowed": True,
                    "reason": "用户交互式确认本次允许。",
                    "choice": "allow_once",
                }
            if choice in {"", "n", "no", "deny", "d"}:
                return {
                    "action": "deny",
                    "allowed": False,
                    "reason": "用户交互式确认拒绝。",
                    "choice": "deny",
                }
            print(ui.event_line("hint", "请输入 y 允许本次执行，或 n 拒绝。", "yellow"))

    return prompt


def _format_session_time(timestamp: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(timestamp))


async def _choose_session(store: SessionStore, ui: TerminalUI) -> tuple[SessionRecord, list[dict[str, Any]]]:
    sessions = await store.list_sessions()
    reader = create_terminal_input("\nsession> ")
    print(ui.session_picker(sessions))

    while True:
        choice = (await reader.read()).strip().lower()
        if choice in {"", "0", "new", "n"}:
            record = await store.create_session()
            return record, []
        if choice.isdigit():
            selected_index = int(choice) - 1
            if 0 <= selected_index < len(sessions):
                record = sessions[selected_index]
                return record, await store.load_messages(record.id)
        print(ui.event_line("hint", "请输入编号，或者输入 0 创建新会话。", "yellow"))


async def _refresh_session_summary(
    store: SessionStore,
    session_id: str,
    messages: list[dict[str, Any]],
) -> None:
    memory_index = load_memory_index()
    summary = await summarize_session(
        messages=messages,
        memory_index=memory_index,
        model_call=dashscope_stream_chat,
    )
    await store.update_summary(session_id, summary["title"], summary["summary"])
    logger.info("session summary updated session_id=%s title=%s", session_id, summary["title"])


async def _load_tools(ui: TerminalUI) -> dict[str, dict[str, Any]]:
    tools = get_tool_registry()
    servers = load_mcp_servers()
    if not servers:
        return tools

    print(ui.event_line("mcp", f"discovering {len(servers)} server(s)", "blue"))
    mcp_tools = await get_mcp_tool_registry(timeout=45.0)
    tools.update(mcp_tools)
    if mcp_tools:
        by_server: dict[str, int] = {}
        for info in mcp_tools.values():
            server = str(info.get("mcp_server") or "unknown")
            by_server[server] = by_server.get(server, 0) + 1
        summary = ", ".join(f"{server}:{count}" for server, count in sorted(by_server.items()))
        print(ui.event_line("mcp", f"ready {summary}", "green"))
        for server_name in servers:
            if server_name not in by_server:
                print(ui.event_line("mcp", f"{server_name} configured but no tools discovered", "yellow"))
    else:
        print(ui.event_line("mcp", "configured but no tools discovered", "yellow"))
    return tools


def _mcp_servers_for_tools(tools: dict[str, dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for info in tools.values():
        if info.get("category") != "MCP":
            continue
        server = str(info.get("mcp_server") or "unknown")
        counts[server] = counts.get(server, 0) + 1
    return counts


async def _mcp_forced_input(
    raw_input: str,
    tools: dict[str, dict[str, Any]],
    ui: TerminalUI,
) -> str | None:
    servers = load_mcp_servers()
    if not servers:
        print(ui.event_line("mcp", "没有找到 .mcp.json 配置。", "yellow"))
        return None

    text = raw_input.strip()
    selected_server = ""
    question = ""
    if text != "/@":
        first, _, rest = text.partition(" ")
        selected_server = first[2:].strip()
        question = rest.strip()

    if not selected_server:
        counts = _mcp_servers_for_tools(tools)
        lines = ["选择 MCP server", ""]
        for index, name in enumerate(servers, start=1):
            discovered = counts.get(name, 0)
            lines.append(f"[{index}] {name} -- discovered tools: {discovered}")
        print(ui.panel(lines, strong=True, title="mcp servers"))
        reader = create_terminal_input("\nmcp> ")
        while True:
            choice = (await reader.read()).strip()
            if choice.isdigit():
                index = int(choice) - 1
                names = list(servers)
                if 0 <= index < len(names):
                    selected_server = names[index]
                    break
            if choice in servers:
                selected_server = choice
                break
            print(ui.event_line("hint", "请输入 MCP 编号或 server 名称。", "yellow"))

    if selected_server not in servers:
        print(ui.event_line("mcp", f"未知 MCP server: {selected_server}", "red"))
        return None

    if not question:
        reader = create_terminal_input(f"\n/@{selected_server}> ")
        question = (await reader.read()).strip()
    if not question:
        return None
    return f"/@{selected_server} 用户强制要求使用 MCP server {selected_server}。问题：{question}"


def _track_task(tasks: set[asyncio.Task[Any]], task: asyncio.Task[Any]) -> None:
    tasks.add(task)
    task.add_done_callback(tasks.discard)


async def _drain_tasks(tasks: set[asyncio.Task[Any]]) -> None:
    if not tasks:
        return
    await asyncio.gather(*list(tasks), return_exceptions=True)
    tasks.clear()


async def _shutdown_background_tasks(
    memory_observer: MemoryObserver,
    summary_tasks: set[asyncio.Task[Any]],
    history: list[dict[str, Any]],
    main_agent_saved_memory: bool,
) -> None:
    if history:
        await memory_observer.flush(
            history,
            main_agent_saved_memory=main_agent_saved_memory,
        )
    else:
        await memory_observer.drain()
    await _drain_tasks(summary_tasks)


async def chat_loop(max_turns: int, color: bool = False) -> None:
    ui = TerminalUI(color=color and sys.stdout.isatty())
    session_store = SessionStore()
    await session_store.setup()
    try:
        session_record, history = await _choose_session(session_store, ui)
    except (EOFError, KeyboardInterrupt):
        print(ui.event_line("terminal", "aborted_streaming", "red"))
        logger.info("chat_loop aborted while choosing session")
        return
    summary_tasks: set[asyncio.Task[Any]] = set()
    tools = await _load_tools(ui)
    memory_observer = MemoryObserver(model_call=dashscope_stream_chat)
    reader = create_terminal_input("\ncode_agent> ")
    last_main_agent_saved_memory = False
    logger.info("chat_loop started max_turns=%s tools=%s", max_turns, list(tools))
    print(ui.welcome(session_record, reader.name, len(tools), max_turns))
    while True:
        try:
            user_input = await reader.read()
        except (EOFError, KeyboardInterrupt):
            print(ui.event_line("terminal", "aborted_streaming", "red"))
            await _shutdown_background_tasks(
                memory_observer,
                summary_tasks,
                history,
                last_main_agent_saved_memory,
            )
            logger.info("chat_loop aborted while reading input")
            return
        if user_input.lower() in {"exit", "quit"}:
            await _shutdown_background_tasks(
                memory_observer,
                summary_tasks,
                history,
                last_main_agent_saved_memory,
            )
            print(ui.event_line("terminal", "completed", "green"))
            logger.info("chat_loop completed by user command")
            return
        if not user_input:
            continue
        if user_input.startswith("/@"):
            forced_input = await _mcp_forced_input(user_input, tools, ui)
            if forced_input is None:
                continue
            user_input = forced_input
        if user_input == "/help":
            print(ui.help_text())
            continue
        if user_input == "/session":
            print(
                ui.kv_panel(
                    "session",
                    [
                        ("id", session_record.id),
                        ("title", session_record.title),
                        ("db", SESSION_DB_PATH),
                    ],
                )
            )
            continue
        if user_input == "/clear":
            print("\033[2J\033[H", end="")
            print(ui.welcome(session_record, reader.name, len(tools), max_turns))
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
            permission_prompter=_make_permission_prompter(ui),
            tools=tools,
            max_turns=max_turns,
            memory_context=memory_context,
        ):
            _print_event(event, ui)
            if "state" in event:
                last_state = event["state"]
        if last_state is not None:
            history = last_state["messages"]
            last_main_agent_saved_memory = bool(last_state.get("main_agent_saved_memory"))
            await session_store.save_messages(session_record.id, history)
            _track_task(
                summary_tasks,
                asyncio.create_task(
                    _refresh_session_summary(session_store, session_record.id, history)
                ),
            )
            memory_observer.observe(
                history,
                main_agent_saved_memory=last_main_agent_saved_memory,
            )
            logger.info("history updated messages=%s", len(history))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the minimal async agent.")
    parser.add_argument("--max-turns", type=int, default=10)
    parser.add_argument("--color", action="store_true", help="Enable ANSI colors in terminal output.")
    args = parser.parse_args()
    log_path = configure_agent_logging()
    logger.info("agent cli main started log_path=%s", log_path)
    with patch_stdout_context()():
        asyncio.run(chat_loop(args.max_turns, color=args.color))
