from __future__ import annotations

from pathlib import Path
from typing import Any

from agent.tools.skills.definition import SkillDefinition


def _parse_scalar(value: str) -> Any:
    text = value.strip()
    if text in {"true", "True"}:
        return True
    if text in {"false", "False"}:
        return False
    if len(text) >= 2 and text[0] in {"'", '"'} and text[-1] == text[0]:
        return text[1:-1]
    return text


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text
    raw = text[4:end].splitlines()
    body = text[end + len("\n---") :].lstrip("\n")
    data: dict[str, Any] = {}
    current_key: str | None = None
    for line in raw:
        if not line.strip():
            continue
        if line.startswith("  - ") and current_key:
            data.setdefault(current_key, []).append(_parse_scalar(line[4:]))
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value:
            data[key] = _parse_scalar(value)
            current_key = None
        else:
            data[key] = []
            current_key = key
    return data, body


def _as_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    if isinstance(value, str) and value.strip():
        return tuple(part for part in value.split() if part)
    return ()


def load_skill_from_dir(path: Path) -> SkillDefinition | None:
    skill_file = path / "SKILL.md"
    if not skill_file.exists():
        return None
    metadata, body = _parse_frontmatter(skill_file.read_text(encoding="utf-8"))
    name = str(metadata.get("name") or path.name).strip()
    description = str(metadata.get("description") or "").strip()
    if not description:
        return None

    def prompt_generator(skill: SkillDefinition, context: dict[str, Any]) -> str:
        arguments = str(context.get("arguments") or "")
        argv = list(context.get("argv") or [])
        rendered = body.replace("$ARGUMENTS", arguments)
        for index, value in enumerate(argv, start=1):
            rendered = rendered.replace(f"${index}", str(value))
        rendered = rendered.replace("${CLAUDE_SKILL_DIR}", str(skill.source_dir or ""))
        return rendered

    return SkillDefinition(
        name=name,
        description=description,
        when_to_use=str(metadata.get("when_to_use") or ""),
        aliases=_as_tuple(metadata.get("aliases")),
        arguments=str(metadata.get("arguments") or ""),
        argument_hint=str(metadata.get("argument-hint") or ""),
        allowed_tools=_as_tuple(metadata.get("allowed-tools")),
        model=str(metadata["model"]) if metadata.get("model") else None,
        effort=str(metadata["effort"]) if metadata.get("effort") else None,
        user_invocable=bool(metadata.get("user-invocable", True)),
        disable_model_invocation=bool(metadata.get("disable-model-invocation", False)),
        context="fork" if str(metadata.get("context") or "inline") == "fork" else "inline",
        agent=str(metadata["agent"]) if metadata.get("agent") else None,
        version=str(metadata.get("version") or "1.0"),
        paths=_as_tuple(metadata.get("paths")),
        source_dir=path,
        prompt_generator=prompt_generator,
    )


def load_file_skills(root: Path) -> dict[str, SkillDefinition]:
    if not root.exists():
        return {}
    skills: dict[str, SkillDefinition] = {}
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        skill = load_skill_from_dir(child)
        if skill is not None and skill.enabled():
            skills[skill.slug] = skill
    return skills
