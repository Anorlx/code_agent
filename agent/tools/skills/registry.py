from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from agent.main_agent.config import AGENT_DATA_ROOT, PROJECT_ROOT
from agent.tools.skills.bundled import init_bundled_skills
from agent.tools.skills.definition import SkillDefinition, render_skill_prompt
from agent.tools.skills.parser import load_file_skills


SKILL_DATA_ROOT = AGENT_DATA_ROOT / "bundled_skills"
USER_SKILLS_ROOT = PROJECT_ROOT / ".agents" / "skills"

_extract_tasks: dict[str, asyncio.Task[Path | None]] = {}


def load_skill_registry() -> dict[str, SkillDefinition]:
    skills = init_bundled_skills()
    skills.update(load_file_skills(USER_SKILLS_ROOT))
    return skills


def skill_catalog_text(skills: dict[str, SkillDefinition] | None = None) -> str:
    registry = skills or load_skill_registry()
    if not registry:
        return "Skills: 未注册。"
    lines = ["Skills:"]
    for skill in sorted(registry.values(), key=lambda item: item.name):
        aliases = f" aliases={','.join(skill.aliases)}" if skill.aliases else ""
        lines.append(
            f"- /{skill.name}: {skill.description} "
            f"(tool={skill.tool_name}, context={skill.context}, user_invocable={skill.user_invocable}{aliases})"
        )
    return "\n".join(lines)


def resolve_skill_slash(command: str, skills: dict[str, SkillDefinition] | None = None) -> SkillDefinition | None:
    name = command.strip().lower().lstrip("/")
    if not name:
        return None
    registry = skills or load_skill_registry()
    normalized = name.replace("_", "-")
    if normalized in registry:
        return registry[normalized]
    for skill in registry.values():
        if normalized in {alias.lower().replace("_", "-") for alias in skill.aliases}:
            return skill
    return None


def skill_tool_spec(skill: SkillDefinition) -> dict[str, Any]:
    return {
        "name": skill.tool_name,
        "description": f"调用内置技能 /{skill.name}: {skill.description} 使用场景: {skill.when_to_use}",
        "parameters": {
            "type": "object",
            "properties": {
                "arguments": {
                    "type": "string",
                    "description": skill.argument_hint or "传给技能的原始参数文本。",
                }
            },
            "required": ["arguments"],
        },
    }


def _safe_write_file(path: Path, content: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)
    path.chmod(0o600)


async def _extract_skill_files(skill: SkillDefinition) -> Path | None:
    if not skill.files:
        return None
    target_dir = SKILL_DATA_ROOT / skill.slug
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target_dir.chmod(0o700)
    for item in skill.files:
        target = (target_dir / item.path).resolve()
        if target != target_dir and target_dir not in target.parents:
            raise ValueError(f"Skill file path escapes skill directory: {item.path}")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.parent.chmod(0o700)
        if target.exists():
            target.chmod(0o600)
            continue
        _safe_write_file(target, item.content)
    return target_dir


async def extract_skill_files_once(skill: SkillDefinition) -> Path | None:
    if not skill.files:
        return None
    task = _extract_tasks.get(skill.slug)
    if task is None:
        task = asyncio.create_task(_extract_skill_files(skill))
        _extract_tasks[skill.slug] = task
    return await task


async def run_skill_tool(arguments: dict[str, Any], skill: SkillDefinition) -> dict[str, Any]:
    if not skill.enabled():
        return {"ok": False, "error": f"Skill disabled: {skill.name}"}
    for hook in skill.pre_hooks:
        await hook(arguments)
    extracted_dir = await extract_skill_files_once(skill)
    prompt = render_skill_prompt(skill, str(arguments.get("arguments", "")), extracted_dir)
    for hook in skill.post_hooks:
        await hook(arguments)
    return {
        "ok": True,
        "skill": skill.name,
        "context": skill.context,
        "allowed_tools": list(skill.allowed_tools),
        "model": skill.model,
        "disable_model_invocation": skill.disable_model_invocation,
        "reference_dir": extracted_dir.as_posix() if extracted_dir else None,
        "content": prompt,
    }


def make_skill_runner(skill: SkillDefinition):
    async def run(arguments: dict[str, Any]) -> dict[str, Any]:
        return await run_skill_tool(arguments, skill)

    return run
