from __future__ import annotations

import logging
from typing import Any, AsyncGenerator

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