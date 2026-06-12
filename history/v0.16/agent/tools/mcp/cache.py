from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from agent.main_agent.config import AGENT_DATA_ROOT
from agent.tools.mcp.config import McpServerConfig

MCP_TOOLS_CACHE_VERSION = 1
DEFAULT_MCP_TOOLS_CACHE_TTL_SECONDS = 86_400
MCP_TOOLS_CACHE_PATH = AGENT_DATA_ROOT / "mcp_tools_cache.json"


def mcp_tools_cache_ttl_seconds() -> int:
    raw_value = os.getenv("MCP_TOOLS_CACHE_TTL_SECONDS", "")
    if not raw_value:
        return DEFAULT_MCP_TOOLS_CACHE_TTL_SECONDS
    try:
        return max(0, int(raw_value))
    except ValueError:
        return DEFAULT_MCP_TOOLS_CACHE_TTL_SECONDS


def server_config_fingerprint(server: McpServerConfig) -> str:
    public_config = {
        "name": server.name,
        "type": server.type,
        "command": server.command,
        "args": list(server.args),
        "cwd": server.cwd,
        "env_keys": sorted(server.env),
    }
    payload = json.dumps(public_config, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_mcp_tools_cache(
    servers: dict[str, McpServerConfig],
    *,
    cache_path: Path = MCP_TOOLS_CACHE_PATH,
    ttl_seconds: int | None = None,
) -> dict[str, list[dict[str, Any]]]:
    if not cache_path.exists():
        return {}
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(cache, dict) or cache.get("version") != MCP_TOOLS_CACHE_VERSION:
        return {}
    updated_at = float(cache.get("updated_at") or 0)
    ttl = mcp_tools_cache_ttl_seconds() if ttl_seconds is None else ttl_seconds
    if ttl > 0 and time.time() - updated_at > ttl:
        return {}

    raw_servers = cache.get("servers") or {}
    if not isinstance(raw_servers, dict):
        return {}

    cached_tools: dict[str, list[dict[str, Any]]] = {}
    for server_name, server in servers.items():
        cached_server = raw_servers.get(server_name)
        if not isinstance(cached_server, dict):
            continue
        if cached_server.get("config_fingerprint") != server_config_fingerprint(server):
            continue
        tools = cached_server.get("tools") or []
        if isinstance(tools, list):
            cached_tools[server_name] = [tool for tool in tools if isinstance(tool, dict)]
    return cached_tools


def save_mcp_tools_cache(
    discovered_tools: dict[str, list[dict[str, Any]]],
    servers: dict[str, McpServerConfig],
    *,
    cache_path: Path = MCP_TOOLS_CACHE_PATH,
) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": MCP_TOOLS_CACHE_VERSION,
        "updated_at": time.time(),
        "servers": {},
    }
    for server_name, tools in discovered_tools.items():
        server = servers.get(server_name)
        if server is None:
            continue
        payload["servers"][server_name] = {
            "config_fingerprint": server_config_fingerprint(server),
            "tools": [_safe_tool_schema(tool) for tool in tools],
        }
    cache_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _safe_tool_schema(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": str(tool.get("name") or ""),
        "description": str(tool.get("description") or ""),
        "inputSchema": tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {},
    }
