from __future__ import annotations

import os
import re
import shutil
import unicodedata
from dataclasses import dataclass
from typing import Any

from agent.main_agent.config import PROJECT_ROOT
from agent.main_agent.session_store import SessionRecord

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _terminal_width() -> int:
    return max(72, min(shutil.get_terminal_size((88, 24)).columns, 96))


def _visible_len(text: str) -> int:
    plain = ANSI_RE.sub("", text)
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in plain)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _visible_len(text))


def _take_width(text: str, width: int) -> str:
    output = []
    used = 0
    for char in text:
        char_width = 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        if used + char_width > width:
            break
        output.append(char)
        used += char_width
    return "".join(output)


def _clip(text: Any, width: int) -> str:
    value = str(text)
    if _visible_len(value) <= width:
        return value
    if width <= 1:
        return ""
    plain = ANSI_RE.sub("", value)
    return _take_width(plain, max(0, width - 1)) + "."


def _middle(text: Any, width: int) -> str:
    value = str(text)
    if _visible_len(value) <= width:
        return value
    if width <= 8:
        return _clip(value, width)
    plain = ANSI_RE.sub("", value)
    keep = width - 3
    left = keep // 2
    right = keep - left
    left_text = _take_width(plain, left)
    right_chars = []
    used = 0
    for char in reversed(plain):
        char_width = 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        if used + char_width > right:
            break
        right_chars.append(char)
        used += char_width
    return left_text + "..." + "".join(reversed(right_chars))


@dataclass(frozen=True)
class Theme:
    dim: str = "\033[2m"
    cyan: str = "\033[36m"
    green: str = "\033[32m"
    yellow: str = "\033[33m"
    red: str = "\033[31m"
    blue: str = "\033[34m"
    reset: str = "\033[0m"


class TerminalUI:
    def __init__(self, color: bool | None = None) -> None:
        self.width = _terminal_width()
        self.color = bool(color if color is not None else os.getenv("NO_COLOR") is None)
        self.theme = Theme()
        self._assistant_open = False

    def style(self, text: Any, color: str) -> str:
        if not self.color:
            return str(text)
        return f"{getattr(self.theme, color)}{text}{self.theme.reset}"

    def divider(self, char: str = "-") -> str:
        return "+" + char * (self.width - 2) + "+"

    def row(self, text: Any = "") -> str:
        body_width = self.width - 4
        body = _middle(text, body_width)
        return f"| {_pad(body, body_width)} |"

    def center(self, text: Any = "") -> str:
        body_width = self.width - 4
        plain = _middle(text, body_width)
        padding = max(0, body_width - _visible_len(plain))
        left = padding // 2
        right = padding - left
        return f"| {' ' * left}{plain}{' ' * right} |"

    def panel(self, lines: list[str], strong: bool = False) -> str:
        border = "=" if strong else "-"
        return "\n".join([self.divider(border), *[self.row(line) for line in lines], self.divider(border)])

    def welcome(self, session: SessionRecord, input_mode: str, tools_count: int, max_turns: int) -> str:
        inner = self.width - 4
        gap = 3
        left_width = (inner - gap) // 2
        right_width = inner - gap - left_width

        def cell(label: str, value: Any, width: int) -> str:
            return _pad(_middle(f"{label:<9} {value}", width), width)

        def pair(left_label: str, left_value: Any, right_label: str, right_value: Any) -> str:
            return self.row(
                cell(left_label, left_value, left_width)
                + " " * gap
                + cell(right_label, right_value, right_width)
            )

        lines = [
            self.divider("="),
            self.center("code_agent"),
            self.center("local terminal agent"),
            self.center("quiet shell, streaming work"),
            self.divider("-"),
            self.row(""),
            self.row("WORKSPACE  " + _middle(PROJECT_ROOT, inner - 11)),
            pair("SESSION", f"{session.title} ({session.id})", "INPUT", input_mode),
            pair("TOOLS", tools_count, "MAXTURN", max_turns),
            self.row(""),
            self.row("Commands: /help  /session  /clear  exit"),
            self.divider("="),
        ]
        return "\n".join(lines)

    def session_picker(self, sessions: list[SessionRecord]) -> str:
        lines = [
            "Choose a conversation",
            "",
            "[0] 新会话",
        ]
        for index, session in enumerate(sessions, start=1):
            summary = session.summary or "无摘要"
            lines.append(
                f"[{index}] {_clip(session.title, 18)} -- {_clip(summary, 42)} "
                f"({session.message_count} msgs)"
            )
        return self.panel(lines, strong=True)

    def help_text(self) -> str:
        return self.panel(
            [
                "Commands",
                "/help     显示这个帮助",
                "/session  显示当前会话 id 和本地数据库",
                "/clear    清屏并重新显示顶部信息",
                "exit      退出 agent",
            ]
        )

    def state_line(self, turn: int, phase: str, detail: str = "") -> str:
        prefix = self.style("state", "blue")
        body = f"{prefix} turn={turn} {phase}"
        if detail:
            body += f"  {detail}"
        return body

    def event_line(self, label: str, text: str = "", color: str = "cyan") -> str:
        badge = self.style(label, color)
        return f"{badge} {text}".rstrip()

    def assistant_start(self) -> str:
        if self._assistant_open:
            return ""
        self._assistant_open = True
        return self.style("assistant", "green") + " "

    def assistant_end(self) -> str:
        if not self._assistant_open:
            return ""
        self._assistant_open = False
        return ""

    def ensure_line_break(self) -> str:
        if self._assistant_open:
            self._assistant_open = False
            return "\n"
        return ""
