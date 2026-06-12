from __future__ import annotations

import json
from pathlib import Path
from typing import Any, AsyncGenerator, Callable

from agent.main_agent.config import DEFAULT_SUB_AGENT_MODEL


PERMISSION_REVIEW_SYSTEM_PROMPT = """你是 permission_review。
你的任务是在工具真正执行前审查它是否会对本地项目造成不合理影响。
只返回 JSON，例如 {"allowed":true,"risk":"low","reason":"只读操作"}。
如果命令会删除大量文件、修改 git 历史、推送远端、离开项目目录、安装不明脚本或难以判断，请返回 allowed=false。
"""

_DANGEROUS_COMMANDS = {
    "rm",
    "rmdir",
    "dd",
    "mkfs",
    "shutdown",
    "reboot",
    "kill",
    "pkill",
}
_DANGEROUS_GIT_SUBCOMMANDS = {"push", "reset", "clean", "checkout", "restore"}
_LOW_RISK_TOOLS = {
    "read_file",
    "list_dir",
    "ls_project",
    "grep_project",
    "read_project_file",
    "calculator",
    "current_time",
    "prune_memories",
}


def _extract_json(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        return {}
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _command_parts(arguments: dict[str, Any]) -> list[str]:
    command = arguments.get("command")
    if isinstance(command, list):
        return [str(part) for part in command]
    if isinstance(command, str):
        return command.split()
    return []


def _fallback_review(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if tool_name in _LOW_RISK_TOOLS:
        return {"allowed": True, "risk": "low", "reason": "只读或低风险工具。"}
    if tool_name == "run_command":
        parts = _command_parts(arguments)
        executable = Path(parts[0]).name if parts else ""
        if executable in _DANGEROUS_COMMANDS:
            return {
                "allowed": False,
                "risk": "high",
                "reason": f"命令 {executable} 可能破坏项目文件。",
            }
        if executable == "git" and len(parts) > 1 and parts[1] in _DANGEROUS_GIT_SUBCOMMANDS:
            return {
                "allowed": False,
                "risk": "high",
                "reason": f"git {parts[1]} 可能修改历史、工作区或远端。",
            }
        return {"allowed": True, "risk": "medium", "reason": "本地命令已通过基础风险检查。"}
    if tool_name == "delete_file":
        return {"allowed": True, "risk": "medium", "reason": "删除单个项目内文件，需要谨慎执行。"}
    if tool_name == "delete_memory":
        return {"allowed": True, "risk": "medium", "reason": "用户显式请求删除长期记忆。"}
    if tool_name == "write_file":
        return {"allowed": True, "risk": "medium", "reason": "写入项目内文件，需要谨慎执行。"}
    return {"allowed": False, "risk": "high", "reason": "未知工具，默认阻止。"}


async def review_tool_call(
    user_input: str,
    messages: list[dict[str, Any]],
    tool_call: dict[str, Any],
    tool_info: dict[str, Any],
    model_call: Callable[..., AsyncGenerator[dict[str, Any], None]] | None = None,
    model_name: str = DEFAULT_SUB_AGENT_MODEL,
) -> dict[str, Any]:
    tool_name = tool_call.get("name") or tool_call.get("function", {}).get("name") or ""
    arguments = tool_call.get("arguments") or tool_call.get("function", {}).get("arguments") or {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            arguments = {"raw": arguments}
    if not isinstance(arguments, dict):
        arguments = {}

    if model_call is None:
        return _fallback_review(tool_name, arguments)

    prompt = {
        "role": "user",
        "content": json.dumps(
            {
                "user_input": user_input,
                "recent_messages": messages[-8:],
                "tool": {
                    "name": tool_name,
                    "category": tool_info.get("category"),
                    "responsibility": tool_info.get("responsibility"),
                    "parallel_safe": tool_info.get("parallel_safe"),
                    "requires_review": tool_info.get("requires_review"),
                    "arguments": arguments,
                },
            },
            ensure_ascii=False,
        ),
    }
    content = ""
    try:
        async for event in model_call(
            messages=[prompt],
            system_prompt=PERMISSION_REVIEW_SYSTEM_PROMPT,
            tools=[],
            model_name=model_name,
        ):
            if event.get("type") == "assistant_delta":
                content += event.get("content", "")
    except Exception:
        return _fallback_review(tool_name, arguments)

    parsed = _extract_json(content)
    if not isinstance(parsed.get("allowed"), bool):
        return _fallback_review(tool_name, arguments)
    return {
        "allowed": parsed["allowed"],
        "risk": str(parsed.get("risk", "unknown")),
        "reason": str(parsed.get("reason", "")),
    }
