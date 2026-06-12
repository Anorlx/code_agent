from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from agent.main_agent.config import PROJECT_ROOT
from agent.mcp.config import McpServerConfig


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
    async with mcp_session(server) as session:
        result = await session.list_tools()
    return [_model_dump(tool) for tool in result.tools]


def _content_item_to_text(item: Any) -> str:
    data = _model_dump(item)
    if data.get("type") == "text":
        return str(data.get("text") or "")
    return json.dumps(data, ensure_ascii=False)


async def call_mcp_tool(server: McpServerConfig, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        async with mcp_session(server) as session:
            result = await session.call_tool(tool_name, arguments)
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
