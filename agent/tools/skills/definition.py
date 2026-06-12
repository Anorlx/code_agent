from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal


SkillContext = Literal["inline", "fork"]
PromptGenerator = Callable[["SkillDefinition", dict[str, Any]], str]
EnabledCallback = Callable[[], bool]
LifecycleHook = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class SkillFile:
    path: str
    content: str


@dataclass(frozen=True)
class SkillDefinition:
    name: str
    description: str
    when_to_use: str = ""
    aliases: tuple[str, ...] = ()
    arguments: str = ""
    argument_hint: str = ""
    allowed_tools: tuple[str, ...] = ()
    model: str | None = None
    effort: str | None = None
    user_invocable: bool = True
    disable_model_invocation: bool = False
    context: SkillContext = "inline"
    agent: str | None = None
    version: str = "1.0"
    paths: tuple[str, ...] = ()
    files: tuple[SkillFile, ...] = ()
    prompt_generator: PromptGenerator | None = None
    is_enabled: EnabledCallback | None = None
    pre_hooks: tuple[LifecycleHook, ...] = ()
    post_hooks: tuple[LifecycleHook, ...] = ()
    source_dir: Path | None = None

    @property
    def slug(self) -> str:
        return self.name.strip().lower().replace("_", "-")

    @property
    def tool_name(self) -> str:
        return "skill_" + self.slug.replace("-", "_")

    def enabled(self) -> bool:
        return True if self.is_enabled is None else bool(self.is_enabled())


def render_skill_prompt(skill: SkillDefinition, arguments: str, extracted_dir: Path | None = None) -> str:
    context = {
        "arguments": arguments,
        "argv": arguments.split(),
        "extracted_dir": extracted_dir,
    }
    if skill.prompt_generator is not None:
        return skill.prompt_generator(skill, context)
    return _default_prompt(skill, context)


def _default_prompt(skill: SkillDefinition, context: dict[str, Any]) -> str:
    arguments = str(context.get("arguments") or "")
    extracted_dir = context.get("extracted_dir")
    lines = [
        f"# Skill: {skill.name}",
        "",
        skill.description,
    ]
    if skill.when_to_use:
        lines.extend(["", f"When to use: {skill.when_to_use}"])
    if skill.allowed_tools:
        lines.extend(["", "Allowed tools: " + ", ".join(skill.allowed_tools)])
    if extracted_dir:
        lines.extend(["", f"Reference files are available at: {extracted_dir}"])
    if arguments:
        lines.extend(["", "User arguments:", arguments])
    lines.extend(["", "Follow this skill instruction and produce the requested result."])
    return "\n".join(lines)
