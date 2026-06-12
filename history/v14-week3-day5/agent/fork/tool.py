from __future__ import annotations

import json
from typing import Any

from agent.fork.runner import run_fork_tasks
from agent.main_agent.config import DEFAULT_SUB_AGENT_MODEL


def fork_tasks_spec() -> dict[str, Any]:
    return {
        "name": "fork_tasks",
        "description": (
            "Fork 模式：并行运行多个只读短生命周期 worker，适合独立模块分析、并行搜索和方案比较。"
            "不适合复杂多阶段实现或并发写文件。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "shared_goal": {"type": "string", "description": "所有 fork worker 共同服务的总目标。"},
                "tasks": {
                    "type": "array",
                    "description": "互相独立的子任务列表。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "title": {"type": "string"},
                            "instruction": {"type": "string"},
                        },
                        "required": ["title", "instruction"],
                    },
                },
                "max_workers": {"type": "integer", "description": "最多并行 worker 数，默认 4，最大 6。"},
            },
            "required": ["shared_goal", "tasks"],
        },
    }


async def fork_tasks(arguments: dict[str, Any], runtime_context: dict[str, Any]) -> dict[str, Any]:
    raw_tasks = arguments.get("tasks") or []
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return {"ok": False, "error": "tasks must be a non-empty list."}
    tasks = [task for task in raw_tasks if isinstance(task, dict)]
    if not tasks:
        return {"ok": False, "error": "tasks must contain objects."}
    model_call = runtime_context.get("model_call")
    if model_call is None:
        return {"ok": False, "error": "model_call is missing from runtime context."}

    result = await run_fork_tasks(
        tasks=tasks,
        shared_goal=str(arguments.get("shared_goal") or runtime_context.get("user_input") or ""),
        parent_messages=list(runtime_context.get("messages") or []),
        tools=dict(runtime_context.get("tools") or {}),
        model_call=model_call,
        model_name=str(runtime_context.get("subagent_model_name") or runtime_context.get("model_name") or DEFAULT_SUB_AGENT_MODEL),
        max_workers=int(arguments.get("max_workers") or 4),
    )
    return {
        "ok": True,
        "content": json.dumps(result, ensure_ascii=False, indent=2),
        "structured": result,
    }
