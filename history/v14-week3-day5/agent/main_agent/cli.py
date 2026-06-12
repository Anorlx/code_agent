from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from dataclasses import dataclass
from typing import Any

from agent.main_agent.checkpoint_store import CheckpointRecord, CheckpointStore
from agent.main_agent.config import SESSION_DB_PATH
from agent.main_agent.logging_config import configure_agent_logging
from agent.main_agent.query_engine import QueryEngine
from agent.main_agent.session_store import SessionRecord, SessionStore
from agent.main_agent.terminal_input import create_terminal_input, patch_stdout_context
from agent.main_agent.terminal_ui import TerminalUI
from agent.memory_system.observer import MemoryObserver
from agent.memory_system.store import load_memory_index
from agent.tools.mcp.config import load_mcp_servers
from agent.tools.mcp.registry import (
    McpRegistryResult,
    get_cached_mcp_tool_registry,
    refresh_mcp_tool_registry,
)
from agent.main_agent.model_client import dashscope_stream_chat
from agent.sub_agent.memory_retrieval import format_memory_context, load_memory_context
from agent.sub_agent.permission_review import review_tool_call
from agent.sub_agent.session_summarizer import summarize_session
from agent.sub_agent.tool_search import select_tools
from agent.tools.registry import get_tool_registry

logger = logging.getLogger(__name__)


@dataclass
class ToolLoadResult:
    tools: dict[str, dict[str, Any]]
    refresh_task: asyncio.Task[None] | None
    metrics: dict[str, Any]


@dataclass
class CheckpointStartupAction:
    pending_input: str | None = None
    history: list[dict[str, Any]] | None = None
    main_agent_saved_memory: bool = False


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
        "shared_goal",
        "task",
    ):
        if key in arguments:
            parts.append(f"{key}={arguments[key]}")
    if "tasks" in arguments and isinstance(arguments["tasks"], list):
        parts.append(f"tasks={len(arguments['tasks'])}")
    if "research_tasks" in arguments and isinstance(arguments["research_tasks"], list):
        parts.append(f"research_tasks={len(arguments['research_tasks'])}")
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


def _event_field(event: dict[str, Any], key: str, default: Any = None) -> Any:
    if key in event:
        return event[key]
    state = event.get("state")
    if isinstance(state, dict) and key in state:
        return state[key]
    return default


def _execution_mode(event: dict[str, Any]) -> str:
    phase = str(event.get("phase") or "")
    if phase == "初始化":
        return ""
    selected_tools = list(_event_field(event, "selected_tools", []) or [])
    tool_calls = [
        call.get("name") or call.get("function", {}).get("name")
        for call in _event_field(event, "tool_calls", []) or []
    ]
    tool_results = [
        result.get("name")
        for result in _event_field(event, "tool_results", []) or []
    ]
    names = {str(name) for name in [*selected_tools, *tool_calls, *tool_results] if name}
    if "coordinator_plan" in names:
        return "coordinator"
    if "fork_tasks" in names:
        return "fork"
    if names:
        return "tool"
    if phase in {"API调用", "终止检查"}:
        return "chat"
    return ""


def _state_suffix(event: dict[str, Any]) -> str:
    details = []
    mode = _execution_mode(event)
    if mode:
        details.append(f"mode={mode}")
    selected_tools = _event_field(event, "selected_tools", []) or []
    tool_calls = _event_field(event, "tool_calls", []) or []
    tool_results = _event_field(event, "tool_results", []) or []
    if selected_tools:
        details.append("tools " + ",".join(selected_tools))
    if tool_calls:
        details.append("calls " + ",".join(call.get("name", "?") for call in tool_calls))
    if tool_results:
        summaries = [
            result.get("summary") or result.get("name", "?")
            for result in tool_results
        ]
        details.append("done " + "; ".join(summary for summary in summaries if summary))
    token_usage = _event_field(event, "token_usage")
    context_report = _event_field(event, "context_report", {}) or {}
    if token_usage:
        details.append(_format_token_usage(token_usage))
    if context_report.get("actions"):
        levels = [action.get("level", "?") for action in context_report["actions"]]
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
    elif event_type == "retry":
        _close_assistant_line(ui)
        scope = event.get("scope", "unknown")
        attempt = event.get("attempt", "?")
        max_attempts = event.get("max_attempts", "?")
        delay = float(event.get("delay") or 0)
        reason = event.get("reason") or "transient failure"
        print(ui.event_line("retry", f"{scope} attempt={attempt}/{max_attempts} delay={delay:.1f}s reason={reason}", "yellow"))
        logger.info("retry scope=%s attempt=%s/%s delay=%.2f reason=%s", scope, attempt, max_attempts, delay, reason)
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


def _checkpoint_summary_lines(record: CheckpointRecord) -> list[str]:
    state = record.state
    tool_states = state.get("tool_states") or []
    tool_calls = state.get("tool_calls") or []
    tool_results = state.get("tool_results") or []
    user_input = str(state.get("user_input") or "").replace("\n", " ").strip()
    if len(user_input) > 96:
        user_input = user_input[:95] + "."
    risky = [
        item
        for item in tool_states
        if item.get("side_effectful") and item.get("status") in {"queued", "reviewing", "waiting_user", "executing"}
    ]
    lines = [
        f"session   {record.session_id}",
        f"run_id    {record.run_id}",
        f"status    {record.status}",
        f"phase     {record.phase}",
        f"turn      {record.turn}",
        f"updated   {_format_session_time(record.updated_at)}",
        f"input     {user_input or '(empty)'}",
        f"tools     calls={len(tool_calls)} results={len(tool_results)} states={len(tool_states)}",
    ]
    if risky:
        names = ", ".join(str(item.get("name") or "unknown") for item in risky[:4])
        lines.append(f"risk      side-effect tool may be unfinished: {names}")
    return lines


def _checkpoint_has_unknown_side_effect(record: CheckpointRecord) -> bool:
    state = record.state
    tool_states = state.get("tool_states") or []
    for item in tool_states:
        if not isinstance(item, dict):
            continue
        if item.get("side_effectful") and item.get("status") in {"reviewing", "waiting_user", "executing"}:
            return True
    return False


def _checkpoint_can_rerun(record: CheckpointRecord) -> bool:
    if _checkpoint_has_unknown_side_effect(record):
        return False
    phase = str(record.phase or record.state.get("phase") or "")
    return phase in {
        "初始化",
        "预处理",
        "工具选择",
        "API调用",
        "API调用中",
        "收到tool_call",
        "终止检查",
    }


def _checkpoint_can_backfill(record: CheckpointRecord) -> bool:
    phase = str(record.phase or record.state.get("phase") or "")
    state = record.state
    return phase in {"工具结果完成", "结果回填"} and bool(state.get("messages"))


async def _handle_checkpoint_on_start(
    *,
    checkpoint_store: CheckpointStore,
    session_store: SessionStore,
    session_record: SessionRecord,
    history: list[dict[str, Any]],
    ui: TerminalUI,
) -> CheckpointStartupAction:
    record = await checkpoint_store.latest_unfinished(session_record.id)
    if record is None:
        return CheckpointStartupAction(history=history)

    reader = create_terminal_input("\ncheckpoint> ")
    while True:
        print(
            ui.panel(
                [
                    "发现一个未完成的运行断点。",
                    "",
                    *_checkpoint_summary_lines(record),
                    "",
                    "选择: r=安全恢复  d=丢弃  v=查看摘要",
                ],
                strong=True,
                title="checkpoint",
            )
        )
        choice = (await reader.read()).strip().lower()
        if choice in {"v", "view", "3"}:
            tool_states = record.state.get("tool_states") or []
            lines = [
                *_checkpoint_summary_lines(record),
                "",
                "tool states",
            ]
            if tool_states:
                for item in tool_states[:12]:
                    if not isinstance(item, dict):
                        continue
                    lines.append(
                        f"- {item.get('name')} {item.get('status')} "
                        f"side_effectful={item.get('side_effectful')}"
                    )
            else:
                lines.append("- none")
            print(ui.panel(lines, title="checkpoint summary"))
            continue
        if choice in {"d", "discard", "2", ""}:
            await checkpoint_store.mark_discarded(record.run_id)
            print(ui.event_line("checkpoint", "discarded; continue from last completed session", "yellow"))
            return CheckpointStartupAction(history=history)
        if choice in {"r", "recover", "1"}:
            if _checkpoint_has_unknown_side_effect(record):
                await checkpoint_store.mark_status(record.run_id, "unknown_outcome")
                print(
                    ui.event_line(
                        "checkpoint",
                        "side-effect tool may have partially executed; inspect project state before continuing",
                        "red",
                    )
                )
                continue
            if _checkpoint_can_backfill(record):
                restored_history = list(record.state.get("messages") or history)
                await session_store.save_messages(session_record.id, restored_history)
                await checkpoint_store.mark_completed(record.run_id)
                print(ui.event_line("checkpoint", "backfilled completed messages into session history", "green"))
                return CheckpointStartupAction(
                    history=restored_history,
                    main_agent_saved_memory=bool(record.state.get("main_agent_saved_memory")),
                )
            if _checkpoint_can_rerun(record):
                user_input = str(record.state.get("user_input") or "").strip()
                if not user_input:
                    print(ui.event_line("checkpoint", "checkpoint has no user_input; cannot recover", "red"))
                    continue
                await checkpoint_store.mark_discarded(record.run_id)
                print(ui.event_line("checkpoint", "safe phase: rerunning current input", "green"))
                return CheckpointStartupAction(pending_input=user_input, history=history)
            await checkpoint_store.mark_status(record.run_id, "needs_review")
            print(ui.event_line("checkpoint", "this phase needs manual review; choose d after inspecting state", "yellow"))
            continue
        print(ui.event_line("hint", "请输入 r 恢复，d 丢弃，或 v 查看摘要。", "yellow"))


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


async def _load_tools(ui: TerminalUI) -> ToolLoadResult:
    started = time.perf_counter()
    local_started = time.perf_counter()
    tools = get_tool_registry()
    local_tools_load_ms = int((time.perf_counter() - local_started) * 1000)
    servers = load_mcp_servers()
    metrics: dict[str, Any] = {
        "local_tools_load_ms": local_tools_load_ms,
        "mcp_cache_hit": False,
        "mcp_servers_total": len(servers),
        "mcp_servers_loaded": 0,
        "mcp_discovery_ms": 0,
        "tools_total": len(tools),
    }
    if not servers:
        metrics["startup_tools_load_ms"] = int((time.perf_counter() - started) * 1000)
        logger.info("startup tools metrics=%s", metrics)
        return ToolLoadResult(tools=tools, refresh_task=None, metrics=metrics)

    cached = get_cached_mcp_tool_registry(servers=servers)
    if cached.registry:
        tools.update(cached.registry)
        metrics.update(
            {
                "mcp_cache_hit": True,
                "mcp_servers_loaded": len(cached.server_counts),
                "mcp_discovery_ms": cached.elapsed_ms,
                "tools_total": len(tools),
            }
        )
        print(ui.event_line("mcp", f"cache hit {_format_server_counts(cached.server_counts)}", "green"))
        refresh_task = asyncio.create_task(_refresh_mcp_tools_in_background(tools, servers, ui, metrics))
        metrics["startup_tools_load_ms"] = int((time.perf_counter() - started) * 1000)
        logger.info("startup tools metrics=%s", metrics)
        return ToolLoadResult(tools=tools, refresh_task=refresh_task, metrics=metrics)

    print(ui.event_line("mcp", f"discovering {len(servers)} server(s)", "blue"))
    refreshed = await refresh_mcp_tool_registry(servers=servers, timeout=45.0)
    tools.update(refreshed.registry)
    _print_mcp_refresh_result(ui, refreshed, servers)
    metrics.update(
        {
            "mcp_servers_loaded": len(refreshed.server_counts),
            "mcp_discovery_ms": refreshed.elapsed_ms,
            "tools_total": len(tools),
        }
    )
    metrics["startup_tools_load_ms"] = int((time.perf_counter() - started) * 1000)
    logger.info("startup tools metrics=%s", metrics)
    return ToolLoadResult(tools=tools, refresh_task=None, metrics=metrics)


def _format_server_counts(server_counts: dict[str, int]) -> str:
    return ", ".join(f"{server}:{count}" for server, count in sorted(server_counts.items())) or "none"


def _drop_mcp_tools(tools: dict[str, dict[str, Any]]) -> None:
    for name in [name for name, info in tools.items() if info.get("category") == "MCP"]:
        tools.pop(name, None)


def _print_mcp_refresh_result(
    ui: TerminalUI,
    result: McpRegistryResult,
    servers: dict[str, Any],
) -> None:
    if result.registry:
        print(
            ui.event_line(
                "mcp",
                f"refresh done {_format_server_counts(result.server_counts)} in {result.elapsed_ms}ms",
                "green",
            )
        )
        for server_name, count in sorted(result.server_counts.items()):
            print(ui.event_line("mcp", f"{server_name} discovered {count} tool(s)", "green"))
    else:
        print(ui.event_line("mcp", f"refresh found no tools in {result.elapsed_ms}ms", "yellow"))
    for server_name in servers:
        if server_name not in result.server_counts:
            detail = result.errors.get(server_name) or "configured but no tools discovered"
            print(ui.event_line("mcp", f"{server_name} {detail}", "yellow"))


async def _refresh_mcp_tools_in_background(
    tools: dict[str, dict[str, Any]],
    servers: dict[str, Any],
    ui: TerminalUI,
    metrics: dict[str, Any],
) -> None:
    try:
        result = await refresh_mcp_tool_registry(servers=servers, timeout=45.0)
    except Exception as exc:
        logger.warning("mcp background refresh failed error=%s", exc)
        print(ui.event_line("mcp", f"refresh failed {exc}", "yellow"))
        return
    _drop_mcp_tools(tools)
    tools.update(result.registry)
    metrics.update(
        {
            "mcp_servers_loaded": len(result.server_counts),
            "mcp_discovery_ms": result.elapsed_ms,
            "tools_total": len(tools),
        }
    )
    logger.info("mcp background refresh metrics=%s errors=%s", metrics, result.errors)
    _print_mcp_refresh_result(ui, result, servers)


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
    startup_started = time.perf_counter()
    session_store = SessionStore()
    checkpoint_store = CheckpointStore()
    tools_task = asyncio.create_task(_load_tools(ui))
    session_setup_started = time.perf_counter()
    await asyncio.gather(session_store.setup(), checkpoint_store.setup())
    await checkpoint_store.cleanup_old()
    session_setup_ms = int((time.perf_counter() - session_setup_started) * 1000)
    try:
        session_select_started = time.perf_counter()
        session_record, history = await _choose_session(session_store, ui)
        session_select_ms = int((time.perf_counter() - session_select_started) * 1000)
        checkpoint_action = await _handle_checkpoint_on_start(
            checkpoint_store=checkpoint_store,
            session_store=session_store,
            session_record=session_record,
            history=history,
            ui=ui,
        )
        history = checkpoint_action.history if checkpoint_action.history is not None else history
        pending_user_input = checkpoint_action.pending_input
        last_main_agent_saved_memory = checkpoint_action.main_agent_saved_memory
    except (EOFError, KeyboardInterrupt):
        if not tools_task.done():
            tools_task.cancel()
        print(ui.event_line("terminal", "aborted_streaming", "red"))
        logger.info("chat_loop aborted while choosing session")
        return
    summary_tasks: set[asyncio.Task[Any]] = set()
    try:
        tool_load_result = await tools_task
        tools = tool_load_result.tools
        if tool_load_result.refresh_task is not None:
            _track_task(summary_tasks, tool_load_result.refresh_task)
        startup_metrics = {
            **tool_load_result.metrics,
            "session_setup_ms": session_setup_ms,
            "session_select_ms": session_select_ms,
            "startup_total_ms": int((time.perf_counter() - startup_started) * 1000),
        }
    except Exception as exc:
        logger.warning("startup tools load failed, falling back to local tools: %s", exc)
        print(ui.event_line("tools", f"load failed, fallback to local tools: {exc}", "yellow"))
        tools = get_tool_registry()
        startup_metrics = {
            "session_setup_ms": session_setup_ms,
            "session_select_ms": session_select_ms,
            "local_tools_load_ms": 0,
            "mcp_discovery_ms": 0,
            "mcp_cache_hit": False,
            "mcp_servers_total": len(load_mcp_servers()),
            "mcp_servers_loaded": 0,
            "tools_total": len(tools),
            "startup_total_ms": int((time.perf_counter() - startup_started) * 1000),
        }
    logger.info("startup metrics=%s", startup_metrics)
    print(
        ui.event_line(
            "startup",
            (
                f"ready in {startup_metrics['startup_total_ms']}ms "
                f"tools={startup_metrics['tools_total']} "
                f"mcp_cache_hit={startup_metrics['mcp_cache_hit']} "
                f"mcp={startup_metrics['mcp_servers_loaded']}/{startup_metrics['mcp_servers_total']}"
            ),
            "blue",
        )
    )
    memory_observer = MemoryObserver(model_call=dashscope_stream_chat)
    reader = create_terminal_input("\ncode_agent> ")
    logger.info("chat_loop started max_turns=%s tools=%s", max_turns, list(tools))
    print(ui.welcome(session_record, reader.name, len(tools), max_turns))
    while True:
        if pending_user_input is not None:
            user_input = pending_user_input
            pending_user_input = None
            print(ui.event_line("checkpoint", f"recovering input: {user_input[:120]}", "green"))
        else:
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
        engine = QueryEngine(
            model_call=dashscope_stream_chat,
            tools=tools,
            tool_selector=_selector,
            permission_reviewer=_permission_reviewer,
            permission_prompter=_make_permission_prompter(ui),
            checkpoint_store=checkpoint_store,
            session_id=session_record.id,
            max_turns=max_turns,
        )
        last_state = None
        async for event in engine.submitMessage(
            user_input,
            history=history,
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
