from __future__ import annotations

import asyncio
import importlib.util
import logging
import shutil
import sys
import time
from pathlib import Path
from typing import Any, AsyncGenerator, Callable

from agent.main_agent.config import PROJECT_ROOT
from agent.sub_agent.verifier_review import review_verification

logger = logging.getLogger(__name__)

ModelCall = Callable[..., AsyncGenerator[dict[str, Any], None]]

MUTATING_TOOLS = {
    "write_file",
    "delete_file",
    "run_command",
    "save_memory",
    "delete_memory",
    "prune_memories",
}


def _result_tool_names(tool_results: list[dict[str, Any]]) -> set[str]:
    return {str(result.get("name") or "") for result in tool_results if result.get("name")}


def should_verify(tool_results: list[dict[str, Any]], tools: dict[str, dict[str, Any]]) -> bool:
    for result in tool_results:
        name = str(result.get("name") or "")
        info = tools.get(name, {})
        if name in MUTATING_TOOLS:
            return True
        if info.get("side_effectful"):
            return True
        raw_result = result.get("raw_result") or {}
        if isinstance(raw_result, dict) and raw_result.get("ok") is False:
            return True
    return False


def _trim(text: str, limit: int = 6000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


async def _run_command(command: list[str], *, timeout: float = 60.0) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=PROJECT_ROOT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return {
            "command": command,
            "ok": False,
            "exit_code": None,
            "stdout": "",
            "stderr": f"Command timed out after {timeout:g}s.",
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:
        return {
            "command": command,
            "ok": False,
            "exit_code": None,
            "stdout": "",
            "stderr": str(exc),
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    return {
        "command": command,
        "ok": process.returncode == 0,
        "exit_code": process.returncode,
        "stdout": _trim(stdout, 8000),
        "stderr": _trim(stderr, 8000),
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }


async def _git_diff_snapshot() -> dict[str, Any]:
    commands = {
        "name_only": ["git", "diff", "--name-only"],
        "stat": ["git", "diff", "--stat"],
        "diff": ["git", "diff"],
    }
    results = {key: await _run_command(command, timeout=20) for key, command in commands.items()}
    changed_files = [
        line.strip()
        for line in str(results["name_only"].get("stdout") or "").splitlines()
        if line.strip()
    ]
    return {
        "changed_files": changed_files,
        "stat": results["stat"].get("stdout") or results["stat"].get("stderr") or "",
        "diff": _trim(str(results["diff"].get("stdout") or ""), 12000),
        "commands": results,
    }


def _python_project_exists() -> bool:
    return (PROJECT_ROOT / "agent").exists() or (PROJECT_ROOT / "main.py").exists()


def _has_module(module: str) -> bool:
    return shutil.which(sys.executable) is not None and importlib.util.find_spec(module) is not None


def _verification_commands(diff_snapshot: dict[str, Any], tool_results: list[dict[str, Any]]) -> list[list[str]]:
    commands: list[list[str]] = []
    changed_files = [str(path) for path in diff_snapshot.get("changed_files", [])]
    touches_python = any(path.endswith(".py") for path in changed_files)
    names = _result_tool_names(tool_results)
    if _python_project_exists() and (touches_python or "write_file" in names or "delete_file" in names):
        commands.append([sys.executable, "-m", "compileall", "agent", "main.py"])
    if (PROJECT_ROOT / "tests").exists():
        commands.append([sys.executable, "-m", "unittest", "discover", "-s", "tests"])
    if (PROJECT_ROOT / "pyproject.toml").exists() and _has_module("ruff"):
        commands.append([sys.executable, "-m", "ruff", "check", "."])
    if (PROJECT_ROOT / "mypy.ini").exists() and _has_module("mypy"):
        commands.append([sys.executable, "-m", "mypy", "."])
    if (PROJECT_ROOT / "package.json").exists() and shutil.which("npm"):
        commands.append(["npm", "test", "--", "--runInBand"])
    return commands


def _command_summary(result: dict[str, Any]) -> str:
    command = " ".join(str(part) for part in result.get("command", []))
    status = "ok" if result.get("ok") else "failed"
    output = str(result.get("stderr") or result.get("stdout") or "").strip()
    return f"{command} -> {status}\n{_trim(output, 1500)}".strip()


def _fallback_review(verification_results: list[dict[str, Any]], diff_snapshot: dict[str, Any]) -> dict[str, Any]:
    failed = [result for result in verification_results if not result.get("ok")]
    if failed:
        return {
            "status": "failed",
            "summary": f"{len(failed)} verification command(s) failed.",
            "issues": [
                {
                    "severity": "high",
                    "location": "verification command",
                    "problem": _command_summary(result),
                    "suggestion": "把失败输出作为下一轮上下文，先修复命令报告的问题。",
                }
                for result in failed[:5]
            ],
        }
    if not diff_snapshot.get("changed_files"):
        return {
            "status": "warning",
            "summary": "No git diff detected after tool execution.",
            "issues": [],
        }
    return {
        "status": "passed",
        "summary": "Verification commands passed and git diff was captured.",
        "issues": [],
    }


async def run_verifier(
    *,
    user_input: str,
    messages: list[dict[str, Any]],
    tool_results: list[dict[str, Any]],
    tools: dict[str, dict[str, Any]],
    model_call: ModelCall | None,
    model_name: str,
) -> dict[str, Any]:
    if not should_verify(tool_results, tools):
        return {
            "status": "skipped",
            "summary": "No mutating or failed tool result detected.",
            "issues": [],
            "diff": {},
            "commands": [],
            "review": {},
        }

    diff_snapshot = await _git_diff_snapshot()
    commands = _verification_commands(diff_snapshot, tool_results)
    verification_results = []
    for command in commands:
        verification_results.append(await _run_command(command, timeout=90))

    review = await review_verification(
        user_input=user_input,
        tool_results=tool_results,
        diff_snapshot=diff_snapshot,
        verification_results=verification_results,
        model_call=model_call,
        model_name=model_name,
    )
    fallback = _fallback_review(verification_results, diff_snapshot)
    if not review:
        review = fallback
    elif any(not result.get("ok") for result in verification_results) and review.get("status") == "passed":
        review = fallback

    return {
        "status": review.get("status", fallback["status"]),
        "summary": review.get("summary", fallback["summary"]),
        "issues": review.get("issues", fallback["issues"]),
        "diff": {
            "changed_files": diff_snapshot.get("changed_files", []),
            "stat": diff_snapshot.get("stat", ""),
            "diff": diff_snapshot.get("diff", ""),
        },
        "commands": verification_results,
        "review": review,
        "message_count": len(messages),
    }


def verifier_tool_message(report: dict[str, Any]) -> dict[str, Any]:
    status = str(report.get("status") or "unknown")
    summary = str(report.get("summary") or "")
    commands = report.get("commands") or []
    failed_commands = [result for result in commands if not result.get("ok")]
    issues = report.get("issues") or []
    content_parts = [
        f"Verification status: {status}",
        f"Summary: {summary}",
    ]
    if report.get("diff", {}).get("stat"):
        content_parts.append("Git diff stat:\n" + str(report["diff"]["stat"]).strip())
    if failed_commands:
        content_parts.append(
            "Failed commands:\n"
            + "\n\n".join(_command_summary(result) for result in failed_commands[:5])
        )
    elif commands:
        content_parts.append(
            "Commands passed:\n"
            + "\n".join(" ".join(str(part) for part in result.get("command", [])) for result in commands)
        )
    if issues:
        content_parts.append(
            "Review issues:\n"
            + "\n".join(
                f"- {item.get('severity', 'unknown')} {item.get('location', 'unknown')}: "
                f"{item.get('problem', '')} Suggestion: {item.get('suggestion', '')}"
                for item in issues[:8]
                if isinstance(item, dict)
            )
        )
    return {
        "role": "user",
        "name": "verifier",
        "arguments": {},
        "summary": f"{status}: {summary}",
        "content": "[verifier_result]\n" + "\n\n".join(part for part in content_parts if part.strip()),
        "raw_result": report,
        "created_at": time.time(),
    }
