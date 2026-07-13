from __future__ import annotations

import logging
from typing import Any, AsyncGenerator

from agent.hooks import HookAction, HookEvent, HookFailure, HookManager
from agent.main_agent.checkpoint_store import CheckpointStore
from agent.main_agent.config import DEFAULT_MAIN_MODEL, DEFAULT_SUB_AGENT_MODEL
from agent.main_agent.context_manager import ContextConfig
from agent.main_agent.graph import (
    ModelCall,
    PermissionPrompter,
    PermissionReviewer,
    StopHook,
    ToolSelector,
    run_agent,
)
from agent.tools.registry import get_tool_registry

logger = logging.getLogger(__name__)

QueryEvent = dict[str, Any]


def _redact_message(message: str, sensitive_values: tuple[str, ...]) -> str:
    redacted = message
    for value in sorted(set(sensitive_values), key=len, reverse=True):
        if value:
            redacted = redacted.replace(value, "[REDACTED]")
    return redacted


def _prompt_sensitive_values(
    original_payload: dict[str, Any],
    final_payload: dict[str, Any],
) -> tuple[str, ...]:
    return tuple(
        value
        for payload in (original_payload, final_payload)
        for field in ("user_input", "memory_context")
        if isinstance((value := payload.get(field)), str)
    )


def _hook_error_event(
    event_name: str,
    failure: HookFailure,
    *,
    sensitive_values: tuple[str, ...] = (),
) -> QueryEvent:
    return {
        "type": "hook_error",
        "event_name": event_name,
        "handler_name": failure.handler_name,
        "error_type": failure.error_type,
        "message": _redact_message(failure.message, sensitive_values),
    }


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
        hook_manager: HookManager | None = None,
    ) -> None:
        self.model_call = model_call
        self.tools = tools or get_tool_registry()
        self.tool_selector = tool_selector
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
        self.hook_manager = hook_manager

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

    async def submit_message(
        self,
        user_input: str,
        *,
        history: list[dict[str, Any]] | None = None,
        memory_context: str | None = None,
    ) -> AsyncGenerator[QueryEvent, None]:
        if self.hook_manager is not None:
            original_payload = {
                "user_input": user_input,
                "memory_context": memory_context,
            }
            hook_result = await self.hook_manager.emit(
                HookEvent(
                    "prompt.before",
                    self.session_id or "",
                    original_payload,
                )
            )
            payload = hook_result.updated_payload or {}
            sensitive_values = _prompt_sensitive_values(
                original_payload,
                payload,
            )
            for failure in hook_result.failures:
                yield _hook_error_event(
                    "prompt.before",
                    failure,
                    sensitive_values=sensitive_values,
                )

            if hook_result.action is HookAction.BLOCK:
                reason = _redact_message(
                    hook_result.reason or "blocked by prompt hook",
                    sensitive_values,
                )
                yield {
                    "type": "terminal",
                    "reason": "hook_blocked",
                    "message": f"Prompt processing was blocked: {reason}",
                    "state": {
                        "turn": 0,
                        "phase": "hook_blocked",
                        "messages": list(history or []),
                        "termination_reason": "hook_blocked",
                    },
                }
                return

            updated_user_input = payload.get("user_input")
            if isinstance(updated_user_input, str):
                user_input = updated_user_input
            else:
                yield _hook_error_event(
                    "prompt.before",
                    HookFailure(
                        "prompt.before payload",
                        "HookPayloadError",
                        f"user_input has invalid type "
                        f"{type(updated_user_input).__name__}; preserving the previous value",
                    ),
                )

            updated_memory_context = payload.get("memory_context")
            if updated_memory_context is None or isinstance(updated_memory_context, str):
                memory_context = updated_memory_context
            else:
                yield _hook_error_event(
                    "prompt.before",
                    HookFailure(
                        "prompt.before payload",
                        "HookPayloadError",
                        f"memory_context has invalid type "
                        f"{type(updated_memory_context).__name__}; preserving the previous value",
                    ),
                )

            context_segments = [
                segment
                for segment in [memory_context, *hook_result.additional_context]
                if isinstance(segment, str) and segment
            ]
            memory_context = "\n".join(context_segments) or None

        async for event in run_agent(
            user_input=user_input,
            history=history,
            model_call=self.model_call,
            tool_selector=self.tool_selector,
            tools=self.tools,
            max_turns=self.max_turns,
            blocking_token_limit=self.blocking_token_limit,
            stop_hook=self.stop_hook,
            main_model_name=self.main_model_name,
            subagent_model_name=self.subagent_model_name,
            permission_reviewer=self.permission_reviewer,
            permission_prompter=self.permission_prompter,
            reviewer_model_name=self.reviewer_model_name,
            memory_context=memory_context,
            context_config=self.context_config,
            checkpoint_store=self.checkpoint_store,
            session_id=self.session_id,
            hook_manager=self.hook_manager,
        ):
            yield event


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
    hook_manager: HookManager | None = None,
) -> AsyncGenerator[QueryEvent, None]:
    engine = QueryEngine(
        model_call=model_call,
        tools=tools,
        tool_selector=tool_selector,
        permission_reviewer=permission_reviewer,
        permission_prompter=permission_prompter,
        max_turns=max_turns,
        hook_manager=hook_manager,
    )
    return engine.submitMessage(
        user_input,
        history=history,
        memory_context=memory_context,
    )
