from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from agent.main_agent.config import AGENT_DATA_ROOT

SCRATCHPAD_ROOT = AGENT_DATA_ROOT / "coordinator_scratchpad"


def _slug(text: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff_-]+", "-", text).strip("-")
    return value[:60] or "task"


def new_scratchpad_dir(task: str) -> Path:
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    path = SCRATCHPAD_ROOT / f"{timestamp}-{_slug(task)}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_markdown(path: Path, title: str, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {title}\n\n{content.strip()}\n", encoding="utf-8")
    return path


def research_markdown(result: dict[str, Any]) -> str:
    return (
        f"**Status**: {result.get('status')}\n\n"
        f"**Duration**: {result.get('duration_seconds', '?')}s\n\n"
        f"## Result\n\n{result.get('result') or ''}\n"
    )
