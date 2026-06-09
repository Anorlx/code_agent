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
    ) -> None:
        self.memory_root = memory_root or MEMORY_ROOT
        self.model_call = model_call
        self.model_name = model_name
        self.completed_turns = 0
        self.tasks: set[asyncio.Task[dict[str, Any]]] = set()
        self.last_observed_signature = ""
        self._write_lock = asyncio.Lock()

    def _signature(self, messages: list[dict[str, Any]]) -> str:
        if not messages:
            return "empty"
        last = messages[-1]
        return "|".join(
            [
                str(len(messages)),
                str(last.get("role", "")),
                str(last.get("created_at", "")),
                str(last.get("content", ""))[:120],
            ]
        )

    def observe(
        self,
        messages: list[dict[str, Any]],
        main_agent_saved_memory: bool = False,
    ) -> None:
        self.completed_turns += 1
        if main_agent_saved_memory:
            self.last_observed_signature = self._signature(messages)
            return

        signature = self._signature(messages)
        if signature == self.last_observed_signature:
            return
        self.last_observed_signature = signature

        task = asyncio.create_task(self._run_writer(messages, main_agent_saved_memory))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _run_writer(
        self,
        messages: list[dict[str, Any]],
        main_agent_saved_memory: bool,
    ) -> dict[str, Any]:
        async with self._write_lock:
            return await run_memory_writer(
                messages=messages,
                memory_root=self.memory_root,
                model_call=self.model_call,
                model_name=self.model_name,
                main_agent_saved_memory=main_agent_saved_memory,
            )

    async def flush(
        self,
        messages: list[dict[str, Any]],
        main_agent_saved_memory: bool = False,
    ) -> list[dict[str, Any]]:
        self.observe(messages, main_agent_saved_memory=main_agent_saved_memory)
        return await self.drain()

    async def drain(self) -> list[dict[str, Any]]:
        if not self.tasks:
            return []
        results = await asyncio.gather(*list(self.tasks), return_exceptions=True)
        self.tasks.clear()
        return [result for result in results if isinstance(result, dict)]
