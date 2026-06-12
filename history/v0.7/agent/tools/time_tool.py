from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


async def current_time(arguments: dict[str, Any]) -> dict[str, Any]:
    timezone = str(arguments.get("timezone", "Asia/Shanghai"))
    try:
        now = datetime.now(ZoneInfo(timezone)).isoformat(timespec="seconds")
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "content": now}


def current_time_spec() -> dict[str, Any]:
    return {
        "name": "current_time",
        "description": "获取指定时区的当前时间，默认 Asia/Shanghai。",
        "parameters": {
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": "IANA 时区名，例如 Asia/Shanghai 或 America/New_York。",
                }
            },
        },
    }

