from __future__ import annotations

import asyncio
import shlex
from pathlib import Path
from typing import Any

from agent.main_agent.config import PROJECT_ROOT


def _resolve_inside_project(path: str, project_root: Path | None = None) -> Path:
    root = (project_root or PROJECT_ROOT).resolve()
    target = (root / path).resolve()
    if target != root and root not in target.parents:
        raise ValueError("Path is outside project.")
    return target


def _normalize_command(command: Any) -> list[str]:
    if isinstance(command, str):
        return shlex.split(command)
    if isinstance(command, list):
        return [str(part) for part in command]
    return []


async def run_command(
    arguments: dict[str, Any],
    project_root: Path | None = None,
) -> dict[str, Any]:
    command = _normalize_command(arguments.get("command"))
    if not command:
        return {"ok": False, "error": "Missing command."}

    try:
        cwd = _resolve_inside_project(str(arguments.get("cwd", ".")), project_root)
        timeout = min(max(float(arguments.get("timeout", 30)), 1), 120)
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return {"ok": False, "error": f"Command timed out after {timeout:g}s."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    content_parts = []
    if stdout:
        content_parts.append(stdout.rstrip())
    if stderr:
        content_parts.append(stderr.rstrip())
    return {
        "ok": process.returncode == 0,
        "exit_code": process.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "content": "\n".join(content_parts) or f"exit_code={process.returncode}",
    }


def run_command_spec() -> dict[str, Any]:
    return {
        "name": "run_command",
        "description": "在当前项目内本地运行命令，例如运行 Python 脚本或测试。不会通过 shell 执行，工作目录不能离开项目。",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "命令参数列表，例如 ['python', '-m', 'unittest', 'discover', '-s', 'tests']。",
                },
                "cwd": {
                    "type": "string",
                    "description": "相对项目根目录的运行目录，默认 .。",
                },
                "timeout": {
                    "type": "number",
                    "description": "超时时间秒数，范围 1-120，默认 30。",
                },
            },
            "required": ["command"],
        },
    }
