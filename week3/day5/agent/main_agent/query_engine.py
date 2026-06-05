from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Awaitable, Callable

from agent.main_agent.checkpoint_store import CheckpointStore
from agent.main_agent.config import DEFAULT_MAIN_MODEL, DEFAULT_SUB_AGENT_MODEL
from agent.main_agent.context_manager import ContextConfig, manage_context, snip_tool_results
from agent.main_agent.graph import (
    ModelCall,
    PermissionPrompter,
    PermissionReviewer,
    StopHook,
    SYSTEM_PROMPT,
    TERMINATION_MESSAGES,
    ToolSelector,
)
from agent.main_agent.state import new_state, state_event, terminal_event
from agent.main_agent.token_usage import build_real_usage_snapshot, build_token_snapshot, estimate_tokens
from agent.sub_agent.tool_runner import run_tool_subagent
from agent.sub_agent.tool_search import select_tools
from agent.tools.registry import dashscope_tool_specs, get_tool_registry

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


def _assistant_message(content: str, tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content, "created_at": time.time()}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _visible_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": state.get("run_id"),
        "turn": state.get("turn", 0),
        "phase": state.get("phase", "初始化"),
        "messages": list(state.get("messages", [])),
        "selected_tools": list(state.get("selected_tools", [])),
        "tool_calls": list(state.get("tool_calls", [])),
        "tool_results": list(state.get("tool_results", [])),
        "termination_reason": state.get("termination_reason"),
        "main_agent_saved_memory": bool(state.get("main_agent_saved_memory", False)),
    }


def _system_prompt(memory_context: str | None) -> str:
    context = str(memory_context or "").strip()
    if not context:
        return SYSTEM_PROMPT
    return f"{SYSTEM_PROMPT}\n\n# Long-term memory\n\n{context}"


def _message_token_estimate(messages: list[dict[str, Any]]) -> int:
    return estimate_tokens(messages)


def _coerce_selected_tools(selected: Any, available_tools: dict[str, dict[str, Any]]) -> list[str]:
    if not isinstance(selected, list):
        return []
    return [
        name
        for name in selected
        if isinstance(name, str) and name in available_tools
    ]


async def _default_selector(
    user_input: str,
    messages: list[dict[str, Any]],
    tools: dict[str, dict[str, Any]],
    model_name: str,
) -> list[str]:
    return await select_tools(
        user_input=user_input,
        messages=messages,
        available_tools=tools,
        model_name=model_name,
    )


class StreamingToolExecutor:
    @dataclass
    class _TrackedTool:
        sequence: int
        tool_call: dict[str, Any]
        name: str | None
        parallel_safe: bool
        side_effectful: bool
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
    ) -> None:
        self._user_input = user_input
        self._messages = messages
        self._tools = tools
        self._permission_reviewer = permission_reviewer
        self._permission_prompter = permission_prompter
        self._reviewer_model_name = reviewer_model_name
        self._memory_context = memory_context
        self._runtime_context = runtime_context
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
            states.append(
                {
                    "tool_call_id": tracked.tool_call.get("id", tracked.name),
                    "name": tracked.name,
                    "arguments": _tool_arguments(tracked.tool_call),
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
                "arguments": tracked.tool_call.get("arguments") or {},
                "summary": "cancelled",
                "content": f"ERROR: {reason}",
                "raw_result": result,
                "created_at": time.time(),
            },
        }

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
            ):
                if event.get("type") == "tool_result":
                    tracked.result_event = event
                    continue
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


class QueryEngine:
    def __init__(
        self,
        *,
        model_call: ModelCall,
        tools: dict[str, dict[str, Any]] | None = None,
        tool_selector: ToolSelector | None = None,
        permission_reviewer: PermissionReviewer | None = None,
        permission_prompter: PermissionPrompter | None = None,
        stop_hook: StopHook | None = None,
        main_model_name: str = DEFAULT_MAIN_MODEL,
        subagent_model_name: str = DEFAULT_SUB_AGENT_MODEL,
        reviewer_model_name: str = DEFAULT_SUB_AGENT_MODEL,
        max_turns: int = 10,
        blocking_token_limit: int = 120_000,
        context_config: ContextConfig | None = None,
        checkpoint_store: CheckpointStore | None = None,
        session_id: str | None = None,
    ) -> None:
        self.model_call = model_call
        self.tools = tools or get_tool_registry()
        self.tool_selector = tool_selector or _default_selector
        self.permission_reviewer = permission_reviewer
        self.permission_prompter = permission_prompter
        self.stop_hook = stop_hook
        self.main_model_name = main_model_name
        self.subagent_model_name = subagent_model_name
        self.reviewer_model_name = reviewer_model_name
        self.max_turns = max_turns
        self.blocking_token_limit = blocking_token_limit
        self.context_config = context_config or ContextConfig()
        self.checkpoint_store = checkpoint_store
        self.session_id = session_id

    def _checkpoint_payload(
        self,
        *,
        state: dict[str, Any],
        run_id: str,
        user_input: str,
        assistant_text: str = "",
        submitted_tool_ids: set[str] | None = None,
        memory_context: str | None = None,
        tool_executor: StreamingToolExecutor | None = None,
        created_at: float,
    ) -> dict[str, Any]:
        tool_states = tool_executor.checkpoint_tool_states() if tool_executor is not None else []
        return {
            "session_id": self.session_id,
            "run_id": run_id,
            "user_input": user_input,
            "turn": state.get("turn", 0),
            "phase": state.get("phase", "初始化"),
            "messages": list(state.get("messages", [])),
            "selected_tools": list(state.get("selected_tools", [])),
            "tool_calls": list(state.get("tool_calls", [])),
            "tool_results": list(state.get("tool_results", [])),
            "tool_states": tool_states,
            "assistant_text": assistant_text,
            "submitted_tool_ids": sorted(submitted_tool_ids or set()),
            "main_agent_saved_memory": bool(state.get("main_agent_saved_memory", False)),
            "memory_context": memory_context or "",
            "created_at": created_at,
            "updated_at": time.time(),
        }

    async def _save_checkpoint(
        self,
        *,
        state: dict[str, Any],
        run_id: str,
        user_input: str,
        assistant_text: str = "",
        submitted_tool_ids: set[str] | None = None,
        memory_context: str | None = None,
        tool_executor: StreamingToolExecutor | None = None,
        created_at: float,
        status: str = "running",
    ) -> None:
        if self.checkpoint_store is None or not self.session_id:
            return
        payload = self._checkpoint_payload(
            state=state,
            run_id=run_id,
            user_input=user_input,
            assistant_text=assistant_text,
            submitted_tool_ids=submitted_tool_ids,
            memory_context=memory_context,
            tool_executor=tool_executor,
            created_at=created_at,
        )
        await self.checkpoint_store.save_checkpoint(
            session_id=self.session_id,
            run_id=run_id,
            turn=int(state.get("turn", 0) or 0),
            phase=str(state.get("phase") or "初始化"),
            state=payload,
            status=status,
        )

    async def _mark_checkpoint_terminal(
        self,
        run_id: str,
        status: str,
        *,
        reason: str = "",
        state: dict[str, Any] | None = None,
    ) -> None:
        if self.checkpoint_store is None or not self.session_id:
            return
        has_full_checkpoint_payload = bool(
            isinstance(state, dict)
            and state.get("run_id")
            and "user_input" in state
            and "tool_states" in state
        )
        if status == "completed":
            await self.checkpoint_store.mark_completed(run_id)
        elif status == "aborted":
            if state is not None and has_full_checkpoint_payload:
                payload = dict(state)
                if reason:
                    payload["checkpoint_error"] = reason
                await self.checkpoint_store.mark_status(run_id, "aborted", state=payload)
            else:
                await self.checkpoint_store.mark_aborted(run_id, reason)
        elif status == "failed":
            if state is not None and has_full_checkpoint_payload:
                payload = dict(state)
                if reason:
                    payload["checkpoint_error"] = reason
                await self.checkpoint_store.mark_status(run_id, "failed", state=payload)
            else:
                await self.checkpoint_store.mark_failed(run_id, reason)
        elif status in {"needs_review", "unknown_outcome"}:
            await self.checkpoint_store.mark_status(run_id, status, state=state)

    async def submit_message(
        self,
        user_input: str,
        *,
        history: list[dict[str, Any]] | None = None,
        memory_context: str | None = None,
    ) -> AsyncGenerator[QueryEvent, None]:
        run_id = uuid.uuid4().hex
        checkpoint_created_at = time.time()
        state = {
            **new_state(user_input, history),
            "main_agent_saved_memory": False,
            "tool_calls": [],
            "tool_results": [],
            "run_id": run_id,
        }
        system_prompt = _system_prompt(memory_context)
        state["phase"] = "初始化"
        await self._save_checkpoint(
            state=state,
            run_id=run_id,
            user_input=user_input,
            memory_context=memory_context,
            created_at=checkpoint_created_at,
        )
        yield state_event(
            _visible_state(state),
            "初始化",
            token_usage=build_token_snapshot(
                messages=state["messages"],
                system_prompt=system_prompt,
                blocking_token_limit=self.blocking_token_limit,
            ),
        )

        for turn in range(1, self.max_turns + 1):
            state["turn"] = turn
            state["phase"] = "预处理"
            managed_messages, context_report = await manage_context(
                state["messages"],
                system_prompt=system_prompt,
                model_call=self.model_call,
                config=self.context_config,
            )
            state["messages"] = managed_messages
            yield state_event(
                _visible_state(state),
                "预处理",
                context_report=context_report,
                token_usage=build_token_snapshot(
                    messages=state["messages"],
                    system_prompt=system_prompt,
                    blocking_token_limit=self.blocking_token_limit,
                ),
            )
            if context_report.get("actions"):
                yield {"type": "context_management", "context_report": context_report}
            await self._save_checkpoint(
                state=state,
                run_id=run_id,
                user_input=user_input,
                memory_context=memory_context,
                created_at=checkpoint_created_at,
            )

            if _message_token_estimate(state["messages"]) > self.blocking_token_limit:
                await self._mark_checkpoint_terminal(run_id, "failed", reason="blocking_limit", state=state)
                yield terminal_event(
                    _visible_state(state),
                    "blocking_limit",
                    TERMINATION_MESSAGES["blocking_limit"],
                )
                return

            selected = await self.tool_selector(
                user_input,
                state["messages"],
                self.tools,
                self.subagent_model_name,
            )
            state["selected_tools"] = _coerce_selected_tools(selected, self.tools)
            state["tool_calls"] = []
            state["tool_results"] = []
            state["phase"] = "工具选择"
            await self._save_checkpoint(
                state=state,
                run_id=run_id,
                user_input=user_input,
                memory_context=memory_context,
                created_at=checkpoint_created_at,
            )
            tool_specs = dashscope_tool_specs(state["selected_tools"], self.tools)

            state["phase"] = "API调用"
            yield state_event(
                _visible_state(state),
                "API调用",
                selected_tools=state["selected_tools"],
                token_usage=build_token_snapshot(
                    messages=state["messages"],
                    system_prompt=system_prompt,
                    tools=tool_specs,
                    blocking_token_limit=self.blocking_token_limit,
                ),
            )
            await self._save_checkpoint(
                state=state,
                run_id=run_id,
                user_input=user_input,
                memory_context=memory_context,
                created_at=checkpoint_created_at,
            )
            yield {"type": "message_start", "turn": turn, "usage": {}}

            assistant_text = ""
            tool_calls: list[dict[str, Any]] = []
            tool_blocks: dict[int, dict[str, Any]] = {}
            submitted_tool_ids: set[str] = set()
            usage: dict[str, Any] | None = None
            last_stream_checkpoint_at = time.monotonic()
            last_stream_checkpoint_chars = 0
            tool_executor = StreamingToolExecutor(
                user_input=user_input,
                messages=state["messages"],
                tools=self.tools,
                permission_reviewer=self.permission_reviewer,
                permission_prompter=self.permission_prompter,
                reviewer_model_name=self.reviewer_model_name,
                memory_context=memory_context,
                runtime_context={
                    "user_input": user_input,
                    "messages": state["messages"],
                    "tools": self.tools,
                    "model_call": self.model_call,
                    "main_model_name": self.main_model_name,
                    "subagent_model_name": self.subagent_model_name,
                    "memory_context": memory_context,
                },
            )

            try:
                async for event in self.model_call(
                    messages=state["messages"],
                    system_prompt=system_prompt,
                    tools=tool_specs,
                    model_name=self.main_model_name,
                ):
                    for tool_event in await tool_executor.drain_ready():
                        if tool_event.get("type") == "tool_result":
                            state["tool_results"] = list(tool_executor.results)
                            await self._save_checkpoint(
                                state=state,
                                run_id=run_id,
                                user_input=user_input,
                                assistant_text=assistant_text,
                                submitted_tool_ids=submitted_tool_ids,
                                memory_context=memory_context,
                                tool_executor=tool_executor,
                                created_at=checkpoint_created_at,
                            )
                        elif tool_event.get("type") in {"tool_status", "tool_start", "tool_review", "permission_decision"}:
                            state["phase"] = "工具执行"
                            await self._save_checkpoint(
                                state=state,
                                run_id=run_id,
                                user_input=user_input,
                                assistant_text=assistant_text,
                                submitted_tool_ids=submitted_tool_ids,
                                memory_context=memory_context,
                                tool_executor=tool_executor,
                                created_at=checkpoint_created_at,
                            )
                        yield tool_event

                    event_type = event.get("type")
                    if event_type == "assistant_delta":
                        assistant_text += event.get("content", "")
                        now = time.monotonic()
                        if (
                            now - last_stream_checkpoint_at >= 1.0
                            or len(assistant_text) - last_stream_checkpoint_chars >= 500
                        ):
                            state["phase"] = "API调用中"
                            await self._save_checkpoint(
                                state=state,
                                run_id=run_id,
                                user_input=user_input,
                                assistant_text=assistant_text,
                                submitted_tool_ids=submitted_tool_ids,
                                memory_context=memory_context,
                                tool_executor=tool_executor,
                                created_at=checkpoint_created_at,
                            )
                            last_stream_checkpoint_at = now
                            last_stream_checkpoint_chars = len(assistant_text)
                        yield {"type": "message_delta", "turn": turn, "content": event.get("content", "")}
                        yield event
                    elif event_type == "tool_call":
                        tool_call = event["tool_call"]
                        tool_id = str(tool_call.get("id") or tool_call.get("name") or len(tool_calls))
                        if tool_id not in submitted_tool_ids:
                            submitted_tool_ids.add(tool_id)
                            tool_calls.append(tool_call)
                            state["tool_calls"] = list(tool_calls)
                            state["phase"] = "收到tool_call"
                            await self._save_checkpoint(
                                state=state,
                                run_id=run_id,
                                user_input=user_input,
                                assistant_text=assistant_text,
                                submitted_tool_ids=submitted_tool_ids,
                                memory_context=memory_context,
                                tool_executor=tool_executor,
                                created_at=checkpoint_created_at,
                            )
                            yield event
                            state["phase"] = "工具执行"
                            yield state_event(
                                _visible_state(state),
                                "工具执行",
                                tool_calls=list(tool_calls),
                            )
                            tool_executor.submit(tool_call)
                            await asyncio.sleep(0)
                            for tool_event in await tool_executor.drain_ready():
                                if tool_event.get("type") == "tool_result":
                                    state["tool_results"] = list(tool_executor.results)
                                    await self._save_checkpoint(
                                        state=state,
                                        run_id=run_id,
                                        user_input=user_input,
                                        assistant_text=assistant_text,
                                        submitted_tool_ids=submitted_tool_ids,
                                        memory_context=memory_context,
                                        tool_executor=tool_executor,
                                        created_at=checkpoint_created_at,
                                    )
                                elif tool_event.get("type") in {"tool_status", "tool_start", "tool_review", "permission_decision"}:
                                    await self._save_checkpoint(
                                        state=state,
                                        run_id=run_id,
                                        user_input=user_input,
                                        assistant_text=assistant_text,
                                        submitted_tool_ids=submitted_tool_ids,
                                        memory_context=memory_context,
                                        tool_executor=tool_executor,
                                        created_at=checkpoint_created_at,
                                    )
                                yield tool_event
                    elif event_type == "token_usage":
                        usage = event.get("token_usage", {})
                        yield event
                    elif event_type in {"content_block_start", "content_block_delta", "content_block_stop"}:
                        yield event
                        if event_type == "content_block_start":
                            block = event.get("block") or {}
                            if block.get("type") == "tool_use":
                                index = int(event.get("index", 0) or 0)
                                tool_blocks[index] = {
                                    "id": str(block.get("id") or f"tool-{index}"),
                                    "name": block.get("name"),
                                    "arguments": "",
                                }
                        elif event_type == "content_block_delta":
                            index = int(event.get("index", 0) or 0)
                            block = tool_blocks.get(index)
                            delta = event.get("delta") or {}
                            if block is not None and delta.get("type") == "input_json_delta":
                                block["arguments"] += str(delta.get("partial_json") or "")
                                tool_id = str(block.get("id") or f"tool-{index}")
                                if tool_id in submitted_tool_ids or not block.get("name"):
                                    continue
                                try:
                                    json.loads(block["arguments"] or "{}")
                                except json.JSONDecodeError:
                                    continue
                                submitted_tool_ids.add(tool_id)
                                tool_call = {
                                    "id": tool_id,
                                    "name": block["name"],
                                    "arguments": block["arguments"] or "{}",
                                }
                                tool_calls.append(tool_call)
                                state["tool_calls"] = list(tool_calls)
                                state["phase"] = "收到tool_call"
                                await self._save_checkpoint(
                                    state=state,
                                    run_id=run_id,
                                    user_input=user_input,
                                    assistant_text=assistant_text,
                                    submitted_tool_ids=submitted_tool_ids,
                                    memory_context=memory_context,
                                    tool_executor=tool_executor,
                                    created_at=checkpoint_created_at,
                                )
                                yield {"type": "tool_call", "tool_call": tool_call}
                                state["phase"] = "工具执行"
                                yield state_event(
                                    _visible_state(state),
                                    "工具执行",
                                    tool_calls=list(tool_calls),
                                )
                                tool_executor.submit(tool_call)
                                await asyncio.sleep(0)
                                for tool_event in await tool_executor.drain_ready():
                                    if tool_event.get("type") == "tool_result":
                                        state["tool_results"] = list(tool_executor.results)
                                    await self._save_checkpoint(
                                        state=state,
                                        run_id=run_id,
                                        user_input=user_input,
                                        assistant_text=assistant_text,
                                        submitted_tool_ids=submitted_tool_ids,
                                        memory_context=memory_context,
                                        tool_executor=tool_executor,
                                        created_at=checkpoint_created_at,
                                    )
                                    yield tool_event
                    else:
                        yield event
            except KeyboardInterrupt:
                await self._mark_checkpoint_terminal(run_id, "aborted", reason="aborted_streaming", state=state)
                yield terminal_event(
                    _visible_state(state),
                    "aborted_streaming",
                    TERMINATION_MESSAGES["aborted_streaming"],
                )
                return
            except Exception as exc:
                logger.exception("submitMessage model call failed")
                await self._mark_checkpoint_terminal(run_id, "failed", reason=str(exc), state=state)
                yield terminal_event(
                    _visible_state(state),
                    "model_error",
                    f"{TERMINATION_MESSAGES['model_error']} {exc}",
                )
                return

            try:
                for tool_event in await tool_executor.finish():
                    if tool_event.get("type") == "tool_result":
                        state["tool_results"] = list(tool_executor.results)
                    await self._save_checkpoint(
                        state=state,
                        run_id=run_id,
                        user_input=user_input,
                        assistant_text=assistant_text,
                        submitted_tool_ids=submitted_tool_ids,
                        memory_context=memory_context,
                        tool_executor=tool_executor,
                        created_at=checkpoint_created_at,
                    )
                    yield tool_event
            except KeyboardInterrupt:
                payload = self._checkpoint_payload(
                    state=state,
                    run_id=run_id,
                    user_input=user_input,
                    assistant_text=assistant_text,
                    submitted_tool_ids=submitted_tool_ids,
                    memory_context=memory_context,
                    tool_executor=tool_executor,
                    created_at=checkpoint_created_at,
                )
                has_unknown_side_effect = any(
                    item.get("side_effectful") and item.get("status") == "executing"
                    for item in payload.get("tool_states", [])
                )
                await self._mark_checkpoint_terminal(
                    run_id,
                    "unknown_outcome" if has_unknown_side_effect else "aborted",
                    reason="aborted_tools",
                    state=payload,
                )
                yield terminal_event(
                    _visible_state(state),
                    "aborted_tools",
                    TERMINATION_MESSAGES["aborted_tools"],
                )
                return
            except Exception as exc:
                logger.exception("submitMessage tool execution failed")
                await self._mark_checkpoint_terminal(run_id, "failed", reason=str(exc), state=state)
                yield terminal_event(
                    _visible_state(state),
                    "model_error",
                    f"{TERMINATION_MESSAGES['model_error']} {exc}",
                )
                return

            state["tool_calls"] = tool_calls
            state["tool_results"] = list(tool_executor.results)
            state["messages"] = [
                *state["messages"],
                _assistant_message(assistant_text, tool_calls),
                *state["tool_results"],
            ]
            state["phase"] = "工具结果完成"
            await self._save_checkpoint(
                state=state,
                run_id=run_id,
                user_input=user_input,
                assistant_text=assistant_text,
                submitted_tool_ids=submitted_tool_ids,
                memory_context=memory_context,
                tool_executor=tool_executor,
                created_at=checkpoint_created_at,
            )
            yield {
                "type": "message_stop",
                "turn": turn,
                "usage": usage or {},
                "stop_reason": "tool_use" if tool_calls else "end_turn",
            }
            yield {
                "type": "token_usage",
                "token_usage": (
                    build_real_usage_snapshot(usage, blocking_token_limit=self.blocking_token_limit)
                    if usage
                    else build_token_snapshot(
                        messages=state["messages"],
                        system_prompt=system_prompt,
                        tools=tool_specs,
                        blocking_token_limit=self.blocking_token_limit,
                        output_text=assistant_text,
                    )
                ),
            }

            if not tool_calls:
                state["phase"] = "终止检查"
                await self._save_checkpoint(
                    state=state,
                    run_id=run_id,
                    user_input=user_input,
                    assistant_text=assistant_text,
                    submitted_tool_ids=submitted_tool_ids,
                    memory_context=memory_context,
                    tool_executor=tool_executor,
                    created_at=checkpoint_created_at,
                )
                yield state_event(_visible_state(state), "终止检查")
                if self.stop_hook and self.stop_hook(_visible_state(state)):
                    await self._mark_checkpoint_terminal(
                        run_id,
                        "completed",
                        reason="stop_hook_prevented",
                        state=state,
                    )
                    yield terminal_event(
                        _visible_state(state),
                        "stop_hook_prevented",
                        TERMINATION_MESSAGES["stop_hook_prevented"],
                    )
                    return
                await self._mark_checkpoint_terminal(run_id, "completed", reason="completed", state=state)
                yield terminal_event(
                    _visible_state(state),
                    "completed",
                    TERMINATION_MESSAGES["completed"],
                )
                return

            snip_reports = []
            for result in state["tool_results"]:
                if result.get("name") != "snip_context":
                    continue
                raw_result = result.get("raw_result") or {}
                messages, snip_report = snip_tool_results(
                    state["messages"],
                    tool_call_ids=list(raw_result.get("tool_call_ids") or []),
                    tool_names=list(raw_result.get("tool_names") or []),
                )
                state["messages"] = messages
                snip_reports.append(snip_report)
            if snip_reports:
                yield {"type": "context_management", "context_report": {"actions": snip_reports}}
            state["main_agent_saved_memory"] = bool(state.get("main_agent_saved_memory")) or any(
                result.get("name") in {"save_memory", "delete_memory", "prune_memories"}
                for result in state["tool_results"]
            )
            state["phase"] = "结果回填"
            await self._save_checkpoint(
                state=state,
                run_id=run_id,
                user_input=user_input,
                assistant_text=assistant_text,
                submitted_tool_ids=submitted_tool_ids,
                memory_context=memory_context,
                tool_executor=tool_executor,
                created_at=checkpoint_created_at,
            )
            yield state_event(_visible_state(state), "结果回填", tool_results=state["tool_results"])

        await self._mark_checkpoint_terminal(run_id, "failed", reason="max_turns", state=state)
        yield terminal_event(
            _visible_state(state),
            "max_turns",
            TERMINATION_MESSAGES["max_turns"],
        )

    def submitMessage(
        self,
        user_input: str,
        *,
        history: list[dict[str, Any]] | None = None,
        memory_context: str | None = None,
    ) -> AsyncGenerator[QueryEvent, None]:
        return self.submit_message(
            user_input,
            history=history,
            memory_context=memory_context,
        )


def submitMessage(
    user_input: str,
    *,
    history: list[dict[str, Any]] | None = None,
    model_call: ModelCall,
    tools: dict[str, dict[str, Any]] | None = None,
    tool_selector: ToolSelector | None = None,
    permission_reviewer: PermissionReviewer | None = None,
    permission_prompter: PermissionPrompter | None = None,
    memory_context: str | None = None,
    max_turns: int = 10,
) -> AsyncGenerator[QueryEvent, None]:
    engine = QueryEngine(
        model_call=model_call,
        tools=tools,
        tool_selector=tool_selector,
        permission_reviewer=permission_reviewer,
        permission_prompter=permission_prompter,
        max_turns=max_turns,
    )
    return engine.submitMessage(
        user_input,
        history=history,
        memory_context=memory_context,
    )
