from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, AsyncGenerator, Awaitable, Callable, Literal

from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from agent.main_agent.checkpoint_store import CheckpointStore
from agent.main_agent.config import DEFAULT_MAIN_MODEL, DEFAULT_SUB_AGENT_MODEL
from agent.main_agent.context_manager import ContextConfig, manage_context, snip_tool_results
from agent.main_agent.prompt_cache import evaluate_prompt_cache, request_messages_for_prompt
from agent.main_agent.state import new_state, state_event, terminal_event
from agent.main_agent.token_usage import build_real_usage_snapshot, build_token_snapshot, estimate_tokens
from agent.main_agent.tool_executor import StreamingToolExecutor
from agent.main_agent.verifier import run_verifier, verifier_tool_message
from agent.sub_agent.mode_router import (
    build_coordinator_arguments,
    build_fork_task_arguments,
    route_agent_mode,
)
from agent.sub_agent.tool_runner import PermissionPrompter, PermissionReviewer
from agent.sub_agent.tool_search import select_tools
from agent.tools.registry import exposed_dashscope_tool_specs, get_tool_registry

SYSTEM_PROMPT = """你是一个从零开始逐步扩展的 Python Agent。
你可以正常对话，也可以在需要时调用工具。
你每轮默认只会看到 AlwaysLoad 基础工具；如果需要写文件、运行命令、MCP、skills 或编排能力，先使用 tool_search 发现需要延迟加载的工具。
工具由 tool_runner 执行；它会拿到完整上下文，但只把工具结果返回给你。
拿到工具结果后，请继续判断是再调用工具，还是给用户最终回复。
如果用户明确提出长期偏好、项目长期约束或外部引用，你可以使用 save_memory 工具主动保存。
如果用户明确要求忘记或删除某条长期记忆，你可以使用 delete_memory 工具。
记忆只是线索，使用任何记忆前都要结合当前项目状态验证。
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
StopHook = Callable[[dict[str, Any]], bool]
logger = logging.getLogger(__name__)
ORCHESTRATION_TOOLS = {"fork_tasks", "coordinator_plan"}


class AgentGraphState(TypedDict, total=False):
    user_input: str
    turn: int
    phase: str
    messages: list[dict[str, Any]]
    selected_tools: list[str]
    tool_calls: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    termination_reason: str | None
    terminal_message: str | None
    model_call: ModelCall
    tool_selector: ToolSelector | None
    tools: dict[str, dict[str, Any]]
    max_turns: int
    blocking_token_limit: int
    stop_hook: StopHook | None
    main_model_name: str
    subagent_model_name: str
    permission_reviewer: PermissionReviewer | None
    permission_prompter: PermissionPrompter | None
    reviewer_model_name: str
    event_sink: Callable[[dict[str, Any]], None] | None
    memory_context: str | None
    main_agent_saved_memory: bool
    context_config: ContextConfig
    context_report: dict[str, Any] | None
    run_id: str
    checkpoint_store: CheckpointStore | None
    session_id: str | None
    created_at: float
    verifier_report: dict[str, Any] | None
    verification_attempts: int
    execution_mode: str
    mode_decision: dict[str, Any] | None
    planned_tool_call: dict[str, Any] | None
    frozen_system_prompt: str
    prompt_cache_report: dict[str, Any] | None


def _message_token_estimate(messages: list[dict[str, Any]]) -> int:
    return estimate_tokens(messages)


def _langgraph_recursion_limit(max_turns: int) -> int:
    return max(25, max_turns * 5 + 10)


def _assistant_message(content: str, tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content, "created_at": time.time()}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _system_prompt(state: "AgentGraphState") -> str:
    return str(state.get("frozen_system_prompt") or SYSTEM_PROMPT)


def _request_messages(state: "AgentGraphState") -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    return request_messages_for_prompt(
        messages=state.get("messages", []),
        user_input=state.get("user_input", ""),
        memory_context=state.get("memory_context"),
    )


def _request_tool_specs(state: "AgentGraphState") -> list[dict[str, Any]]:
    tools = state.get("tools", {})
    if not tools:
        return []
    return exposed_dashscope_tool_specs(list(state.get("selected_tools", []) or []), tools)


def _visible_state(state: AgentGraphState) -> dict[str, Any]:
    return {
        "run_id": state.get("run_id"),
        "turn": state.get("turn", 0),
        "phase": state.get("phase", "初始化"),
        "messages": list(state.get("messages", [])),
        "selected_tools": list(state.get("selected_tools", [])),
        "tool_calls": list(state.get("tool_calls", [])),
        "tool_results": list(state.get("tool_results", [])),
        "verifier_report": state.get("verifier_report"),
        "execution_mode": state.get("execution_mode", ""),
        "mode_decision": state.get("mode_decision"),
        "prompt_cache_report": state.get("prompt_cache_report"),
        "termination_reason": state.get("termination_reason"),
        "main_agent_saved_memory": bool(state.get("main_agent_saved_memory", False)),
    }


def _emit(state: AgentGraphState, event: dict[str, Any]) -> None:
    sink = state.get("event_sink")
    if sink is not None:
        sink(event)


def _emit_state(state: AgentGraphState, phase: str, **extra: Any) -> None:
    extra.setdefault(
        "token_usage",
        build_token_snapshot(
            messages=_request_messages(state)[0],
            system_prompt=_system_prompt(state),
            tools=_request_tool_specs(state),
            blocking_token_limit=state.get("blocking_token_limit", 120_000),
        ),
    )
    _emit(state, state_event(_visible_state(state), phase, **extra))


def _emit_terminal(state: AgentGraphState, reason: str, message: str) -> None:
    _emit(state, terminal_event(_visible_state(state), reason, message))


def _terminal_update(
    state: AgentGraphState,
    reason: str,
    message: str,
) -> dict[str, Any]:
    _emit_terminal(state, reason, message)
    return {
        "termination_reason": reason,
        "terminal_message": message,
        "phase": reason,
    }


def _coerce_selected_tools(selected: Any, available_tools: dict[str, dict[str, Any]]) -> list[str]:
    if not isinstance(selected, list):
        return []
    return [
        name
        for name in selected
        if isinstance(name, str) and name in available_tools
    ]


def _completed_orchestration(messages: list[dict[str, Any]]) -> set[str]:
    completed: set[str] = set()
    for message in messages:
        if message.get("role") != "tool":
            continue
        name = str(message.get("name") or "")
        if name in ORCHESTRATION_TOOLS:
            completed.add(name)
    return completed


def _explicit_orchestration_rerun(user_input: str, tool_name: str) -> bool:
    text = user_input.lower()
    rerun_markers = ("重新", "再跑", "再执行", "重跑", "rerun", "run again", "again")
    if not any(marker in text for marker in rerun_markers):
        return False
    if tool_name == "fork_tasks":
        return "fork" in text or "并行" in text
    if tool_name == "coordinator_plan":
        return "coordinator" in text or "协调器" in text
    return False


def _checkpoint_payload(
    state: AgentGraphState,
    *,
    assistant_text: str = "",
    submitted_tool_ids: set[str] | None = None,
    tool_executor: StreamingToolExecutor | None = None,
) -> dict[str, Any]:
    tool_states = tool_executor.checkpoint_tool_states() if tool_executor is not None else []
    return {
        "session_id": state.get("session_id"),
        "run_id": state.get("run_id"),
        "user_input": state.get("user_input", ""),
        "turn": state.get("turn", 0),
        "phase": state.get("phase", "初始化"),
        "messages": list(state.get("messages", [])),
        "selected_tools": list(state.get("selected_tools", [])),
        "tool_calls": list(state.get("tool_calls", [])),
        "tool_results": list(state.get("tool_results", [])),
        "tool_states": tool_states,
        "verifier_report": state.get("verifier_report"),
        "execution_mode": state.get("execution_mode", ""),
        "mode_decision": state.get("mode_decision"),
        "planned_tool_call": state.get("planned_tool_call"),
        "prompt_cache_report": state.get("prompt_cache_report"),
        "assistant_text": assistant_text,
        "submitted_tool_ids": sorted(submitted_tool_ids or set()),
        "main_agent_saved_memory": bool(state.get("main_agent_saved_memory", False)),
        "memory_context": state.get("memory_context") or "",
        "created_at": state.get("created_at", time.time()),
        "updated_at": time.time(),
    }


async def _save_checkpoint(
    state: AgentGraphState,
    *,
    assistant_text: str = "",
    submitted_tool_ids: set[str] | None = None,
    tool_executor: StreamingToolExecutor | None = None,
    status: str = "running",
) -> None:
    store = state.get("checkpoint_store")
    if store is None or not state.get("session_id"):
        return
    payload = _checkpoint_payload(
        state,
        assistant_text=assistant_text,
        submitted_tool_ids=submitted_tool_ids,
        tool_executor=tool_executor,
    )
    await store.save_checkpoint(
        session_id=state["session_id"],
        run_id=state["run_id"],
        turn=int(state.get("turn", 0) or 0),
        phase=str(state.get("phase") or "初始化"),
        state=payload,
        status=status,
    )


async def _mark_checkpoint_terminal(
    state: AgentGraphState,
    status: str,
    *,
    reason: str = "",
) -> None:
    store = state.get("checkpoint_store")
    if store is None or not state.get("session_id"):
        return
    run_id = state.get("run_id", "")
    payload = _checkpoint_payload(state)
    if status == "completed":
        await store.mark_completed(run_id)
    elif status in {"aborted", "failed", "needs_review", "unknown_outcome"}:
        if reason:
            payload["checkpoint_error"] = reason
        if status == "aborted":
            await store.mark_aborted(run_id, reason)
        elif status == "failed":
            await store.mark_failed(run_id, reason)
        else:
            await store.mark_status(run_id, status, state=payload)


async def _preprocess_node(state: AgentGraphState) -> dict[str, Any]:
    if state.get("turn", 0) >= state["max_turns"]:
        logger.info("agent max_turns reached turn=%s", state.get("turn", 0))
        return _terminal_update(state, "max_turns", TERMINATION_MESSAGES["max_turns"])

    next_state: AgentGraphState = dict(state)
    managed_messages, context_report = await manage_context(
        state["messages"],
        system_prompt=_system_prompt(state),
        model_call=state.get("model_call"),
        config=state.get("context_config"),
    )
    next_state["messages"] = managed_messages
    next_state["context_report"] = context_report
    next_state["turn"] = state.get("turn", 0) + 1
    next_state["phase"] = "预处理"
    await _save_checkpoint(next_state)
    _emit_state(next_state, "预处理", context_report=context_report)
    if context_report.get("actions"):
        _emit(
            next_state,
            {
                "type": "context_management",
                "context_report": context_report,
            },
        )
        logger.info("context management actions=%s", context_report["actions"])

    if _message_token_estimate(next_state["messages"]) > state["blocking_token_limit"]:
        logger.warning("agent blocking token limit reached after context management")
        return _terminal_update(next_state, "blocking_limit", TERMINATION_MESSAGES["blocking_limit"])

    mode_decision = await route_agent_mode(
        user_input=state["user_input"],
        messages=next_state["messages"],
        model_call=state.get("model_call"),
        model_name=state["subagent_model_name"],
    )
    execution_mode = str(mode_decision.get("mode") or "tools")
    completed_orchestration = _completed_orchestration(next_state["messages"])
    completed_without_rerun = {
        name
        for name in completed_orchestration
        if not _explicit_orchestration_rerun(state["user_input"], name)
    }
    if completed_without_rerun:
        execution_mode = "post_orchestration"
        mode_decision = {
            **mode_decision,
            "mode": execution_mode,
            "reason": "编排工具已完成，本轮进入主模型总结阶段。",
        }
    planned_tool_call: dict[str, Any] | None = None
    selected_tools: list[str] = []

    if execution_mode in {"chat", "post_orchestration"}:
        selected_tools = []
    elif execution_mode == "fork" and "fork_tasks" in state["tools"]:
        arguments = await build_fork_task_arguments(
            user_input=state["user_input"],
            messages=next_state["messages"],
            available_tools=state["tools"],
            model_call=state.get("model_call"),
            model_name=state["subagent_model_name"],
        )
        planned_tool_call = {
            "id": f"mode-fork-{next_state['turn']}",
            "name": "fork_tasks",
            "arguments": arguments,
        }
        selected_tools = ["fork_tasks"]
    elif execution_mode == "coordinator" and "coordinator_plan" in state["tools"]:
        arguments = await build_coordinator_arguments(
            user_input=state["user_input"],
            messages=next_state["messages"],
            model_call=state.get("model_call"),
            model_name=state["subagent_model_name"],
        )
        planned_tool_call = {
            "id": f"mode-coordinator-{next_state['turn']}",
            "name": "coordinator_plan",
            "arguments": arguments,
        }
        selected_tools = ["coordinator_plan"]
    else:
        execution_mode = "tools"
        selector = state.get("tool_selector")
        if selector is None:
            selected = await select_tools(
                state["user_input"],
                next_state["messages"],
                state["tools"],
            )
        else:
            selected = await selector(
                state["user_input"],
                next_state["messages"],
                state["tools"],
                state["subagent_model_name"],
            )
        selected_tools = [
            name
            for name in _coerce_selected_tools(selected, state["tools"])
            if name not in {"fork_tasks", "coordinator_plan"}
        ]

    logger.info(
        "preprocess mode=%s selected_tools=%s turn=%s reason=%s",
        execution_mode,
        selected_tools,
        next_state["turn"],
        mode_decision.get("reason"),
    )
    routed_state: AgentGraphState = dict(next_state)
    routed_state["selected_tools"] = selected_tools
    routed_state["execution_mode"] = execution_mode
    routed_state["mode_decision"] = mode_decision
    routed_state["planned_tool_call"] = planned_tool_call
    await _save_checkpoint(routed_state)
    _emit_state(
        routed_state,
        "预处理",
        selected_tools=selected_tools,
        mode_decision=mode_decision,
    )

    return {
        "turn": next_state["turn"],
        "phase": "预处理",
        "messages": next_state["messages"],
        "context_report": context_report,
        "selected_tools": selected_tools,
        "tool_calls": [],
        "tool_results": [],
        "execution_mode": execution_mode,
        "mode_decision": mode_decision,
        "planned_tool_call": planned_tool_call,
    }


async def _api_call_node(state: AgentGraphState) -> dict[str, Any]:
    request_messages, stable_history, dynamic_messages = _request_messages(state)
    tool_specs = _request_tool_specs(state)
    prompt_cache_report = evaluate_prompt_cache(
        session_id=state.get("session_id"),
        system_prompt=_system_prompt(state),
        tools=tool_specs,
        stable_history=stable_history,
        dynamic_messages=dynamic_messages,
        selected_tools=list(state.get("selected_tools", [])),
    )
    cache_state: AgentGraphState = dict(state)
    cache_state["prompt_cache_report"] = prompt_cache_report
    _emit_state(
        cache_state,
        "API调用",
        selected_tools=state.get("selected_tools", []),
        prompt_cache_report=prompt_cache_report,
        token_usage=build_token_snapshot(
            messages=request_messages,
            system_prompt=_system_prompt(state),
            tools=tool_specs,
            blocking_token_limit=state["blocking_token_limit"],
        ),
    )
    _emit(state, {"type": "prompt_cache", "prompt_cache": prompt_cache_report})
    await _save_checkpoint(state)
    logger.info(
        "api_call start turn=%s selected_tools=%s request_tools=%s cache_hit=%s append_only=%s",
        state.get("turn"),
        state.get("selected_tools", []),
        len(tool_specs),
        prompt_cache_report.get("stable_cache_hit"),
        prompt_cache_report.get("prefix_append_only"),
    )

    tool_executor = StreamingToolExecutor(
        user_input=state["user_input"],
        messages=state["messages"],
        tools=state["tools"],
        permission_reviewer=state.get("permission_reviewer"),
        permission_prompter=state.get("permission_prompter"),
        reviewer_model_name=state["reviewer_model_name"],
        memory_context=state.get("memory_context"),
        runtime_context={
            "user_input": state["user_input"],
            "messages": state["messages"],
            "tools": state["tools"],
            "model_call": state["model_call"],
            "main_model_name": state["main_model_name"],
            "subagent_model_name": state["subagent_model_name"],
            "memory_context": state.get("memory_context"),
            "session_id": state.get("session_id"),
        },
    )

    assistant_text = ""
    tool_calls: list[dict[str, Any]] = []
    submitted_tool_ids: set[str] = set()
    model_usage: dict[str, Any] | None = None
    last_stream_checkpoint_at = time.monotonic()
    last_stream_checkpoint_chars = 0

    planned_tool_call = state.get("planned_tool_call")
    if isinstance(planned_tool_call, dict) and planned_tool_call.get("name"):
        if state.get("execution_mode") == "fork":
            assistant_text = "我会把这个任务拆给多个只读 worker 并行调查，然后汇总结果。"
        elif state.get("execution_mode") == "coordinator":
            assistant_text = "我会先按 Coordinator 模式进行研究和综合，形成实施规格。"
        else:
            assistant_text = f"我会使用 {state.get('execution_mode')} 模式处理这个任务。"
        tool_calls.append(planned_tool_call)
        submitted_tool_ids.add(str(planned_tool_call.get("id") or planned_tool_call.get("name")))
        next_state_tc: AgentGraphState = dict(state)
        next_state_tc["tool_calls"] = list(tool_calls)
        next_state_tc["phase"] = "收到tool_call"
        await _save_checkpoint(
            next_state_tc,
            assistant_text=assistant_text,
            submitted_tool_ids=submitted_tool_ids,
            tool_executor=tool_executor,
        )
        _emit(state, {"type": "tool_call", "tool_call": planned_tool_call})
        _emit_state(next_state_tc, "工具执行", tool_calls=list(tool_calls))
        tool_executor.submit(planned_tool_call)
        await asyncio.sleep(0)
        for tool_event in await tool_executor.drain_ready():
            _emit(state, tool_event)
    else:
        try:
            async for event in state["model_call"](
                messages=request_messages,
                system_prompt=_system_prompt(state),
                tools=tool_specs,
                model_name=state["main_model_name"],
            ):
                for tool_event in await tool_executor.drain_ready():
                    if tool_event.get("type") == "tool_result":
                        pass
                    _emit(state, tool_event)

                if event.get("type") == "assistant_delta":
                    assistant_text += event.get("content", "")
                    now = time.monotonic()
                    if (
                        now - last_stream_checkpoint_at >= 1.0
                        or len(assistant_text) - last_stream_checkpoint_chars >= 500
                    ):
                        next_state: AgentGraphState = dict(state)
                        next_state["phase"] = "API调用中"
                        next_state["tool_calls"] = tool_calls
                        next_state["tool_results"] = list(tool_executor.results)
                        await _save_checkpoint(
                            next_state,
                            assistant_text=assistant_text,
                            submitted_tool_ids=submitted_tool_ids,
                            tool_executor=tool_executor,
                        )
                        last_stream_checkpoint_at = now
                        last_stream_checkpoint_chars = len(assistant_text)
                    _emit(state, event)
                elif event.get("type") == "tool_call":
                    tool_call = event["tool_call"]
                    tool_id = str(tool_call.get("id") or tool_call.get("name") or len(tool_calls))
                    if tool_id not in submitted_tool_ids:
                        submitted_tool_ids.add(tool_id)
                        tool_calls.append(tool_call)
                        next_state_tc: AgentGraphState = dict(state)
                        next_state_tc["tool_calls"] = list(tool_calls)
                        next_state_tc["phase"] = "收到tool_call"
                        await _save_checkpoint(
                            next_state_tc,
                            assistant_text=assistant_text,
                            submitted_tool_ids=submitted_tool_ids,
                            tool_executor=tool_executor,
                        )
                        _emit(state, event)
                        tool_executor.submit(tool_call)
                        await asyncio.sleep(0)
                        for tool_event in await tool_executor.drain_ready():
                            _emit(state, tool_event)
                elif event.get("type") == "token_usage":
                    model_usage = event.get("token_usage", {})
                    cached_tokens = model_usage.get("cached_tokens")
                    cache_creation_tokens = model_usage.get("cache_creation_input_tokens")
                    if cached_tokens is not None or cache_creation_tokens is not None:
                        prompt_cache_report = dict(prompt_cache_report)
                        prompt_cache_report.update(
                            {
                                "server_cache_hit": bool(cached_tokens),
                                "server_cached_tokens": cached_tokens,
                                "server_cache_creation_input_tokens": cache_creation_tokens,
                            }
                        )
                        _emit(state, {"type": "prompt_cache", "prompt_cache": prompt_cache_report})
                    _emit(state, event)
                else:
                    _emit(state, event)
        except KeyboardInterrupt:
            logger.info("api_call aborted by user")
            return _terminal_update(state, "aborted_streaming", TERMINATION_MESSAGES["aborted_streaming"])
        except Exception as exc:
            logger.exception("api_call failed")
            return _terminal_update(state, "model_error", f"{TERMINATION_MESSAGES['model_error']} {exc}")

    try:
        for tool_event in await tool_executor.finish():
            if tool_event.get("type") == "tool_result":
                pass
            _emit(state, tool_event)
    except KeyboardInterrupt:
        logger.info("tool execution aborted during finish")
        return _terminal_update(state, "aborted_tools", TERMINATION_MESSAGES["aborted_tools"])

    messages = list(state["messages"])
    messages.append(_assistant_message(assistant_text, tool_calls))
    result_state: AgentGraphState = dict(state)
    result_state["messages"] = messages
    result_state["tool_calls"] = tool_calls
    result_state["tool_results"] = list(tool_executor.results)
    result_state["phase"] = "API调用"
    await _save_checkpoint(
        result_state,
        assistant_text=assistant_text,
        submitted_tool_ids=submitted_tool_ids,
        tool_executor=tool_executor,
    )
    logger.info("api_call done turn=%s tool_calls=%s", state.get("turn"), len(tool_calls))
    _emit(
        state,
        {
            "type": "token_usage",
            "token_usage": (
                build_real_usage_snapshot(
                    model_usage,
                    blocking_token_limit=state["blocking_token_limit"],
                )
                if model_usage
                else build_token_snapshot(
                    messages=request_messages,
                    system_prompt=_system_prompt(state),
                    tools=tool_specs,
                    blocking_token_limit=state["blocking_token_limit"],
                    output_text=assistant_text,
                )
            ),
        },
    )
    return {
        "messages": messages,
        "tool_calls": tool_calls,
        "tool_results": list(tool_executor.results),
        "phase": "API调用",
        "prompt_cache_report": prompt_cache_report,
    }


async def _result_backfill_node(state: AgentGraphState) -> dict[str, Any]:
    messages = list(state["messages"])
    tool_results = state.get("tool_results", [])
    messages.extend(tool_results)
    snip_reports = []
    for result in tool_results:
        if result.get("name") != "snip_context":
            continue
        raw_result = result.get("raw_result") or {}
        messages, snip_report = snip_tool_results(
            messages,
            tool_call_ids=list(raw_result.get("tool_call_ids") or []),
            tool_names=list(raw_result.get("tool_names") or []),
        )
        snip_reports.append(snip_report)
    main_agent_saved_memory = any(
        result.get("name") in {"save_memory", "delete_memory", "prune_memories"}
        for result in tool_results
    )
    next_state: AgentGraphState = dict(state)
    next_state["messages"] = messages
    next_state["main_agent_saved_memory"] = (
        bool(state.get("main_agent_saved_memory", False)) or main_agent_saved_memory
    )
    next_state["phase"] = "结果回填"
    await _save_checkpoint(next_state)
    if snip_reports:
        _emit(
            next_state,
            {
                "type": "context_management",
                "context_report": {"actions": snip_reports},
            },
        )
    _emit_state(next_state, "结果回填", tool_results=state.get("tool_results", []))

    stop_hook = state.get("stop_hook")
    if stop_hook and stop_hook(_visible_state(next_state)):
        logger.info("tool hook stopped after result backfill")
        return {
            **_terminal_update(next_state, "hook_stopped", TERMINATION_MESSAGES["hook_stopped"]),
            "messages": messages,
            "main_agent_saved_memory": next_state["main_agent_saved_memory"],
        }

    logger.info(
        "result_backfill done tool_results=%s main_agent_saved_memory=%s",
        len(tool_results),
        next_state["main_agent_saved_memory"],
    )
    return {
        "messages": messages,
        "phase": "结果回填",
        "main_agent_saved_memory": next_state["main_agent_saved_memory"],
    }


async def _verification_node(state: AgentGraphState) -> dict[str, Any]:
    next_state: AgentGraphState = dict(state)
    next_state["phase"] = "验证"
    _emit_state(next_state, "验证", tool_results=state.get("tool_results", []))
    await _save_checkpoint(next_state)
    try:
        report = await run_verifier(
            user_input=state["user_input"],
            messages=state.get("messages", []),
            tool_results=state.get("tool_results", []),
            tools=state["tools"],
            model_call=state.get("model_call"),
            model_name=state["reviewer_model_name"],
        )
    except Exception as exc:
        logger.exception("verifier failed")
        report = {
            "status": "warning",
            "summary": f"Verifier crashed: {exc}",
            "issues": [
                {
                    "severity": "medium",
                    "location": "verifier",
                    "problem": str(exc),
                    "suggestion": "继续主流程，但最终回复中说明验证器没有完整运行。",
                }
            ],
            "diff": {},
            "commands": [],
            "review": {},
        }

    next_state["verifier_report"] = report
    next_state["verification_attempts"] = int(state.get("verification_attempts", 0) or 0) + 1
    messages = list(state.get("messages", []))
    if report.get("status") != "skipped":
        messages.append(verifier_tool_message(report))
    next_state["messages"] = messages
    await _save_checkpoint(next_state)
    _emit(
        next_state,
        {
            "type": "verification",
            "status": report.get("status"),
            "summary": report.get("summary"),
            "issues": report.get("issues", []),
            "commands": report.get("commands", []),
            "changed_files": (report.get("diff") or {}).get("changed_files", []),
        },
    )
    logger.info(
        "verification done status=%s commands=%s changed_files=%s",
        report.get("status"),
        len(report.get("commands", [])),
        len((report.get("diff") or {}).get("changed_files", [])),
    )
    return {
        "messages": messages,
        "phase": "验证",
        "verifier_report": report,
        "verification_attempts": next_state["verification_attempts"],
    }


async def _termination_check_node(state: AgentGraphState) -> dict[str, Any]:
    _emit_state(state, "终止检查")
    stop_hook = state.get("stop_hook")
    if stop_hook and stop_hook(_visible_state(state)):
        logger.info("stop_hook prevented completion")
        await _mark_checkpoint_terminal(state, "aborted", reason="stop_hook_prevented")
        return _terminal_update(
            state,
            "stop_hook_prevented",
            TERMINATION_MESSAGES["stop_hook_prevented"],
        )
    logger.info("agent completed turn=%s", state.get("turn"))
    await _mark_checkpoint_terminal(state, "completed", reason="completed")
    return _terminal_update(state, "completed", TERMINATION_MESSAGES["completed"])


def _route_after_preprocess(state: AgentGraphState) -> Literal["api_call", "__end__"]:
    if state.get("termination_reason"):
        return END
    return "api_call"


def _route_after_api_call(state: AgentGraphState) -> Literal["result_backfill", "termination_check", "__end__"]:
    if state.get("termination_reason"):
        return END
    if state.get("tool_calls"):
        return "result_backfill"
    return "termination_check"


def _route_after_result_backfill(state: AgentGraphState) -> Literal["verify", "__end__"]:
    if state.get("termination_reason"):
        return END
    return "verify"


def _route_after_verify(state: AgentGraphState) -> Literal["preprocess", "__end__"]:
    if state.get("termination_reason"):
        return END
    return "preprocess"


def build_agent_graph():
    graph = StateGraph(AgentGraphState)
    graph.add_node("preprocess", _preprocess_node)
    graph.add_node("api_call", _api_call_node)
    graph.add_node("termination_check", _termination_check_node)
    graph.add_node("result_backfill", _result_backfill_node)
    graph.add_node("verify", _verification_node)

    graph.add_edge(START, "preprocess")
    graph.add_conditional_edges("preprocess", _route_after_preprocess)
    graph.add_conditional_edges("api_call", _route_after_api_call)
    graph.add_edge("termination_check", END)
    graph.add_conditional_edges("result_backfill", _route_after_result_backfill)
    graph.add_conditional_edges("verify", _route_after_verify)
    return graph.compile()


def _initial_graph_state(
    user_input: str,
    history: list[dict[str, Any]] | None,
    model_call: ModelCall,
    tool_selector: ToolSelector | None,
    tools: dict[str, dict[str, Any]],
    max_turns: int,
    blocking_token_limit: int,
    stop_hook: StopHook | None,
    main_model_name: str,
    subagent_model_name: str,
    permission_reviewer: PermissionReviewer | None,
    permission_prompter: PermissionPrompter | None,
    reviewer_model_name: str,
    memory_context: str | None = None,
    event_sink: Callable[[dict[str, Any]], None] | None = None,
    context_config: ContextConfig | None = None,
    checkpoint_store: CheckpointStore | None = None,
    session_id: str | None = None,
) -> AgentGraphState:
    state = new_state(user_input, history)
    run_id = uuid.uuid4().hex
    return {
        **state,
        "user_input": user_input,
        "model_call": model_call,
        "tool_selector": tool_selector,
        "tools": tools,
        "max_turns": max_turns,
        "blocking_token_limit": blocking_token_limit,
        "stop_hook": stop_hook,
        "main_model_name": main_model_name,
        "subagent_model_name": subagent_model_name,
        "permission_reviewer": permission_reviewer,
        "permission_prompter": permission_prompter,
        "reviewer_model_name": reviewer_model_name,
        "memory_context": memory_context,
        "main_agent_saved_memory": False,
        "context_config": context_config or ContextConfig(),
        "context_report": None,
        "event_sink": event_sink,
        "run_id": run_id,
        "checkpoint_store": checkpoint_store,
        "session_id": session_id,
        "created_at": time.time(),
        "verifier_report": None,
        "verification_attempts": 0,
        "frozen_system_prompt": SYSTEM_PROMPT,
        "prompt_cache_report": None,
    }


async def run_agent(
    user_input: str,
    history: list[dict[str, Any]] | None,
    model_call: ModelCall,
    tool_selector: ToolSelector | None,
    tools: dict[str, dict[str, Any]] | None = None,
    max_turns: int = 10,
    blocking_token_limit: int = 120_000,
    stop_hook: StopHook | None = None,
    main_model_name: str = DEFAULT_MAIN_MODEL,
    subagent_model_name: str = DEFAULT_SUB_AGENT_MODEL,
    permission_reviewer: PermissionReviewer | None = None,
    permission_prompter: PermissionPrompter | None = None,
    reviewer_model_name: str = DEFAULT_SUB_AGENT_MODEL,
    memory_context: str | None = None,
    context_config: ContextConfig | None = None,
    checkpoint_store: CheckpointStore | None = None,
    session_id: str | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    registry = tools or get_tool_registry()
    event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    graph_state = _initial_graph_state(
        user_input=user_input,
        history=history,
        model_call=model_call,
        tool_selector=tool_selector,
        tools=registry,
        max_turns=max_turns,
        blocking_token_limit=blocking_token_limit,
        stop_hook=stop_hook,
        main_model_name=main_model_name,
        subagent_model_name=subagent_model_name,
        permission_reviewer=permission_reviewer,
        permission_prompter=permission_prompter,
        reviewer_model_name=reviewer_model_name,
        memory_context=memory_context,
        context_config=context_config,
        event_sink=event_queue.put_nowait,
        checkpoint_store=checkpoint_store,
        session_id=session_id,
    )

    yield state_event(
        _visible_state(graph_state),
        "初始化",
        token_usage=build_token_snapshot(
            messages=_request_messages(graph_state)[0],
            system_prompt=_system_prompt(graph_state),
            tools=_request_tool_specs(graph_state),
            blocking_token_limit=blocking_token_limit,
        ),
    )
    recursion_limit = _langgraph_recursion_limit(max_turns)
    logger.info(
        "run_agent start max_turns=%s recursion_limit=%s run_id=%s",
        max_turns,
        recursion_limit,
        graph_state["run_id"],
    )
    graph_task = asyncio.create_task(
        build_agent_graph().ainvoke(
            graph_state,
            {"recursion_limit": recursion_limit},
        )
    )
    try:
        while True:
            if graph_task.done() and event_queue.empty():
                break
            try:
                event = await asyncio.wait_for(event_queue.get(), timeout=0.05)
            except asyncio.TimeoutError:
                continue
            yield event
        await graph_task
    except GraphRecursionError as exc:
        logger.exception("langgraph recursion limit reached")
        yield terminal_event(
            _visible_state(graph_state),
            "max_turns",
            f"{TERMINATION_MESSAGES['max_turns']} LangGraph recursion limit reached: {exc}",
        )
    except UnicodeError as exc:
        logger.exception("image or unicode error")
        yield terminal_event(
            _visible_state(graph_state),
            "image_error",
            f"{TERMINATION_MESSAGES['image_error']} {exc}",
        )
    except Exception as exc:
        logger.exception("agent graph failed")
        yield terminal_event(
            _visible_state(graph_state),
            "model_error",
            f"{TERMINATION_MESSAGES['model_error']} {exc}",
        )
    finally:
        if not graph_task.done():
            graph_task.cancel()
