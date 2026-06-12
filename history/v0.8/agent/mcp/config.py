from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.mcp.settings import mcp_config_path


@dataclass(frozen=True)
class McpServerConfig:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    type: str = "stdio"
    cwd: str | None = None

    def public_summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "command": self.command,
            "args": self.args,
            "env_keys": sorted(self.env),
        }


def _as_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _as_string_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def load_mcp_servers(path: Path | None = None) -> dict[str, McpServerConfig]:
    target = path or mcp_config_path()
    if not target.exists():
        return {}
    value = json.loads(target.read_text(encoding="utf-8"))
    raw_servers = value.get("mcpServers") if isinstance(value, dict) else {}
    if not isinstance(raw_servers, dict):
        return {}

    servers: dict[str, McpServerConfig] = {}
    for name, raw in raw_servers.items():
        if not isinstance(raw, dict):
            continue
        command = str(raw.get("command") or "").strip()
        if not command:
            continue
        servers[str(name)] = McpServerConfig(
            name=str(name),
            type=str(raw.get("type") or "stdio"),
            command=command,
            args=_as_string_list(raw.get("args")),
            env=_as_string_dict(raw.get("env")),
            cwd=str(raw["cwd"]) if raw.get("cwd") else None,
        )
    return servers


def mcp_servers_catalog(path: Path | None = None) -> str:
    servers = load_mcp_servers(path)
    if not servers:
        return "未配置 MCP server。"
    lines = ["MCP servers:"]
    for server in servers.values():
        summary = server.public_summary()
        args = " ".join(summary["args"])
        env = ",".join(summary["env_keys"]) or "none"
        lines.append(f"- {summary['name']} ({summary['type']}): {summary['command']} {args}; env={env}")
    return "\n".join(lines)
