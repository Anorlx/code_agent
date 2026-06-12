from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

from agent.config import PROJECT_ROOT

SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    "__pycache__",
    ".ruff_cache",
    ".venv",
    "node_modules",
}


def _resolve_inside_project(path: str, project_root: Path | None = None) -> Path:
    root = (project_root or PROJECT_ROOT).resolve()
    target = (root / path).resolve()
    if target != root and root not in target.parents:
        raise ValueError("Path is outside project.")
    return target


def _relative(path: Path, root: Path) -> str:
    return "." if path == root else path.relative_to(root).as_posix()


async def ls_project(
    arguments: dict[str, Any],
    project_root: Path | None = None,
) -> dict[str, Any]:
    try:
        root = (project_root or PROJECT_ROOT).resolve()
        path = _resolve_inside_project(str(arguments.get("path", ".")), root)
        recursive = bool(arguments.get("recursive", False))
        max_entries = int(arguments.get("max_entries", 200))

        if recursive:
            entries = []
            for child in path.rglob("*"):
                if any(part in SKIP_DIRS for part in child.relative_to(root).parts):
                    continue
                entries.append(_relative(child, root) + ("/" if child.is_dir() else ""))
                if len(entries) >= max_entries:
                    break
        else:
            entries = [
                child.name + ("/" if child.is_dir() else "")
                for child in sorted(path.iterdir(), key=lambda item: item.name)
                if child.name not in SKIP_DIRS
            ][:max_entries]

        return {"ok": True, "entries": entries, "content": "\n".join(entries)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def read_project_file(
    arguments: dict[str, Any],
    project_root: Path | None = None,
) -> dict[str, Any]:
    try:
        root = (project_root or PROJECT_ROOT).resolve()
        path = _resolve_inside_project(str(arguments.get("path", "")), root)
        max_chars = int(arguments.get("max_chars", 20_000))
        content = path.read_text(encoding="utf-8")
        truncated = len(content) > max_chars
        if truncated:
            content = content[:max_chars]
        return {"ok": True, "content": content, "truncated": truncated}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def grep_project(
    arguments: dict[str, Any],
    project_root: Path | None = None,
) -> dict[str, Any]:
    pattern = str(arguments.get("pattern", ""))
    if not pattern:
        return {"ok": False, "error": "Missing pattern."}
    try:
        root = (project_root or PROJECT_ROOT).resolve()
        path = _resolve_inside_project(str(arguments.get("path", ".")), root)
        max_matches = int(arguments.get("max_matches", 50))
        command = [
            "rg",
            "--line-number",
            "--column",
            "--no-heading",
            "--color",
            "never",
            "--glob",
            "!**/__pycache__/**",
            "--glob",
            "!**/.git/**",
            pattern,
            path.as_posix(),
        ]
        completed = await asyncio.to_thread(
            subprocess.run,
            command,
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode not in {0, 1}:
            return {"ok": False, "error": completed.stderr.strip() or "rg failed."}

        matches = []
        for line in completed.stdout.splitlines()[:max_matches]:
            file_path, line_no, column, text = line.split(":", 3)
            matches.append(
                {
                    "path": Path(file_path).resolve().relative_to(root).as_posix(),
                    "line": int(line_no),
                    "column": int(column),
                    "text": text,
                }
            )
        content = "\n".join(
            f"{match['path']}:{match['line']}:{match['column']}: {match['text']}"
            for match in matches
        )
        return {"ok": True, "matches": matches, "content": content}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def ls_project_spec() -> dict[str, Any]:
    return {
        "name": "ls_project",
        "description": "类似 ls。列出当前项目目录中的文件/目录，可递归。只读，不能访问项目外路径。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对项目根目录的路径，默认 .。"},
                "recursive": {"type": "boolean", "description": "是否递归列出。"},
                "max_entries": {"type": "integer", "description": "最多返回多少条，默认 200。"},
            },
        },
    }


def grep_project_spec() -> dict[str, Any]:
    return {
        "name": "grep_project",
        "description": "类似 grep/rg。在当前项目内搜索文本，返回文件路径、行号、列号和匹配行。",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "要搜索的文本或正则表达式。"},
                "path": {"type": "string", "description": "相对项目根目录的搜索路径，默认 .。"},
                "max_matches": {"type": "integer", "description": "最多返回多少条匹配，默认 50。"},
            },
            "required": ["pattern"],
        },
    }


def read_project_file_spec() -> dict[str, Any]:
    return {
        "name": "read_project_file",
        "description": "读取当前项目内的文本文件。只读，不能访问项目外路径。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对项目根目录的文件路径。"},
                "max_chars": {"type": "integer", "description": "最多读取多少字符，默认 20000。"},
            },
            "required": ["path"],
        },
    }
