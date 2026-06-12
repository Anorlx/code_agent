from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncGenerator, Awaitable, Callable

from agent.fork.context import cache_safe_messages, fork_worker_prompt
from agent.main_agent.config import DEFAULT_SUB_AGENT_MODEL

ModelCall = Callable[..., AsyncGenerator[dict[str, Any], None]]

READ_ONLY_TOOL_NAMES = {
    "read_file",
    "list_dir",
    "ls_project",
    "grep_project",
    "read_project_file",
    "calculator",
    "current_time",
}


def _safe_worker_tools(tools: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    safe: dict[str, dict[str, Any]] = {}
    for name, info in tools.items():
        if name in READ_ONLY_TOOL_NAMES:
            safe[name] = info
    return safe


async def _all_safe_tools_selector(
    user_input: str,
    messages: list[dict[str, Any]],
    available_tools: dict[str, dict[str, Any]],
    model_name: str,
) -> list[str]:
    return list(available_tools)


async def run_fork_worker(
    *,
    task: dict[str, Any],
    shared_goal: str,
    parent_messages: list[dict[str, Any]],
    tools: dict[str, dict[str, Any]],
    model_call: ModelCall,
    model_name: str = DEFAULT_SUB_AGENT_MODEL,
    max_turns: int = 4,
) -> dict[str, Any]:
    from agent.main_agent.graph import run_agent

    worker_input = fork_worker_prompt(shared_goal, task)
    worker_tools = _safe_worker_tools(tools)
    started_at = time.time()
    text_parts: list[str] = []
    terminal_reason = ""
    last_state: dict[str, Any] | None = None

    async for event in run_agent(
        user_input=worker_input,
        history=cache_safe_messages(parent_messages),
        model_call=model_call,
        tool_selector=_all_safe_tools_selector,
        tools=worker_tools,
        max_turns=max_turns,
        main_model_name=model_name,
        subagent_model_name=model_name,
        permission_reviewer=None,
        permission_prompter=None,
    ):
        if event.get("type") == "assistant_delta":
            text_parts.append(str(event.get("content") or ""))
        elif event.get("type") == "terminal":
            terminal_reason = str(event.get("reason") or "")
        if event.get("state"):
            last_state = event["state"]

    return {
        "task_id": task.get("id") or task.get("title") or "fork-task",
        "title": task.get("title") or task.get("id") or "Fork task",
        "status": "completed" if terminal_reason in {"completed", ""} else terminal_reason,
        "duration_seconds": round(time.time() - started_at, 2),
        "result": "".join(text_parts).strip(),
        "messages_seen": len(last_state.get("messages", [])) if last_state else 0,
    }


async def run_fork_tasks(
    *,
    tasks: list[dict[str, Any]],
    shared_goal: str,
    parent_messages: list[dict[str, Any]],
    tools: dict[str, dict[str, Any]],
    model_call: ModelCall,
    model_name: str = DEFAULT_SUB_AGENT_MODEL,
    max_workers: int = 4,
    max_turns: int = 4,
) -> dict[str, Any]:
    selected_tasks = tasks[: max(1, min(max_workers, 6))]
    results = await asyncio.gather(
        *[
            run_fork_worker(
                task=task,
                shared_goal=shared_goal,
                parent_messages=parent_messages,
                tools=tools,
                model_call=model_call,
                model_name=model_name,
                max_turns=max_turns,
            )
            for task in selected_tasks
        ],
        return_exceptions=True,
    )
    normalized = []
    for task, result in zip(selected_tasks, results):
        if isinstance(result, Exception):
            normalized.append(
                {
                    "task_id": task.get("id") or task.get("title") or "fork-task",
                    "title": task.get("title") or task.get("id") or "Fork task",
                    "status": "failed",
                    "result": str(result),
                }
            )
        else:
            normalized.append(result)
    return {
        "mode": "fork",
        "shared_goal": shared_goal,
        "worker_count": len(normalized),
        "results": normalized,
    }
