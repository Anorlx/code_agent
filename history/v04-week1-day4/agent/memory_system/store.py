from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from agent.main_agent.config import MEMORY_ROOT

MEMORY_TYPES = ("user", "feedback", "project", "reference")
INDEX_FILENAME = "MEMORY.md"


def _slugify(value: str) -> str:
    text = value.strip().lower()
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or "memory"


def _yaml_scalar(value: str) -> str:
    return str(value).replace("\n", " ").replace('"', "'").strip()


def ensure_memory_tree(memory_root: Path | None = None) -> Path:
    root = memory_root or MEMORY_ROOT
    root.mkdir(parents=True, exist_ok=True)
    for memory_type in MEMORY_TYPES:
        (root / memory_type).mkdir(parents=True, exist_ok=True)
    index_path = root / INDEX_FILENAME
    if not index_path.exists():
        index_path.write_text("# Memory Index\n\n", encoding="utf-8")
    return root


def _parse_frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end < 0:
        return {}
    values: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip().strip('"')
    return values


def _memory_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for memory_type in MEMORY_TYPES:
        files.extend(sorted((root / memory_type).glob("*.md")))
    return files


def rebuild_memory_index(memory_root: Path | None = None) -> Path:
    root = ensure_memory_tree(memory_root)
    lines = ["# Memory Index", ""]
    for path in _memory_files(root):
        meta = _parse_frontmatter(path)
        title = meta.get("title") or meta.get("name") or path.stem.replace("-", " ").title()
        description = meta.get("description") or ""
        relative_path = path.relative_to(root).as_posix()
        suffix = f" -- {description}" if description else ""
        lines.append(f"- [{title}]({relative_path}){suffix}")
    lines.append("")
    index_path = root / INDEX_FILENAME
    index_path.write_text("\n".join(lines), encoding="utf-8")
    return index_path


def load_memory_index(memory_root: Path | None = None) -> str:
    root = ensure_memory_tree(memory_root)
    rebuild_memory_index(root)
    return (root / INDEX_FILENAME).read_text(encoding="utf-8")


def _resolve_inside_memory(relative_path: str, memory_root: Path | None = None) -> Path:
    root = ensure_memory_tree(memory_root)
    root_resolved = root.resolve()
    target = (root / relative_path).resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise ValueError("Path is outside memory directory.")
    return target


def read_memory_file(relative_path: str, memory_root: Path | None = None) -> str:
    path = _resolve_inside_memory(relative_path, memory_root)
    return path.read_text(encoding="utf-8")


def write_memory(memory: dict[str, Any], memory_root: Path | None = None) -> Path:
    root = ensure_memory_tree(memory_root)
    memory_type = str(memory.get("type", "")).strip()
    if memory_type not in MEMORY_TYPES:
        raise ValueError(f"Invalid memory type: {memory_type}")

    title = str(memory.get("title") or memory.get("name") or "").strip()
    description = str(memory.get("description") or "").strip()
    content = str(memory.get("content") or "").strip()
    if not title:
        raise ValueError("Memory title is required.")
    if not description:
        raise ValueError("Memory description is required.")
    if not content:
        raise ValueError("Memory content is required.")

    slug = _slugify(str(memory.get("name") or title))
    path = root / memory_type / f"{slug}.md"
    name = _slugify(str(memory.get("name") or title))
    frontmatter = "\n".join(
        [
            "---",
            f"name: {name}",
            f"title: {_yaml_scalar(title)}",
            f"description: {_yaml_scalar(description)}",
            f"type: {memory_type}",
            "---",
            "",
        ]
    )
    path.write_text(frontmatter + content.rstrip() + "\n", encoding="utf-8")
    rebuild_memory_index(root)
    return path
