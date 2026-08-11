# Skills Tools

这里是 skills 工具层。Skills 会和内置工具、MCP 工具一样注册进统一工具池。

## Bundled skills

启动时通过 `init_bundled_skills()` / `initBundledSkills()` 注册内置技能：

- `update-config`
- `keybindings-help`
- `verify`
- `debug`
- `simplify`
- `skillify`
- `remember`
- `batch`
- `stuck`
- `lorem-ipsum`

注册后工具名会变成 `skill_<skill_name>`，例如：

- `/verify` -> `skill_verify`
- `/update-config` -> `skill_update_config`
- `/lorem-ipsum` -> `skill_lorem_ipsum`

技能工具本身只生成 prompt 和引用文件路径，不直接修改项目；真正的文件写入、命令执行、MCP 调用仍然走普通工具权限管线。

## File skills

文件型技能目录格式：

```text
.agents/skills/my-skill/SKILL.md
```

`SKILL.md` 支持 YAML frontmatter：

```yaml
---
name: my-skill
description: 示例技能
when_to_use: 用户需要执行某操作时
arguments: arg1 arg2
argument-hint: "[arg1] [arg2]"
allowed-tools:
  - Read
  - Grep
model: inherit
effort: medium
user-invocable: true
disable-model-invocation: false
context: fork
agent: code-builder
version: "1.0"
paths:
  - "src/**/*.py"
---

# Skill Body

Use $ARGUMENTS, $1, $2 and ${CLAUDE_SKILL_DIR}.
```

## Secure file extraction

Bundled skills can carry reference files. They are extracted lazily on first use into:

```text
.agent_data/bundled_skills/<skill-name>/
```

Extraction is protected by:

- per-process singleton task, so concurrent calls share one extraction.
- directory mode `0o700`.
- file mode `0o600`.
- `O_NOFOLLOW` when available, to avoid symlink following.
- `O_EXCL`, so extraction creates new files instead of silently overwriting existing paths.
