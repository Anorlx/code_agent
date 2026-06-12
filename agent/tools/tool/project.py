from __future__ import annotations

import fnmatch
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from agent.main_agent.config import PROJECT_ROOT

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


def _as_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _is_skipped(path: Path, root: Path, ignore: Iterable[str] = ()) -> bool:
    rel = _relative(path, root)
    if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
        return True
    for pattern in ignore:
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(path.name, pattern):
            return True
    return False


def _matches_any_glob(rel: str, name: str, patterns: list[str]) -> bool:
    if not patterns:
        return True
    return any(fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern) for pattern in patterns)


def _iter_project_files(
    path: Path,
    root: Path,
    *,
    glob_patterns: list[str] | None = None,
    ignore: list[str] | None = None,
) -> Iterable[Path]:
    patterns = glob_patterns or []
    ignored = ignore or []
    if path.is_file():
        rel = _relative(path, root)
        if not _is_skipped(path, root, ignored) and _matches_any_glob(rel, path.name, patterns):
            yield path
        return
    for child in path.rglob("*"):
        if not child.is_file() or _is_skipped(child, root, ignored):
            continue
        rel = _relative(child, root)
        if _matches_any_glob(rel, child.name, patterns):
            yield child


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


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


async def glob_project(
    arguments: dict[str, Any],
    project_root: Path | None = None,
) -> dict[str, Any]:
    pattern = str(arguments.get("pattern", "")).strip()
    if not pattern:
        return {"ok": False, "error": "Missing pattern."}
    try:
        root = (project_root or PROJECT_ROOT).resolve()
        path = _resolve_inside_project(str(arguments.get("path", ".")), root)
        ignore = _as_string_list(arguments.get("ignore"))
        max_results = int(arguments.get("max_results", 100))
        matches: list[str] = []
        for child in _iter_project_files(path, root, glob_patterns=[pattern], ignore=ignore):
            matches.append(_relative(child, root))
            if len(matches) >= max_results:
                break
        return {"ok": True, "matches": matches, "content": "\n".join(matches)}
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


async def read_many_files(
    arguments: dict[str, Any],
    project_root: Path | None = None,
) -> dict[str, Any]:
    paths = arguments.get("paths")
    if not isinstance(paths, list) or not paths:
        return {"ok": False, "error": "Missing paths."}
    try:
        root = (project_root or PROJECT_ROOT).resolve()
        max_chars = int(arguments.get("max_chars_per_file", 12_000))
        files = []
        sections = []
        for raw_path in paths:
            path = _resolve_inside_project(str(raw_path), root)
            content = _read_text(path)
            truncated = len(content) > max_chars
            if truncated:
                content = content[:max_chars]
            rel = _relative(path, root)
            files.append({"path": rel, "content": content, "truncated": truncated})
            suffix = "\n[truncated]" if truncated else ""
            sections.append(f"===== {rel} =====\n{content}{suffix}")
        return {"ok": True, "files": files, "content": "\n\n".join(sections)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def file_info(
    arguments: dict[str, Any],
    project_root: Path | None = None,
) -> dict[str, Any]:
    try:
        root = (project_root or PROJECT_ROOT).resolve()
        path = _resolve_inside_project(str(arguments.get("path", "")), root)
        exists = path.exists()
        info: dict[str, Any] = {
            "path": _relative(path, root) if exists else str(arguments.get("path", "")),
            "exists": exists,
        }
        if exists:
            stat = path.stat()
            info.update(
                {
                    "is_file": path.is_file(),
                    "is_dir": path.is_dir(),
                    "size_bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                }
            )
        return {"ok": True, **info, "content": "\n".join(f"{key}: {value}" for key, value in info.items())}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def edit_file(
    arguments: dict[str, Any],
    project_root: Path | None = None,
) -> dict[str, Any]:
    try:
        root = (project_root or PROJECT_ROOT).resolve()
        path = _resolve_inside_project(str(arguments.get("path", "")), root)
        old_string = str(arguments.get("old_string", ""))
        new_string = str(arguments.get("new_string", ""))
        replace_all = bool(arguments.get("replace_all", False))
        if not old_string:
            return {"ok": False, "error": "old_string must not be empty."}
        content = _read_text(path)
        count = content.count(old_string)
        if count == 0:
            return {"ok": False, "error": "old_string not found."}
        if not replace_all and count > 1:
            return {"ok": False, "error": f"old_string matched {count} times; provide a more precise string or set replace_all=true."}
        updated = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)
        path.write_text(updated, encoding="utf-8")
        replacements = count if replace_all else 1
        return {
            "ok": True,
            "changed": updated != content,
            "replacements": replacements,
            "content": f"Edited {_relative(path, root)}: replacements={replacements}",
        }
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
        output_mode = str(arguments.get("output_mode", "content") or "content")
        if output_mode not in {"content", "files", "count"}:
            return {"ok": False, "error": "output_mode must be one of: content, files, count."}
        glob_patterns = _as_string_list(arguments.get("glob"))
        ignore = _as_string_list(arguments.get("ignore"))
        case_insensitive = bool(arguments.get("case_insensitive", False))
        context_lines = max(0, int(arguments.get("context_lines", 0)))
        flags = re.IGNORECASE if case_insensitive else 0
        regex = re.compile(pattern, flags)
        matches = []
        file_counts: dict[str, int] = {}
        matched_files: list[str] = []
        for file_path in _iter_project_files(path, root, glob_patterns=glob_patterns, ignore=ignore):
            rel = _relative(file_path, root)
            try:
                lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
            except Exception:
                continue
            file_match_count = 0
            for index, line in enumerate(lines):
                found = regex.search(line)
                if not found:
                    continue
                file_match_count += 1
                if rel not in matched_files:
                    matched_files.append(rel)
                if output_mode == "content" and len(matches) < max_matches:
                    before_start = max(0, index - context_lines)
                    after_end = min(len(lines), index + context_lines + 1)
                    context = [
                        {"line": line_number + 1, "text": lines[line_number]}
                        for line_number in range(before_start, after_end)
                    ]
                    matches.append(
                        {
                            "path": rel,
                            "line": index + 1,
                            "column": found.start() + 1,
                            "text": line,
                            "context": context if context_lines else [],
                        }
                    )
                if output_mode == "content" and len(matches) >= max_matches:
                    break
            if file_match_count:
                file_counts[rel] = file_match_count
            if output_mode == "content" and len(matches) >= max_matches:
                break

        if output_mode == "files":
            files = matched_files[:max_matches]
            return {"ok": True, "files": files, "content": "\n".join(files)}
        if output_mode == "count":
            total = sum(file_counts.values())
            content = "\n".join(f"{path}: {count}" for path, count in file_counts.items())
            return {"ok": True, "count": total, "file_counts": file_counts, "content": content}
        content = "\n".join(f"{match['path']}:{match['line']}:{match['column']}: {match['text']}" for match in matches)
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


def glob_project_spec() -> dict[str, Any]:
    return {
        "name": "glob_project",
        "description": "按 glob 文件名模式查找当前项目内文件，支持 ignore 和最大结果数。",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "glob 模式，例如 **/*.py。"},
                "path": {"type": "string", "description": "搜索起点，默认 .。"},
                "ignore": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "忽略的 glob 模式。",
                },
                "max_results": {"type": "integer", "description": "最多返回数量，默认 100。"},
            },
            "required": ["pattern"],
        },
    }


def grep_project_spec() -> dict[str, Any]:
    return {
        "name": "grep_project",
        "description": "类似 grep/rg。在当前项目内搜索文本，支持输出匹配内容、文件列表或计数。",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "要搜索的文本或正则表达式。"},
                "path": {"type": "string", "description": "相对项目根目录的搜索路径，默认 .。"},
                "max_matches": {"type": "integer", "description": "最多返回多少条匹配，默认 50。"},
                "output_mode": {
                    "type": "string",
                    "enum": ["content", "files", "count"],
                    "description": "输出模式：匹配内容、文件列表或计数。",
                },
                "glob": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "只搜索匹配这些 glob 的文件。",
                },
                "ignore": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "忽略的 glob 模式。",
                },
                "case_insensitive": {"type": "boolean", "description": "是否忽略大小写。"},
                "context_lines": {"type": "integer", "description": "每条匹配返回前后多少行上下文。"},
            },
            "required": ["pattern"],
        },
    }


def read_many_files_spec() -> dict[str, Any]:
    return {
        "name": "read_many_files",
        "description": "一次读取多个项目内文本文件，适合批量代码审查。",
        "parameters": {
            "type": "object",
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"}, "description": "相对项目根目录的文件路径列表。"},
                "max_chars_per_file": {"type": "integer", "description": "每个文件最多读取多少字符，默认 12000。"},
            },
            "required": ["paths"],
        },
    }


def file_info_spec() -> dict[str, Any]:
    return {
        "name": "file_info",
        "description": "查看项目内文件或目录的元信息。",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "相对项目根目录的文件或目录路径。"}},
            "required": ["path"],
        },
    }


def edit_file_spec() -> dict[str, Any]:
    return {
        "name": "edit_file",
        "description": "在当前项目内对文本文件做精确字符串替换。适合小范围修改。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对项目根目录的文件路径。"},
                "old_string": {"type": "string", "description": "要被替换的精确原文。"},
                "new_string": {"type": "string", "description": "替换后的文本。"},
                "replace_all": {"type": "boolean", "description": "是否替换所有匹配，默认 false。"},
            },
            "required": ["path", "old_string", "new_string"],
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
