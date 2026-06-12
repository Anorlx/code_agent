from __future__ import annotations

import asyncio
import re
import shlex
from pathlib import Path
from typing import Any

from agent.main_agent.config import PROJECT_ROOT

# ── 危险命令黑名单 ──────────────────────────────────────────────
# 即使权限管线放行，这些模式也会在 run_command 执行前直接拒绝。
_DANGEROUS_PATTERNS: list[tuple[str, str]] = [
    # (正则, 说明)
    (r'\brm\s+-rf?\s+/',           "rm -rf 作用于根目录或绝对路径"),
    (r'\brm\s+-rf?\s+\*',          "rm -rf * 批量删除"),
    (r'\bsudo\b',                  "sudo 提权"),
    (r'\bchmod\s+777\b',           "chmod 777 全开放权限"),
    (r'\bchown\s+-R\b',            "chown -R 递归改所有者"),
    (r'\bchmod\s+-R\b',            "chmod -R 递归改权限"),
    (r'\bdd\s+if=',                "dd 磁盘写入"),
    (r'\bmkfs\.',                  "mkfs 格式化"),
    (r'\b>:?\s*/dev/',             "重定向覆盖设备文件"),
    (r'curl\b.*\|\s*(ba)?sh\b',   "curl 管道到 shell 执行"),
    (r'wget\b.*\|\s*(ba)?sh\b',   "wget 管道到 shell 执行"),
    (r'\breboot\b',                "重启命令"),
    (r'\bshutdown\b',              "关机命令"),
    (r'\bkill\s+-9\b',            "强制杀进程"),
    (r'\bkillall\b',              "批量杀进程"),
    (r'\bpkill\b',                "模式匹配杀进程"),
]


def _check_dangerous(command: list[str]) -> str | None:
    """检查命令是否命中危险模式，返回危险说明或 None。"""
    command_str = " ".join(command)
    for pattern, description in _DANGEROUS_PATTERNS:
        if re.search(pattern, command_str):
            return f"危险命令已拒绝: {description}"
    return None


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

    # 危险命令黑名单检查（最后一道防线）
    danger = _check_dangerous(command)
    if danger is not None:
        return {"ok": False, "error": danger}

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