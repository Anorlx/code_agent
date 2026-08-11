from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent.main_agent.config import PROJECT_ROOT

SETTINGS_PATH = Path(__file__).with_name("settings.json")


def load_mcp_settings(path: Path | None = None) -> dict[str, Any]:
    target = path or SETTINGS_PATH
    if not target.exists():
        return {"servers": {}}
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"servers": {}}
    return value if isinstance(value, dict) else {"servers": {}}


def server_settings(server_name: str, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    data = settings or load_mcp_settings()
    servers = data.get("servers") or {}
    value = servers.get(server_name) or {}
    return value if isinstance(value, dict) else {}


def mcp_config_path() -> Path:
    return PROJECT_ROOT / ".mcp.json"
