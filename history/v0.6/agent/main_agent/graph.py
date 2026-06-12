from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncGenerator, Awaitable, Callable, Literal

from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from agent.main_agent.config import DEFAULT_MAIN_MODEL, DEFAULT_SUB_AGENT_MODEL
from agent.main_agent.context_manager import ContextConfig, manage_context, snip_tool_results
from agent.main_agent.state import new_state, state_event, terminal_event
from agent.main_agent.token_usage import build_real_usage_snapshot, build_token_snapshot, estimate_tokens
from agent.sub_agent.tool_runner import PermissionReviewer, run_tool_subagent
from agent.sub_agent.tool_search import select_tools
from agent.tools.registry import dashscope_tool_specs, get_tool_registry

SYSTEM_PROMPT = """你是一个从零开始逐步扩展的 Python Agent。
你可以正常对话，也可以在需要时调用工具。
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
    reviewer_model_name: str
    event_sink: Callable[[dict[str, Any]], None] | None
    memory_context: str | None
    main_agent_saved_memory: bool
    context_config: ContextConfig
    context_report: dict[str, Any] | None


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
    memory_context = str(state.get("memory_context") or "").strip()
    if not memory_context:
        return SYSTEM_PROMPT
    return f"{SYSTEM_PROMPT}\n\n# Long-term memory\n\n{memory_context}"


def _visible_state(state: AgentGraphState) -> dict[str, Any]:
    return {
        "turn": state.get("turn", 0),
        "phase": state.get("phase", "初始化"),
        "messages": list(state.get("messages", [])),
        "selected_tools": list(state.get("selected_tools", [])),
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
            messages=state.get("messages", []),
            system_prompt=_system_prompt(state),
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
    selected_tools = _coerce_selected_tools(selected, state["tools"])
    logger.info("preprocess selected_tools=%s turn=%s", selected_tools, next_state["turn"])

    return {
        "turn": next_state["turn"],
        "phase": "预处理",
        "messages": next_state["messages"],
        "context_report": context_report,
        "selected_tools": selected_tools,
        "tool_calls": [],
        "tool_results": [],
    }


async def _api_call_node(state: AgentGraphState) -> dict[str, Any]:
    tool_specs = dashscope_tool_specs(state.get("selected_tools", []), state["tools"])
    _emit_state(
        state,
        "API调用",
        selected_tools=state.get("selected_tools", []),
        token_usage=build_token_snapshot(
            messages=state["messages"],
            system_prompt=_system_prompt(state),
            tools=tool_specs,
            blocking_token_limit=state["blocking_token_limit"],
        ),
    )
    logger.info("api_call start turn=%s tools=%s", state.get("turn"), state.get("selected_tools", []))

    assistant_text = ""
    tool_calls: list[dict[str, Any]] = []
    model_usage: dict[str, Any] | None = None
    try:
        async for event in state["model_call"](
            messages=state["messages"],
            system_prompt=_system_prompt(state),
            tools=tool_specs,
            model_name=state["main_model_name"],
        ):
            if event.get("type") == "assistant_delta":
                assistant_text += event.get("content", "")
                _emit(state, event)
            elif event.get("type") == "tool_call":
                tool_calls.append(event["tool_call"])
                _emit(state, event)
            elif event.get("type") == "token_usage":
                model_usage = event.get("token_usage", {})
    except KeyboardInterrupt:
        logger.info("api_call aborted by user")
        return _terminal_update(state, "aborted_streaming", TERMINATION_MESSAGES["aborted_streaming"])
    except Exception as exc:
        logger.exception("api_call failed")
        return _terminal_update(state, "model_error", f"{TERMINATION_MESSAGES['model_error']} {exc}")

    messages = list(state["messages"])
    messages.append(_assistant_message(assistant_text, tool_calls))
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
                    messages=state["messages"],
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
        "phase": "API调用",
    }


async def _termination_check_node(state: AgentGraphState) -> dict[str, Any]:
    _emit_state(state, "终止检查")
    stop_hook = state.get("stop_hook")
    if stop_hook and stop_hook(_visible_state(state)):
        logger.info("stop_hook prevented completion")
        return _terminal_update(
            state,
            "stop_hook_prevented",
            TERMINATION_MESSAGES["stop_hook_prevented"],
        )
    logger.info("agent completed turn=%s", state.get("turn"))
    return _terminal_update(state, "completed", TERMINATION_MESSAGES["completed"])


async def _tool_execution_node(state: AgentGraphState) -> dict[str, Any]:
    tool_calls = state.get("tool_calls", [])
    _emit_state(state, "工具执行", tool_calls=tool_calls)
    logger.info("tool_execution start calls=%s", len(tool_calls))

    tool_messages: list[dict[str, Any]] = []
    try:
        async for tool_event in run_tool_subagent(
            user_input=state["user_input"],
            messages=state["messages"],
            tool_calls=tool_calls,
            tools=state["tools"],
            permission_reviewer=state.get("permission_reviewer"),
            reviewer_model_name=state["reviewer_model_name"],
            memory_context=state.get("memory_context"),
        ):
            if tool_event["type"] == "tool_result":
                tool_messages.append(tool_event["message"])
            _emit(state, tool_event)
    except KeyboardInterrupt:
        logger.info("tool_execution aborted by user")
        return _terminal_update(state, "aborted_tools", TERMINATION_MESSAGES["aborted_tools"])

    logger.info("tool_execution done results=%s", len(tool_messages))
    return {
        "tool_results": tool_messages,
        "phase": "工具执行",
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


def _route_after_preprocess(state: AgentGraphState) -> Literal["api_call", "__end__"]:
    if state.get("termination_reason"):
        return END
    return "api_call"


def _route_after_api_call(state: AgentGraphState) -> Literal["tool_execution", "termination_check", "__end__"]:
    if state.get("termination_reason"):
        return END
    if state.get("tool_calls"):
        return "tool_execution"
    return "termination_check"


def _route_after_tool_execution(state: AgentGraphState) -> Literal["result_backfill", "__end__"]:
    if state.get("termination_reason"):
        return END
    return "result_backfill"


def _route_after_result_backfill(state: AgentGraphState) -> Literal["preprocess", "__end__"]:
    if state.get("termination_reason"):
        return END
    return "preprocess"


def build_agent_graph():
    graph = StateGraph(AgentGraphState)
    graph.add_node("preprocess", _preprocess_node)
    graph.add_node("api_call", _api_call_node)
    graph.add_node("termination_check", _termination_check_node)
    graph.add_node("tool_execution", _tool_execution_node)
    graph.add_node("result_backfill", _result_backfill_node)

    graph.add_edge(START, "preprocess")
    graph.add_conditional_edges("preprocess", _route_after_preprocess)
    graph.add_conditional_edges("api_call", _route_after_api_call)
    graph.add_edge("termination_check", END)
    graph.add_conditional_edges("tool_execution", _route_after_tool_execution)
    graph.add_conditional_edges("result_backfill", _route_after_result_backfill)
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
    reviewer_model_name: str,
    memory_context: str | None = None,
    event_sink: Callable[[dict[str, Any]], None] | None = None,
    context_config: ContextConfig | None = None,
) -> AgentGraphState:
    state = new_state(user_input, history)
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
        "reviewer_model_name": reviewer_model_name,
        "memory_context": memory_context,
        "main_agent_saved_memory": False,
        "context_config": context_config or ContextConfig(),
        "context_report": None,
        "event_sink": event_sink,
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
    reviewer_model_name: str = DEFAULT_SUB_AGENT_MODEL,
    memory_context: str | None = None,
    context_config: ContextConfig | None = None,
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
        reviewer_model_name=reviewer_model_name,
        memory_context=memory_context,
        context_config=context_config,
        event_sink=event_queue.put_nowait,
    )

    yield state_event(
        _visible_state(graph_state),
        "初始化",
        token_usage=build_token_snapshot(
            messages=graph_state.get("messages", []),
            system_prompt=_system_prompt(graph_state),
            blocking_token_limit=blocking_token_limit,
        ),
    )
    recursion_limit = _langgraph_recursion_limit(max_turns)
    logger.info(
        "run_agent start max_turns=%s recursion_limit=%s",
        max_turns,
        recursion_limit,
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
