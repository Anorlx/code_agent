from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from agent.hooks import HookManager
from agent.sub_agent.tool_runner import PermissionPrompter, PermissionReviewer, run_tool_subagent

logger = logging.getLogger(__name__)

QueryEvent = dict[str, Any]
ToolStatus = str


def _tool_arguments(tool_call: dict[str, Any]) -> dict[str, Any]:
    raw_args = tool_call.get("arguments") or tool_call.get("function", {}).get("arguments") or {}
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args or "{}")
        except json.JSONDecodeError:
            return {"raw": raw_args}
        return parsed if isinstance(parsed, dict) else {}
    return raw_args if isinstance(raw_args, dict) else {}


def _is_side_effectful_tool(name: str | None, tools: dict[str, dict[str, Any]]) -> bool:
    if not name or name not in tools:
        return True
    return bool(tools[name].get("side_effectful", not tools[name].get("parallel_safe", False)))


class StreamingToolExecutor:
    @dataclass
    class _TrackedTool:
        sequence: int
        tool_call: dict[str, Any]
        name: str | None
        parallel_safe: bool
        side_effectful: bool
        effective_arguments: dict[str, Any] | None = None
        started_at: float | None = None
        finished_at: float | None = None
        status: ToolStatus = "queued"
        task: asyncio.Task[None] | None = None
        result_event: QueryEvent | None = None

    def __init__(
        self,
        *,
        user_input: str,
        messages: list[dict[str, Any]],
        tools: dict[str, dict[str, Any]],
        permission_reviewer: PermissionReviewer | None,
        permission_prompter: PermissionPrompter | None,
        reviewer_model_name: str,
        memory_context: str | None,
        runtime_context: dict[str, Any],
        hook_manager: HookManager | None = None,
        session_id: str = "",
        run_id: str = "",
    ) -> None:
        self._user_input = user_input
        self._messages = messages
        self._tools = tools
        self._permission_reviewer = permission_reviewer
        self._permission_prompter = permission_prompter
        self._reviewer_model_name = reviewer_model_name
        self._memory_context = memory_context
        self._runtime_context = runtime_context
        self._hook_manager = hook_manager
        self._session_id = session_id
        self._run_id = run_id
        self._queue: asyncio.Queue[QueryEvent] = asyncio.Queue()
        self._tracked: list[StreamingToolExecutor._TrackedTool] = []
        self.results: list[dict[str, Any]] = []

    def submit(self, tool_call: dict[str, Any]) -> None:
        name = tool_call.get("name") or tool_call.get("function", {}).get("name")
        parallel_safe = bool(name in self._tools and self._tools[name].get("parallel_safe", False))
        side_effectful = _is_side_effectful_tool(name, self._tools)
        tracked = self._TrackedTool(
            sequence=len(self._tracked),
            tool_call=tool_call,
            name=name,
            parallel_safe=parallel_safe,
            side_effectful=side_effectful,
        )
        self._tracked.append(tracked)
        self._queue.put_nowait(
            {
                "type": "tool_status",
                "name": name,
                "status": "queued",
                "sequence": tracked.sequence,
                "parallel_safe": parallel_safe,
                "side_effectful": side_effectful,
            }
        )
        self._process_queue()

    def checkpoint_tool_states(self) -> list[dict[str, Any]]:
        states = []
        for tracked in self._tracked:
            message = (tracked.result_event or {}).get("message") or {}
            result = message.get("raw_result") if isinstance(message, dict) else None
            result_arguments = message.get("arguments") if isinstance(message, dict) else None
            arguments = (
                result_arguments
                if isinstance(result_arguments, dict)
                else tracked.effective_arguments
                if tracked.effective_arguments is not None
                else _tool_arguments(tracked.tool_call)
            )
            states.append(
                {
                    "tool_call_id": tracked.tool_call.get("id", tracked.name),
                    "name": tracked.name,
                    "arguments": copy.deepcopy(arguments),
                    "status": tracked.status,
                    "parallel_safe": tracked.parallel_safe,
                    "side_effectful": tracked.side_effectful,
                    "started_at": tracked.started_at,
                    "finished_at": tracked.finished_at,
                    "result": result if tracked.status in {"completed", "yielded"} else None,
                    "error": result.get("error") if isinstance(result, dict) else None,
                }
            )
        return states

    async def drain_ready(self) -> list[QueryEvent]:
        self._flush_completed_results()
        events = []
        while True:
            try:
                events.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return events

    async def finish(self) -> list[QueryEvent]:
        events: list[QueryEvent] = []
        pending = {
            tracked.task
            for tracked in self._tracked
            if tracked.task is not None and not tracked.task.done()
        }
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
            self._process_queue()
            events.extend(await self.drain_ready())
            pending = {
                tracked.task
                for tracked in self._tracked
                if tracked.task is not None and not tracked.task.done()
            }
        events.extend(await self.drain_ready())
        return events

    def _can_execute_tool(self, tracked: _TrackedTool) -> bool:
        executing = [item for item in self._tracked if item.status == "executing"]
        if not executing:
            return True
        return tracked.parallel_safe and all(item.parallel_safe for item in executing)

    def _process_queue(self) -> None:
        started = True
        while started:
            started = False
            for tracked in self._tracked:
                if tracked.status != "queued" or not self._can_execute_tool(tracked):
                    continue
                tracked.status = "executing"
                tracked.started_at = time.time()
                tracked.task = asyncio.create_task(self._consume_tool_stream(tracked))
                self._queue.put_nowait(
                    {
                        "type": "tool_status",
                        "name": tracked.name,
                        "status": "executing",
                        "sequence": tracked.sequence,
                        "parallel_safe": tracked.parallel_safe,
                        "side_effectful": tracked.side_effectful,
                    }
                )
                started = True

    def _flush_completed_results(self) -> None:
        for tracked in self._tracked:
            if tracked.status == "yielded":
                continue
            if tracked.status == "cancelled":
                tracked.status = "yielded"
                continue
            if tracked.status != "completed":
                break
            if tracked.result_event is not None:
                self.results.append(tracked.result_event["message"])
                self._queue.put_nowait(tracked.result_event)
            tracked.status = "yielded"
            self._queue.put_nowait(
                {
                    "type": "tool_status",
                    "name": tracked.name,
                    "status": "yielded",
                    "sequence": tracked.sequence,
                    "parallel_safe": tracked.parallel_safe,
                    "side_effectful": tracked.side_effectful,
                }
            )

    def _is_bash_like_tool(self, tracked: _TrackedTool) -> bool:
        return tracked.name in {"run_command", "bash", "Bash"}

    def _tool_result_failed(self, event: QueryEvent | None) -> bool:
        if not event or event.get("type") != "tool_result":
            return False
        message = event.get("message") or {}
        raw_result = message.get("raw_result") or {}
        if isinstance(raw_result, dict) and raw_result.get("ok") is False:
            return True
        return str(message.get("content") or "").startswith("ERROR:")

    def _cancel_executing_siblings(self, failed: _TrackedTool) -> None:
        for tracked in self._tracked:
            if tracked is failed or tracked.status != "executing":
                continue
            if tracked.task is not None:
                tracked.task.cancel()

    def _cancelled_result_event(self, tracked: _TrackedTool, reason: str) -> QueryEvent:
        arguments = (
            tracked.effective_arguments
            if tracked.effective_arguments is not None
            else _tool_arguments(tracked.tool_call)
        )
        result = {
            "ok": False,
            "error": reason,
            "cancelled": True,
        }
        return {
            "type": "tool_result",
            "message": {
                "role": "tool",
                "tool_call_id": tracked.tool_call.get("id", tracked.name),
                "name": tracked.name,
                "arguments": copy.deepcopy(arguments),
                "summary": "cancelled",
                "content": f"ERROR: {reason}",
                "raw_result": result,
                "created_at": time.time(),
            },
        }

    def _consume_internal_event(
        self,
        tracked: _TrackedTool,
        event: QueryEvent,
    ) -> None:
        if event.get("type") != "_tool_effective_arguments":
            return
        arguments = event.get("arguments")
        if isinstance(arguments, dict):
            tracked.effective_arguments = copy.deepcopy(arguments)

    async def _consume_tool_stream(self, tracked: _TrackedTool) -> None:
        try:
            async for event in run_tool_subagent(
                user_input=self._user_input,
                messages=self._messages,
                tool_calls=[tracked.tool_call],
                tools=self._tools,
                permission_reviewer=self._permission_reviewer,
                reviewer_model_name=self._reviewer_model_name,
                permission_prompter=self._permission_prompter,
                memory_context=self._memory_context,
                runtime_context=self._runtime_context,
                hook_manager=self._hook_manager,
                session_id=self._session_id,
                run_id=self._run_id,
                _internal_event_sink=lambda event: self._consume_internal_event(
                    tracked,
                    event,
                ),
            ):
                if event.get("type") == "tool_result":
                    tracked.result_event = event
                    continue
                if event.get("type") == "tool_start" and isinstance(
                    event.get("arguments"), dict
                ):
                    tracked.effective_arguments = copy.deepcopy(event["arguments"])
                await self._queue.put(event)
        except asyncio.CancelledError:
            tracked.status = "completed"
            tracked.result_event = self._cancelled_result_event(
                tracked,
                "Cancelled because a sibling Bash/run_command tool failed.",
            )
            self._queue.put_nowait(
                {
                    "type": "tool_status",
                    "name": tracked.name,
                    "status": "cancelled",
                    "sequence": tracked.sequence,
                    "parallel_safe": tracked.parallel_safe,
                    "side_effectful": tracked.side_effectful,
                }
            )
            self._flush_completed_results()
            return
        except Exception as exc:
            tracked.result_event = self._cancelled_result_event(tracked, f"Tool execution crashed: {exc}")
        finally:
            if tracked.status == "executing":
                tracked.status = "completed"
                tracked.finished_at = time.time()
                self._queue.put_nowait(
                    {
                        "type": "tool_status",
                        "name": tracked.name,
                        "status": "completed",
                        "sequence": tracked.sequence,
                        "parallel_safe": tracked.parallel_safe,
                        "side_effectful": tracked.side_effectful,
                    }
                )
            if self._is_bash_like_tool(tracked) and self._tool_result_failed(tracked.result_event):
                self._cancel_executing_siblings(tracked)
            self._flush_completed_results()
            self._process_queue()
