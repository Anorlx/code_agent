from __future__ import annotations

from pathlib import Path
from typing import Any


def _resolve_inside_workspace(path: str, workspace_root: Path | None = None) -> Path:
    root = (workspace_root or Path.cwd()).resolve()
    target = (root / path).resolve()
    if target != root and root not in target.parents:
        raise ValueError("Path is outside project.")
    return target


async def read_file(
    arguments: dict[str, Any],
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    try:
        path = _resolve_inside_workspace(str(arguments.get("path", "")), workspace_root)
        return {"ok": True, "content": path.read_text(encoding="utf-8")}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def write_file(
    arguments: dict[str, Any],
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    try:
        path = _resolve_inside_workspace(str(arguments.get("path", "")), workspace_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(arguments.get("content", "")), encoding="utf-8")
        return {"ok": True, "content": f"Wrote {path.name}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def delete_file(
    arguments: dict[str, Any],
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    try:
        path = _resolve_inside_workspace(str(arguments.get("path", "")), workspace_root)
        if not path.exists():
            return {"ok": False, "error": f"File does not exist: {path.name}"}
        if not path.is_file():
            return {"ok": False, "error": "delete_file only deletes files, not directories."}
        path.unlink()
        return {"ok": True, "content": f"Deleted {path.name}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def list_dir(
    arguments: dict[str, Any],
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    try:
        path = _resolve_inside_workspace(str(arguments.get("path", ".")), workspace_root)
        entries = sorted(child.name for child in path.iterdir())
        return {"ok": True, "entries": entries, "content": "\n".join(entries)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def read_file_spec() -> dict[str, Any]:
    return {
        "name": "read_file",
        "description": "读取当前项目内的文本文件内容。不能读取项目之外的路径。",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "相对项目根目录的文件路径。"}},
            "required": ["path"],
        },
    }


def write_file_spec() -> dict[str, Any]:
    return {
        "name": "write_file",
        "description": "写入当前项目内的文本文件，会自动创建父目录。不能写到项目之外。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对项目根目录的文件路径。"},
                "content": {"type": "string", "description": "要写入的文本内容。"},
            },
            "required": ["path", "content"],
        },
    }


def delete_file_spec() -> dict[str, Any]:
    return {
        "name": "delete_file",
        "description": "删除当前项目内的文件。只能删除文件，不能删除目录，不能删除项目之外的路径。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对项目根目录的文件路径。"},
            },
            "required": ["path"],
        },
    }


def list_dir_spec() -> dict[str, Any]:
    return {
        "name": "list_dir",
        "description": "列出当前项目内目录的文件名。",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "相对项目根目录的目录路径。"}},
            "required": ["path"],
        },
    }

