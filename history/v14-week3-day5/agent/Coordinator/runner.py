from __future__ import annotations

import json
from typing import Any, AsyncGenerator, Callable

from agent.Coordinator.prompts import COORDINATOR_SYNTHESIS_PROMPT, synthesis_user_prompt
from agent.Coordinator.scratchpad import new_scratchpad_dir, research_markdown, write_markdown
from agent.fork.runner import run_fork_tasks
from agent.main_agent.config import DEFAULT_SUB_AGENT_MODEL

ModelCall = Callable[..., AsyncGenerator[dict[str, Any], None]]


async def _synthesize(
    *,
    task: str,
    research_results: list[dict[str, Any]],
    scratchpad_files: list[str],
    model_call: ModelCall,
    model_name: str,
) -> str:
    content = ""
    async for event in model_call(
        messages=[
            {
                "role": "user",
                "content": synthesis_user_prompt(task, research_results, scratchpad_files),
            }
        ],
        system_prompt=COORDINATOR_SYNTHESIS_PROMPT,
        tools=[],
        model_name=model_name,
    ):
        if event.get("type") == "assistant_delta":
            content += str(event.get("content") or "")
    return content.strip()


async def run_coordinator_plan(
    *,
    task: str,
    research_tasks: list[dict[str, Any]],
    parent_messages: list[dict[str, Any]],
    tools: dict[str, dict[str, Any]],
    model_call: ModelCall,
    model_name: str = DEFAULT_SUB_AGENT_MODEL,
    max_workers: int = 4,
) -> dict[str, Any]:
    scratchpad_dir = new_scratchpad_dir(task)
    research = await run_fork_tasks(
        tasks=research_tasks,
        shared_goal=task,
        parent_messages=parent_messages,
        tools=tools,
        model_call=model_call,
        model_name=model_name,
        max_workers=max_workers,
        max_turns=4,
    )
    scratchpad_files: list[str] = []
    for result in research["results"]:
        filename = f"research-{result.get('task_id', 'worker')}.md"
        path = write_markdown(
            scratchpad_dir / filename,
            str(result.get("title") or result.get("task_id") or "Research"),
            research_markdown(result),
        )
        scratchpad_files.append(path.as_posix())

    synthesis = await _synthesize(
        task=task,
        research_results=list(research["results"]),
        scratchpad_files=scratchpad_files,
        model_call=model_call,
        model_name=model_name,
    )
    spec_path = write_markdown(scratchpad_dir / "implementation-spec.md", "Implementation Spec", synthesis)
    scratchpad_files.append(spec_path.as_posix())
    result = {
        "mode": "coordinator",
        "task": task,
        "phase": "research+synthesis",
        "scratchpad_dir": scratchpad_dir.as_posix(),
        "scratchpad_files": scratchpad_files,
        "research": research,
        "implementation_spec": synthesis,
    }
    write_markdown(
        scratchpad_dir / "coordinator-output.md",
        "Coordinator Output",
        json.dumps(result, ensure_ascii=False, indent=2),
    )
    return result
