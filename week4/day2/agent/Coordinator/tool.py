from __future__ import annotations

import json
from typing import Any

from agent.Coordinator.runner import run_coordinator_plan
from agent.main_agent.config import DEFAULT_SUB_AGENT_MODEL


def coordinator_plan_spec() -> dict[str, Any]:
    return {
        "name": "coordinator_plan",
        "description": (
            "Coordinator 模式：为复杂多阶段工程任务创建 research workers，并综合成实施规格。"
            "当前 v1 只执行 Research + Synthesis，不直接修改代码。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "复杂工程任务目标。"},
                "research_tasks": {
                    "type": "array",
                    "description": "Coordinator 分配给 research workers 的调查任务。",
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
                "synthesis_requirements": {
                    "type": "array",
                    "description": "综合阶段必须覆盖的信息。",
                    "items": {"type": "string"},
                },
                "expected_output": {
                    "type": "string",
                    "description": "最终 coordinator 应该产出的内容类型。",
                },
                "max_workers": {"type": "integer", "description": "最多 research worker 数，默认 4。"},
            },
            "required": ["task", "research_tasks"],
        },
    }


async def coordinator_plan(arguments: dict[str, Any], runtime_context: dict[str, Any]) -> dict[str, Any]:
    task = str(arguments.get("task") or runtime_context.get("user_input") or "").strip()
    raw_research_tasks = arguments.get("research_tasks") or []
    if not task:
        return {"ok": False, "error": "task is required."}
    if not isinstance(raw_research_tasks, list) or not raw_research_tasks:
        return {"ok": False, "error": "research_tasks must be a non-empty list."}
    research_tasks = [item for item in raw_research_tasks if isinstance(item, dict)]
    if not research_tasks:
        return {"ok": False, "error": "research_tasks must contain objects."}
    model_call = runtime_context.get("model_call")
    if model_call is None:
        return {"ok": False, "error": "model_call is missing from runtime context."}

    result = await run_coordinator_plan(
        task=task,
        research_tasks=research_tasks,
        parent_messages=list(runtime_context.get("messages") or []),
        tools=dict(runtime_context.get("tools") or {}),
        model_call=model_call,
        model_name=str(runtime_context.get("subagent_model_name") or runtime_context.get("model_name") or DEFAULT_SUB_AGENT_MODEL),
        max_workers=int(arguments.get("max_workers") or 4),
    )
    return {
        "ok": True,
        "content": json.dumps(
            {
                "scratchpad_dir": result["scratchpad_dir"],
                "scratchpad_files": result["scratchpad_files"],
                "implementation_spec": result["implementation_spec"],
                "synthesis_requirements": arguments.get("synthesis_requirements"),
                "expected_output": arguments.get("expected_output"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        "structured": {
            **result,
            "synthesis_requirements": arguments.get("synthesis_requirements"),
            "expected_output": arguments.get("expected_output"),
        },
    }
