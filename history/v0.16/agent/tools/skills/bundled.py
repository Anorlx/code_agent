from __future__ import annotations

from typing import Any

from agent.tools.skills.definition import SkillDefinition, SkillFile


VERIFY_GUIDE = """# Verify Skill Reference

Verification should prefer concrete evidence:

1. Inspect the current diff or changed files.
2. Run the smallest meaningful test/lint/typecheck/build command.
3. Read the changed code when command output is insufficient.
4. Summarize what passed, what failed and what remains risky.

Never claim a change is verified without stating the evidence.
"""


DEBUG_GUIDE = """# Debug Skill Reference

Debugging flow:

1. Reproduce or identify the failing signal.
2. Separate symptom from cause.
3. Inspect the smallest relevant code path.
4. Form one hypothesis at a time.
5. Validate with a command, log, test or code read.
"""


def _skill_prompt(skill: SkillDefinition, context: dict[str, Any]) -> str:
    arguments = str(context.get("arguments") or "").strip()
    argv = context.get("argv") or []
    extracted_dir = context.get("extracted_dir")
    lines = [
        f"# Bundled Skill: {skill.name}",
        "",
        f"Description: {skill.description}",
    ]
    if skill.when_to_use:
        lines.append(f"When to use: {skill.when_to_use}")
    if skill.argument_hint:
        lines.append(f"Arguments: {skill.argument_hint}")
    if skill.allowed_tools:
        lines.append("Allowed tools: " + ", ".join(skill.allowed_tools))
    if skill.context == "fork":
        lines.append("Execution context: fork. Use an isolated subtask mindset and return a compact result.")
    if skill.disable_model_invocation:
        lines.append("Model invocation disabled: answer procedurally without asking another model.")
    if extracted_dir:
        lines.append(f"Reference files: {extracted_dir}")
    lines.extend(["", "User arguments:", arguments or "(none)", ""])
    lines.append(_body_for_skill(skill.name, arguments, argv))
    return "\n".join(lines)


def _body_for_skill(name: str, arguments: str, argv: list[str]) -> str:
    if name == "update-config":
        return (
            "Help update local configuration safely. Identify which setting the user wants to change, "
            "read the relevant settings file first, explain the intended edit, and only use write/edit tools after permission review."
        )
    if name == "keybindings-help":
        return (
            "Explain current terminal keybinding customization options. If project keybinding files exist, inspect them; "
            "otherwise provide a concise guide for prompt_toolkit-style bindings and where they could be configured."
        )
    if name == "verify":
        return (
            "Verify the current code change. Prefer git diff, targeted tests, lint/typecheck/build, and changed-file inspection. "
            "Return evidence, failures, residual risks and next actions."
        )
    if name == "debug":
        return (
            "Act as a debugging assistant. Parse the error, identify the likely failing subsystem, form hypotheses, "
            "choose the smallest inspection or command, and avoid broad speculative rewrites."
        )
    if name == "simplify":
        return (
            "Review the target code for unnecessary complexity, duplication and confusing control flow. "
            "Suggest or perform small refactors that preserve behavior and improve readability."
        )
    if name == "skillify":
        return (
            "Turn the user's prompt into a reusable SKILL.md. Produce frontmatter plus a Markdown body. "
            "Include description, when_to_use, arguments, allowed-tools and clear usage instructions."
        )
    if name == "remember":
        return (
            "Convert the user's statement into a memory candidate only if it is long-term, non-derivable and useful across sessions. "
            "If valid, use save_memory; otherwise explain why it should not be persisted."
        )
    if name == "batch":
        return (
            "Plan a batch file operation. First discover target files with glob/grep, summarize the scope, then ask for confirmation "
            "before edits or commands. Prefer dry-run style output when possible."
        )
    if name == "stuck":
        return (
            "Help the agent escape a loop. Restate the goal, list known facts, identify repeated failed moves, propose one narrower next step, "
            "and avoid continuing the same strategy."
        )
    if name == "lorem-ipsum":
        count = argv[0] if argv else "3"
        return (
            f"Generate placeholder content for UI development. Create about {count} short paragraphs unless the user requested another format. "
            "Keep it visibly placeholder-like and avoid pretending it is real copy."
        )
    return "Use the skill description to guide the next response."


def init_bundled_skills() -> dict[str, SkillDefinition]:
    skills = [
        SkillDefinition(
            name="update-config",
            description="配置 settings.json 的各项设置。",
            when_to_use="用户想修改权限规则、环境变量、默认模型或本地配置时。",
            aliases=("config", "settings"),
            argument_hint="[setting] [value]",
            allowed_tools=("Read", "Grep", "Edit", "Write"),
            context="inline",
            prompt_generator=_skill_prompt,
        ),
        SkillDefinition(
            name="keybindings-help",
            description="键盘快捷键自定义帮助。",
            when_to_use="用户想查看、解释或自定义终端按键绑定时。",
            aliases=("keys", "keymap"),
            argument_hint="[topic]",
            allowed_tools=("Read", "Grep"),
            disable_model_invocation=False,
            context="inline",
            prompt_generator=_skill_prompt,
        ),
        SkillDefinition(
            name="verify",
            description="验证代码变更的正确性。",
            when_to_use="提交前最终验证、CI 前本地检查、确认改动是否真的生效。",
            aliases=("check", "validate"),
            argument_hint="[scope]",
            allowed_tools=("Read", "Grep", "Bash"),
            effort="high",
            context="fork",
            files=(SkillFile("verify-guide.md", VERIFY_GUIDE),),
            prompt_generator=_skill_prompt,
        ),
        SkillDefinition(
            name="debug",
            description="调试辅助，提供诊断思路。",
            when_to_use="定位 bug 根因、分析错误堆栈、排查失败命令。",
            aliases=("diagnose",),
            argument_hint="[error-or-symptom]",
            allowed_tools=("Read", "Grep", "Bash"),
            effort="high",
            context="fork",
            files=(SkillFile("debug-guide.md", DEBUG_GUIDE),),
            prompt_generator=_skill_prompt,
        ),
        SkillDefinition(
            name="simplify",
            description="代码简化与重构审查。",
            when_to_use="用户想消除重复代码、降低复杂度或做小范围重构审查时。",
            aliases=("refactor-review",),
            argument_hint="[path-or-topic]",
            allowed_tools=("Read", "Grep", "Edit"),
            effort="medium",
            context="inline",
            prompt_generator=_skill_prompt,
        ),
        SkillDefinition(
            name="skillify",
            description="将 prompt 转换为可复用的技能。",
            when_to_use="用户想把一次性的 prompt 模板化成 SKILL.md 时。",
            aliases=("make-skill",),
            argument_hint="[prompt]",
            allowed_tools=("Read", "Write"),
            effort="medium",
            context="inline",
            prompt_generator=_skill_prompt,
        ),
        SkillDefinition(
            name="remember",
            description="记忆管理，用于添加项目规范、团队约定或长期偏好。",
            when_to_use="用户明确要求记住某条长期规则、偏好或项目决策时。",
            aliases=("memory",),
            argument_hint="[memory]",
            allowed_tools=("Read", "Write"),
            context="inline",
            prompt_generator=_skill_prompt,
        ),
        SkillDefinition(
            name="batch",
            description="批量文件处理。",
            when_to_use="用户要批量重命名、批量格式化、批量替换或批量扫描文件时。",
            aliases=("bulk",),
            argument_hint="[operation] [pattern]",
            allowed_tools=("Read", "Grep", "Glob", "Edit", "Bash"),
            effort="medium",
            context="fork",
            prompt_generator=_skill_prompt,
        ),
        SkillDefinition(
            name="stuck",
            description="帮助模型走出困境。",
            when_to_use="模型陷入循环、反复失败或用户要求重新整理思路时。",
            aliases=("unstuck",),
            argument_hint="[current-problem]",
            allowed_tools=("Read", "Grep"),
            context="inline",
            prompt_generator=_skill_prompt,
        ),
        SkillDefinition(
            name="lorem-ipsum",
            description="生成占位内容。",
            when_to_use="UI 开发时需要占位标题、段落、表格或测试文案。",
            aliases=("placeholder", "lorem"),
            argument_hint="[paragraphs-or-format]",
            allowed_tools=(),
            disable_model_invocation=False,
            context="inline",
            prompt_generator=_skill_prompt,
        ),
    ]
    return {skill.slug: skill for skill in skills if skill.enabled()}


def initBundledSkills() -> dict[str, SkillDefinition]:
    return init_bundled_skills()
