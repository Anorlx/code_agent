from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, AsyncGenerator, Callable

from agent.main_agent.config import DEFAULT_SUB_AGENT_MODEL, MEMORY_ROOT
from agent.memory_system.advanced import PersonalMemorySystem
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
        self.personal_memory = PersonalMemorySystem(self.memory_root / "memory.sqlite3")

    def record_messages(self, session_id: str, messages: list[dict[str, Any]]) -> list[str]:
        """Persist the uncompressed trace before the next preprocess can compact it."""
        return self.personal_memory.record_messages(session_id, messages)

    def _signature(self, messages: list[dict[str, Any]], session_id: str | None = None) -> str:
        if not messages:
            return f"{session_id or ''}|empty"
        last = messages[-1]
        return "|".join(
            [
                str(session_id or ""),
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
        session_id: str | None = None,
    ) -> None:
        self.completed_turns += 1
        if main_agent_saved_memory:
            self.last_observed_signature = self._signature(messages, session_id)
            return

        signature = self._signature(messages, session_id)
        if signature == self.last_observed_signature:
            return
        self.last_observed_signature = signature

        task = asyncio.create_task(self._run_writer(messages, main_agent_saved_memory, session_id))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _run_writer(
        self,
        messages: list[dict[str, Any]],
        main_agent_saved_memory: bool,
        session_id: str | None,
    ) -> dict[str, Any]:
        async with self._write_lock:
            if session_id:
                return await self.personal_memory.process_session(session_id)
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
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if session_id:
            self.record_messages(session_id, messages)
        self.observe(
            messages,
            main_agent_saved_memory=main_agent_saved_memory,
            session_id=session_id,
        )
        return await self.drain()

    async def drain(self) -> list[dict[str, Any]]:
        if not self.tasks:
            return []
        results = await asyncio.gather(*list(self.tasks), return_exceptions=True)
        self.tasks.clear()
        return [result for result in results if isinstance(result, dict)]
