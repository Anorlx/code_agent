from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, AsyncGenerator, Callable

from agent.main_agent.config import DEFAULT_SUB_AGENT_MODEL, MEMORY_ROOT
from agent.sub_agent.memory_writer import run_memory_writer

ModelCall = Callable[..., AsyncGenerator[dict[str, Any], None]]


class MemoryObserver:
    def __init__(
        self,
        memory_root: Path | None = None,
        model_call: ModelCall | None = None,
        model_name: str = DEFAULT_SUB_AGENT_MODEL,
        interval: int = 3,
    ) -> None:
        self.memory_root = memory_root or MEMORY_ROOT
        self.model_call = model_call
        self.model_name = model_name
        self.interval = max(1, interval)
        self.completed_turns = 0
        self.tasks: set[asyncio.Task[dict[str, Any]]] = set()

    def should_run(self) -> bool:
        return self.completed_turns > 0 and self.completed_turns % self.interval == 0

    def observe(
        self,
        messages: list[dict[str, Any]],
        main_agent_saved_memory: bool = False,
    ) -> None:
        self.completed_turns += 1
        should_run = self.should_run()
        if not should_run and not main_agent_saved_memory:
            return

        task = asyncio.create_task(
            run_memory_writer(
                messages=messages,
                memory_root=self.memory_root,
                model_call=self.model_call,
                model_name=self.model_name,
                should_run=should_run,
                main_agent_saved_memory=main_agent_saved_memory,
            )
        )
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def drain(self) -> list[dict[str, Any]]:
        if not self.tasks:
            return []
        results = await asyncio.gather(*list(self.tasks), return_exceptions=True)
        self.tasks.clear()
        return [result for result in results if isinstance(result, dict)]
