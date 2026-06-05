from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from agent.tools.mcp.cache import load_mcp_tools_cache, save_mcp_tools_cache
from agent.tools.mcp.client import call_mcp_tool, list_mcp_tools
from agent.tools.mcp.config import McpServerConfig, load_mcp_servers
from agent.tools.mcp.settings import load_mcp_settings, server_settings

ToolFunc = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
logger = logging.getLogger(__name__)


@dataclass
class McpRegistryResult:
    registry: dict[str, dict[str, Any]]
    discovered_tools: dict[str, list[dict[str, Any]]]
    server_counts: dict[str, int]
    errors: dict[str, str]
    elapsed_ms: int
    cache_hit: bool = False


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


def _mcp_tool_side_effectful(tool_name: str, description: str) -> bool:
    text = f"{tool_name} {description}".lower()
    write_markers = (
        "create",
        "update",
        "delete",
        "remove",
        "write",
        "save",
        "send",
        "post",
        "commit",
        "issue",
        "pull_request",
        "mutation",
    )
    read_markers = (
        "search",
        "query",
        "get",
        "list",
        "read",
        "fetch",
        "find",
        "lookup",
        "geocode",
        "route",
        "weather",
        "map",
        "distance",
    )
    if any(marker in text for marker in write_markers):
        return True
    if any(marker in text for marker in read_markers):
        return False
    return True


def _register_remote_tools(
    *,
    server_name: str,
    server: McpServerConfig,
    remote_tools: list[dict[str, Any]],
    settings: dict[str, Any],
    registry: dict[str, dict[str, Any]],
) -> int:
    server_rule = server_settings(server_name, settings)
    permission = _permission(server_rule)
    if permission == "deny":
        return 0
    count = 0
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
            "side_effectful": _mcp_tool_side_effectful(original_name, description),
            "permission": permission,
            "mcp_server": server_name,
            "mcp_tool": original_name,
        }
        count += 1
    return count


def build_mcp_registry_from_discovered(
    discovered_tools: dict[str, list[dict[str, Any]]],
    *,
    servers: dict[str, McpServerConfig] | None = None,
    settings: dict[str, Any] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    active_servers = servers or load_mcp_servers()
    active_settings = settings or load_mcp_settings()
    registry: dict[str, dict[str, Any]] = {}
    server_counts: dict[str, int] = {}
    for server_name, remote_tools in discovered_tools.items():
        server = active_servers.get(server_name)
        if server is None or server.type != "stdio":
            continue
        count = _register_remote_tools(
            server_name=server_name,
            server=server,
            remote_tools=remote_tools,
            settings=active_settings,
            registry=registry,
        )
        if count:
            server_counts[server_name] = count
    return registry, server_counts


async def _discover_one_server(
    server_name: str,
    server: McpServerConfig,
    *,
    timeout: float,
) -> tuple[str, list[dict[str, Any]], str | None]:
    if server.type != "stdio":
        return server_name, [], f"unsupported MCP server type: {server.type}"
    try:
        remote_tools = await asyncio.wait_for(list_mcp_tools(server), timeout=timeout)
        return server_name, remote_tools, None
    except Exception as exc:
        logger.warning("mcp discovery failed server=%s error=%s", server_name, exc)
        return server_name, [], str(exc)


async def discover_mcp_tools(
    *,
    servers: dict[str, McpServerConfig] | None = None,
    timeout: float = 45.0,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str], int]:
    active_servers = servers or load_mcp_servers()
    started = time.perf_counter()
    tasks = [
        asyncio.create_task(_discover_one_server(server_name, server, timeout=timeout))
        for server_name, server in active_servers.items()
    ]
    discovered: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}
    if tasks:
        for result in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(result, Exception):
                logger.warning("mcp discovery task crashed error=%s", result)
                continue
            server_name, remote_tools, error = result
            if error:
                errors[server_name] = error
            if remote_tools:
                discovered[server_name] = remote_tools
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "mcp discovery completed servers=%s loaded=%s elapsed_ms=%s errors=%s",
        len(active_servers),
        len(discovered),
        elapsed_ms,
        errors,
    )
    return discovered, errors, elapsed_ms


def get_cached_mcp_tool_registry(
    *,
    servers: dict[str, McpServerConfig] | None = None,
) -> McpRegistryResult:
    active_servers = servers or load_mcp_servers()
    started = time.perf_counter()
    cached_tools = load_mcp_tools_cache(active_servers)
    registry, server_counts = build_mcp_registry_from_discovered(cached_tools, servers=active_servers)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return McpRegistryResult(
        registry=registry,
        discovered_tools=cached_tools,
        server_counts=server_counts,
        errors={},
        elapsed_ms=elapsed_ms,
        cache_hit=bool(registry),
    )


async def refresh_mcp_tool_registry(
    *,
    servers: dict[str, McpServerConfig] | None = None,
    timeout: float = 45.0,
    write_cache: bool = True,
) -> McpRegistryResult:
    active_servers = servers or load_mcp_servers()
    discovered, errors, elapsed_ms = await discover_mcp_tools(servers=active_servers, timeout=timeout)
    if write_cache and discovered:
        try:
            save_mcp_tools_cache(discovered, active_servers)
        except Exception as exc:
            logger.warning("mcp cache write failed error=%s", exc)
    registry, server_counts = build_mcp_registry_from_discovered(discovered, servers=active_servers)
    return McpRegistryResult(
        registry=registry,
        discovered_tools=discovered,
        server_counts=server_counts,
        errors=errors,
        elapsed_ms=elapsed_ms,
        cache_hit=False,
    )


async def get_mcp_tool_registry(timeout: float = 45.0) -> dict[str, dict[str, Any]]:
    servers = load_mcp_servers()
    settings = load_mcp_settings()
    discovered, _, _ = await discover_mcp_tools(servers=servers, timeout=timeout)
    registry, _ = build_mcp_registry_from_discovered(discovered, servers=servers, settings=settings)
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
