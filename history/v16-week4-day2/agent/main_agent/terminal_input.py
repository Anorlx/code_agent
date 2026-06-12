from __future__ import annotations

import asyncio
import sys
from typing import Any, Callable


class TerminalInput:
    def __init__(self, session: Any, name: str = "prompt_toolkit") -> None:
        self.session = session
        self.name = name

    async def read(self) -> str:
        text = await self.session.prompt_async()
        return text.strip()


class FallbackInput:
    name = "input"

    def __init__(self, prompt: str = "\n你> ") -> None:
        self.prompt = prompt

    async def read(self) -> str:
        def read_line() -> str:
            print(self.prompt, end="", flush=True)
            line = sys.stdin.readline()
            if line == "":
                raise EOFError
            return line

        return (await asyncio.to_thread(read_line)).strip()


def _continuation_prompt(width: int, line_number: int, is_soft_wrap: bool) -> str:
    if is_soft_wrap:
        return " " * width
    return f"... {line_number:2d}> "


def create_terminal_input(prompt: str = "\n你> ") -> TerminalInput | FallbackInput:
    if not sys.stdin.isatty():
        return FallbackInput(prompt=prompt)
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory
    except ImportError:
        return FallbackInput(prompt=prompt)

    session = PromptSession(
        prompt,
        history=FileHistory(".agent_history"),
        multiline=False,
        wrap_lines=True,
        prompt_continuation=_continuation_prompt,
    )
    return TerminalInput(session=session)


def patch_stdout_context() -> Callable[[], Any]:
    try:
        from prompt_toolkit.patch_stdout import patch_stdout
    except ImportError:
        from contextlib import nullcontext

        return nullcontext
    return patch_stdout
