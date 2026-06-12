from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable

from agent.config import DEFAULT_TOOL_WORKSPACE, PROJECT_ROOT
from agent.tools.calculator import calculator, calculator_spec
from agent.tools.command import run_command, run_command_spec
from agent.tools.filesystem import (
    delete_file,
    delete_file_spec,
    list_dir,
    list_dir_spec,
    read_file,
    read_file_spec,
    write_file,
    write_file_spec,
)
from agent.tools.project import (
    grep_project,
    grep_project_spec,
    ls_project,
    ls_project_spec,
    read_project_file,
    read_project_file_spec,
)
from agent.tools.time_tool import current_time, current_time_spec

ToolFunc = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
TOOL_CATALOG_PATH = Path(__file__).with_name("README.md")


def _with_workspace(func: Callable[..., Awaitable[dict[str, Any]]], root: Path) -> ToolFunc:
    async def run(arguments: dict[str, Any]) -> dict[str, Any]:
        return await func(arguments, workspace_root=root)

    return run


def _with_project(func: Callable[..., Awaitable[dict[str, Any]]], root: Path) -> ToolFunc:
    async def run(arguments: dict[str, Any]) -> dict[str, Any]:
        return await func(arguments, project_root=root)

    return run


def get_tool_registry(workspace_root: Path | None = None) -> dict[str, dict[str, Any]]:
    root = (workspace_root or PROJECT_ROOT).resolve()
    root.mkdir(parents=True, exist_ok=True)
    project_root = PROJECT_ROOT.resolve()
    return {
        "read_file": {
            "spec": read_file_spec(),
            "run": _with_workspace(read_file, root),
            "category": "文件",
            "responsibility": "读取当前项目内的文本文件",
            "parallel_safe": True,
        },
        "write_file": {
            "spec": write_file_spec(),
            "run": _with_workspace(write_file, root),
            "category": "文件",
            "responsibility": "写入当前项目内的文本文件",
            "parallel_safe": False,
        },
        "delete_file": {
            "spec": delete_file_spec(),
            "run": _with_workspace(delete_file, root),
            "category": "文件",
            "responsibility": "删除当前项目内的文件",
            "parallel_safe": False,
        },
        "list_dir": {
            "spec": list_dir_spec(),
            "run": _with_workspace(list_dir, root),
            "category": "文件",
            "responsibility": "列出当前项目内目录",
            "parallel_safe": True,
        },
        "ls_project": {
            "spec": ls_project_spec(),
            "run": _with_project(ls_project, project_root),
            "category": "搜索",
            "responsibility": "列出当前项目目录结构",
            "parallel_safe": True,
        },
        "grep_project": {
            "spec": grep_project_spec(),
            "run": _with_project(grep_project, project_root),
            "category": "搜索",
            "responsibility": "在当前项目内搜索文本",
            "parallel_safe": True,
        },
        "read_project_file": {
            "spec": read_project_file_spec(),
            "run": _with_project(read_project_file, project_root),
            "category": "文件",
            "responsibility": "读取当前项目内的文本文件",
            "parallel_safe": True,
        },
        "calculator": {
            "spec": calculator_spec(),
            "run": calculator,
            "category": "执行",
            "responsibility": "安全计算四则运算表达式",
            "parallel_safe": True,
        },
        "current_time": {
            "spec": current_time_spec(),
            "run": current_time,
            "category": "执行",
            "responsibility": "获取指定时区当前时间",
            "parallel_safe": True,
        },
        "run_command": {
            "spec": run_command_spec(),
            "run": _with_project(run_command, project_root),
            "category": "执行",
            "responsibility": "在当前项目内本地运行命令或测试",
            "parallel_safe": False,
            "requires_review": True,
        },
    }


def tool_descriptions(tools: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {"name": name, "description": info["spec"]["description"]}
        for name, info in tools.items()
    ]


def tool_catalog_text() -> str:
    return TOOL_CATALOG_PATH.read_text(encoding="utf-8")


def dashscope_tool_specs(selected_names: list[str], tools: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    specs = []
    for name in selected_names:
        if name in tools:
            spec = tools[name]["spec"]
            specs.append({"type": "function", "function": spec})
    return specs
