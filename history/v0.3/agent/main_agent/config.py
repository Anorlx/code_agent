from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TOOL_WORKSPACE = PROJECT_ROOT / "agent_write"
MEMORY_ROOT = PROJECT_ROOT / "memory"
DASHSCOPE_COMPATIBLE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MAIN_MODEL = "qwen3.5-122b-a10b"
DEFAULT_SUB_AGENT_MODEL = "qwen3.5-122b-a10b"
