from __future__ import annotations

import math
import re
import time
from pathlib import Path
from typing import Any

from agent.main_agent.config import MEMORY_ROOT

MEMORY_TYPES = ("user", "feedback", "project", "reference")
INDEX_FILENAME = "MEMORY.md"
DEFAULT_TTL_DAYS: dict[str, int | None] = {
    "user": None,
    "feedback": None,
    "project": 365,
    "reference": 180,
}
DEFAULT_SALIENCE = {
    "high": 1.0,
    "medium": 0.72,
}
SALIENCE_HALF_LIFE_DAYS = 90
FORGET_THRESHOLD = 0.18
SECRET_PATTERNS = (
    re.compile(r"(?i)\b(api[_-]?key|token|password|passwd|secret|private[_-]?key)\b"),
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def _slugify(value: str) -> str:
    text = value.strip().lower()
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or "memory"


def _yaml_scalar(value: str) -> str:
    return str(value).replace("\n", " ").replace('"', "'").strip()


def _now() -> float:
    return time.time()


def _days_to_seconds(days: int | None) -> float | None:
    if days is None:
        return None
    return float(days) * 24 * 60 * 60


def _float_meta(meta: dict[str, str], key: str, default: float = 0.0) -> float:
    try:
        return float(meta.get(key, default))
    except (TypeError, ValueError):
        return default


def _int_meta(meta: dict[str, str], key: str, default: int = 0) -> int:
    try:
        return int(float(meta.get(key, default)))
    except (TypeError, ValueError):
        return default


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text
    values: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip().strip('"')
    body = text[end + len("\n---") :].lstrip("\n")
    return values, body


def _frontmatter_text(meta: dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in meta.items():
        lines.append(f"{key}: {_yaml_scalar(str(value))}")
    lines.extend(["---", ""])
    return "\n".join(lines)


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
    values, _ = _split_frontmatter(text)
    return values


def _memory_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for memory_type in MEMORY_TYPES:
        files.extend(sorted((root / memory_type).glob("*.md")))
    return files


def _effective_salience(meta: dict[str, str], now: float | None = None) -> float:
    current_time = now or _now()
    salience = _float_meta(meta, "salience", DEFAULT_SALIENCE.get(meta.get("confidence", "medium"), 0.72))
    use_count = _int_meta(meta, "use_count", 0)
    last_used = _float_meta(meta, "last_used_at", _float_meta(meta, "updated_at", _float_meta(meta, "created_at", current_time)))
    idle_days = max(0.0, (current_time - last_used) / 86400)
    decay = math.pow(0.5, idle_days / SALIENCE_HALF_LIFE_DAYS)
    usage_boost = min(0.3, use_count * 0.03)
    return max(0.0, min(1.0, salience * decay + usage_boost))


def _should_forget(path: Path, meta: dict[str, str], now: float | None = None) -> tuple[bool, str]:
    current_time = now or _now()
    status = meta.get("status", "active")
    if status == "deleted":
        return True, "deleted"
    expires_at = _float_meta(meta, "expires_at", 0.0)
    if expires_at and expires_at <= current_time:
        return True, "ttl_expired"
    if meta.get("type") in {"user", "feedback"} and not expires_at:
        return False, ""
    score = _effective_salience(meta, current_time)
    use_count = _int_meta(meta, "use_count", 0)
    created_at = _float_meta(meta, "created_at", current_time)
    age_days = max(0.0, (current_time - created_at) / 86400)
    if age_days >= SALIENCE_HALF_LIFE_DAYS and use_count == 0 and score < FORGET_THRESHOLD:
        return True, "salience_decay"
    return False, ""


def forget_stale_memories(memory_root: Path | None = None, now: float | None = None) -> list[dict[str, str]]:
    root = ensure_memory_tree(memory_root)
    forgotten: list[dict[str, str]] = []
    for path in _memory_files(root):
        meta = _parse_frontmatter(path)
        should_forget, reason = _should_forget(path, meta, now)
        if not should_forget:
            continue
        relative_path = path.relative_to(root).as_posix()
        path.unlink()
        forgotten.append({"path": relative_path, "reason": reason})
    return forgotten


def rebuild_memory_index(memory_root: Path | None = None) -> Path:
    root = ensure_memory_tree(memory_root)
    forget_stale_memories(root)
    lines = ["# Memory Index", ""]
    for path in _memory_files(root):
        meta = _parse_frontmatter(path)
        title = meta.get("title") or meta.get("name") or path.stem.replace("-", " ").title()
        description = meta.get("description") or ""
        score = _effective_salience(meta)
        relative_path = path.relative_to(root).as_posix()
        suffix_parts = []
        if description:
            suffix_parts.append(description)
        suffix_parts.append(f"score={score:.2f}")
        suffix = " -- " + " | ".join(suffix_parts)
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


def _contains_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def read_memory_file(relative_path: str, memory_root: Path | None = None) -> str:
    path = _resolve_inside_memory(relative_path, memory_root)
    mark_memory_used(relative_path, memory_root)
    return path.read_text(encoding="utf-8")


def mark_memory_used(relative_path: str, memory_root: Path | None = None) -> None:
    path = _resolve_inside_memory(relative_path, memory_root)
    text = path.read_text(encoding="utf-8")
    meta, body = _split_frontmatter(text)
    if not meta:
        return
    now = _now()
    use_count = _int_meta(meta, "use_count", 0) + 1
    salience = min(1.0, _float_meta(meta, "salience", 0.72) + 0.04)
    meta["use_count"] = str(use_count)
    meta["last_used_at"] = str(now)
    meta["salience"] = f"{salience:.3f}"
    path.write_text(_frontmatter_text(meta) + body.rstrip() + "\n", encoding="utf-8")
    rebuild_memory_index(memory_root)


def delete_memory(relative_path: str, memory_root: Path | None = None) -> Path:
    path = _resolve_inside_memory(relative_path, memory_root)
    if path.name == INDEX_FILENAME or path.suffix != ".md":
        raise ValueError("Only concrete memory Markdown files can be deleted.")
    path.unlink()
    rebuild_memory_index(memory_root)
    return path


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
    combined = "\n".join([title, description, content])
    if _contains_secret(combined):
        raise ValueError("Memory appears to contain sensitive credentials.")

    slug = _slugify(str(memory.get("name") or title))
    path = root / memory_type / f"{slug}.md"
    replace_path = str(memory.get("replace_path") or "").strip()
    if replace_path:
        old_path = _resolve_inside_memory(replace_path, root)
        if old_path.suffix == ".md" and old_path.exists() and old_path != path:
            old_path.unlink()

    name = _slugify(str(memory.get("name") or title))
    scope = str(memory.get("scope") or "project-local").strip()
    confidence = str(memory.get("confidence") or "medium").strip()
    now = _now()
    ttl_days = memory.get("ttl_days", DEFAULT_TTL_DAYS.get(memory_type))
    try:
        ttl_days = int(ttl_days) if ttl_days is not None and str(ttl_days).strip() else None
    except (TypeError, ValueError):
        ttl_days = DEFAULT_TTL_DAYS.get(memory_type)
    ttl_seconds = _days_to_seconds(ttl_days)
    expires_at = now + ttl_seconds if ttl_seconds is not None else ""
    salience = memory.get("salience", DEFAULT_SALIENCE.get(confidence, 0.72))
    try:
        salience = max(0.0, min(1.0, float(salience)))
    except (TypeError, ValueError):
        salience = DEFAULT_SALIENCE.get(confidence, 0.72)
    meta = {
        "name": name,
        "title": title,
        "description": description,
        "type": memory_type,
        "scope": scope,
        "confidence": confidence,
        "created_at": str(now),
        "updated_at": str(now),
        "last_used_at": "",
        "use_count": "0",
        "salience": f"{salience:.3f}",
        "ttl_days": "" if ttl_days is None else str(ttl_days),
        "expires_at": "" if expires_at == "" else str(expires_at),
        "status": "active",
    }
    if replace_path:
        meta["replaces"] = replace_path
    path.write_text(_frontmatter_text(meta) + content.rstrip() + "\n", encoding="utf-8")
    rebuild_memory_index(root)
    return path
