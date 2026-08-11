from __future__ import annotations

import asyncio
import re
from typing import Any, Awaitable, Callable

from agent.mcp.client import call_mcp_tool, list_mcp_tools
from agent.mcp.config import McpServerConfig, load_mcp_servers
from agent.mcp.settings import load_mcp_settings, server_settings

ToolFunc = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def mcp_tool_name(server_name: str, tool_name: str) -> str:
    safe_server = re.sub(r"[^a-zA-Z0-9_]+", "_", server_name).strip("_")
    safe_tool = re.sub(r"[^a-zA-Z0-9_]+", "_", tool_name).strip("_")
    return f"mcp__{safe_server}__{safe_tool}"


def _server_from_registered_name(name: str) -> str | None:
    parts = name.split("__", 2)
    if len(parts) != 3 or parts[0] != "mcp":
        return None
    return parts[1].replace("_", "-")


def _schema(tool: dict[str, Any]) -> dict[str, Any]:
    schema = tool.get("inputSchema") or {"type": "object", "properties": {}}
    return schema if isinstance(schema, dict) else {"type": "object", "properties": {}}


def _allowed_by_settings(tool_name: str, settings: dict[str, Any]) -> bool:
    denied = settings.get("deniedTools") or []
    allowed = settings.get("allowedTools") or ["*"]
    if "*" in denied or tool_name in denied:
        return False
    return "*" in allowed or tool_name in allowed


def _permission(settings: dict[str, Any]) -> str:
    value = str(settings.get("permission") or "ask").strip().lower()
    return value if value in {"allow", "ask", "deny"} else "ask"


def _make_runner(server: McpServerConfig, tool_name: str) -> ToolFunc:
    async def run(arguments: dict[str, Any]) -> dict[str, Any]:
        return await call_mcp_tool(server, tool_name, arguments)

    return run


async def get_mcp_tool_registry(timeout: float = 45.0) -> dict[str, dict[str, Any]]:
    servers = load_mcp_servers()
    settings = load_mcp_settings()
    registry: dict[str, dict[str, Any]] = {}
    for server_name, server in servers.items():
        if server.type != "stdio":
            continue
        server_rule = server_settings(server_name, settings)
        permission = _permission(server_rule)
        if permission == "deny":
            continue
        try:
            remote_tools = await asyncio.wait_for(list_mcp_tools(server), timeout=timeout)
        except Exception:
            continue
        for remote_tool in remote_tools:
            original_name = str(remote_tool.get("name") or "").strip()
            if not original_name or not _allowed_by_settings(original_name, server_rule):
                continue
            registered_name = mcp_tool_name(server_name, original_name)
            description = str(
                remote_tool.get("description")
                or server_rule.get("description")
                or f"MCP tool {original_name} from {server_name}."
            )
            registry[registered_name] = {
                "spec": {
                    "name": registered_name,
                    "description": f"[MCP:{server_name}] {description}",
                    "parameters": _schema(remote_tool),
                },
                "run": _make_runner(server, original_name),
                "category": "MCP",
                "responsibility": description,
                "parallel_safe": False,
                "requires_review": permission == "ask",
                "permission": permission,
                "mcp_server": server_name,
                "mcp_tool": original_name,
            }
    return registry


def mcp_tool_catalog(tools: dict[str, dict[str, Any]]) -> str:
    mcp_tools = [(name, info) for name, info in tools.items() if info.get("category") == "MCP"]
    if not mcp_tools:
        return "MCP tools: none discovered."
    lines = ["MCP tools:"]
    for name, info in sorted(mcp_tools):
        server = info.get("mcp_server", "?")
        original = info.get("mcp_tool", "?")
        description = info.get("spec", {}).get("description", "")
        lines.append(f"- {name}: server={server}, tool={original}, {description}")
    return "\n".join(lines)


def select_mcp_tools_for_server(server_name: str, tools: dict[str, dict[str, Any]]) -> list[str]:
    return [
        name
        for name, info in tools.items()
        if info.get("category") == "MCP" and info.get("mcp_server") == server_name
    ]
