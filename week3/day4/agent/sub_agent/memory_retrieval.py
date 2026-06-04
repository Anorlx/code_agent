from __future__ import annotations

import json
from pathlib import Path
from typing import Any, AsyncGenerator, Callable

from agent.main_agent.config import DEFAULT_SUB_AGENT_MODEL, MEMORY_ROOT
from agent.memory_system.store import load_memory_index, read_memory_file

MEMORY_RETRIEVAL_SYSTEM_PROMPT = """你是 memory_retrieval。
你的任务是根据用户当前问题和 MEMORY.md 索引，选择可能相关的记忆正文文件。
不要解决用户问题，只返回 JSON。

如果没有相关记忆，返回：
{"files":[]}

如果有相关记忆，返回：
{"files":["feedback/pre-commit-lint.md"]}

注意：
- MEMORY.md 只是目录。
- 不要选择无关记忆。
- 记忆只是线索，主 Agent 使用前仍然需要验证当前项目状态。
"""

ModelCall = Callable[..., AsyncGenerator[dict[str, Any], None]]


def _extract_json(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        return {}
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _indexed_files(index_text: str) -> list[str]:
    files = []
    for line in index_text.splitlines():
        if "](" not in line or ")" not in line:
            continue
        start = line.find("](") + 2
        end = line.find(")", start)
        if end > start:
            files.append(line[start:end])
    return files


async def select_relevant_memory_files(
    user_input: str,
    memory_index: str,
    model_call: ModelCall | None = None,
    model_name: str = DEFAULT_SUB_AGENT_MODEL,
) -> list[str]:
    available = set(_indexed_files(memory_index))
    if model_call is None or not available:
        return []

    prompt = {
        "role": "user",
        "content": json.dumps(
            {
                "user_input": user_input,
                "memory_index": memory_index,
            },
            ensure_ascii=False,
        ),
    }
    content = ""
    try:
        async for event in model_call(
            messages=[prompt],
            system_prompt=MEMORY_RETRIEVAL_SYSTEM_PROMPT,
            tools=[],
            model_name=model_name,
        ):
            if event.get("type") == "assistant_delta":
                content += event.get("content", "")
    except Exception:
        return []

    parsed = _extract_json(content)
    files = parsed.get("files", [])
    if not isinstance(files, list):
        return []
    return [path for path in files if isinstance(path, str) and path in available]


async def load_memory_context(
    user_input: str,
    memory_root: Path | None = None,
    model_call: ModelCall | None = None,
    model_name: str = DEFAULT_SUB_AGENT_MODEL,
) -> dict[str, Any]:
    root = memory_root or MEMORY_ROOT
    index = load_memory_index(root)
    selected_files = await select_relevant_memory_files(
        user_input=user_input,
        memory_index=index,
        model_call=model_call,
        model_name=model_name,
    )
    selected_parts = []
    for relative_path in selected_files:
        try:
            selected_parts.append(f"## {relative_path}\n\n{read_memory_file(relative_path, root)}")
        except OSError:
            continue
    selected_memories = "\n\n".join(selected_parts)
    return {
        "index": f"MEMORY.md\n\n{index}",
        "selected_files": selected_files,
        "selected_memories": selected_memories,
    }


def format_memory_context(context: dict[str, Any]) -> str:
    parts = [
        "长期记忆索引如下。MEMORY.md 是目录，记忆只是线索；使用前需要验证当前项目状态。",
        str(context.get("index") or ""),
    ]
    selected = str(context.get("selected_memories") or "").strip()
    if selected:
        parts.extend(
            [
                "当前问题可能相关的记忆正文如下。不要无脑相信，先检查是否仍然成立。",
                selected,
            ]
        )
    return "\n\n".join(part for part in parts if part.strip())
