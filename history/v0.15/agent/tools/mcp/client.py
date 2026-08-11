from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from agent.main_agent.config import PROJECT_ROOT
from agent.main_agent.retry import RetryConfig, retry_async
from agent.tools.mcp.config import McpServerConfig

logger = logging.getLogger(__name__)
MCP_RETRY_CONFIG = RetryConfig(attempts=3, initial_delay=0.4, max_delay=3.0)


def _model_dump(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(by_alias=True)
    if hasattr(value, "dict"):
        return value.dict()
    return dict(value)


@asynccontextmanager
async def mcp_session(server: McpServerConfig) -> AsyncIterator[Any]:
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:
        raise RuntimeError("mcp package is not installed in this conda env.") from exc

    cwd = (PROJECT_ROOT / server.cwd).resolve() if server.cwd else PROJECT_ROOT
    params = StdioServerParameters(
        command=server.command,
        args=server.args,
        env={**os.environ, **server.env},
        cwd=cwd,
    )
    with open(os.devnull, "w", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session


async def list_mcp_tools(server: McpServerConfig) -> list[dict[str, Any]]:
    async def operation() -> Any:
        async with mcp_session(server) as session:
            return await session.list_tools()

    result = await retry_async(
        operation,
        config=MCP_RETRY_CONFIG,
        on_retry=lambda attempt, exc, delay: logger.warning(
            "mcp list_tools retry server=%s attempt=%s delay=%.2fs error=%s",
            server.name,
            attempt,
            delay,
            exc,
        ),
    )
    return [_model_dump(tool) for tool in result.tools]


def _content_item_to_text(item: Any) -> str:
    data = _model_dump(item)
    if data.get("type") == "text":
        return str(data.get("text") or "")
    return json.dumps(data, ensure_ascii=False)


async def call_mcp_tool(server: McpServerConfig, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        async def operation() -> Any:
            async with mcp_session(server) as session:
                return await session.call_tool(tool_name, arguments)

        result = await retry_async(
            operation,
            config=MCP_RETRY_CONFIG,
            on_retry=lambda attempt, exc, delay: logger.warning(
                "mcp call_tool retry server=%s tool=%s attempt=%s delay=%.2fs error=%s",
                server.name,
                tool_name,
                attempt,
                delay,
                exc,
            ),
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    content = [_content_item_to_text(item) for item in result.content]
    structured = getattr(result, "structuredContent", None)
    return {
        "ok": not bool(getattr(result, "isError", False)),
        "content": "\n".join(text for text in content if text).strip(),
        "structured": structured,
        "is_error": bool(getattr(result, "isError", False)),
    }
