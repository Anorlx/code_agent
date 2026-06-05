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
    return max(76, min(shutil.get_terminal_size((96, 24)).columns, 108))


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
    bold: str = "\033[1m"
    dim: str = "\033[2m"
    cyan: str = "\033[36m"
    green: str = "\033[32m"
    yellow: str = "\033[33m"
    red: str = "\033[31m"
    blue: str = "\033[34m"
    magenta: str = "\033[35m"
    reset: str = "\033[0m"


class TerminalUI:
    def __init__(self, color: bool | None = None) -> None:
        self.width = _terminal_width()
        self.color = bool(color if color is not None else False)
        self.theme = Theme()
        self._assistant_open = False

    def style(self, text: Any, color: str) -> str:
        if not self.color:
            return str(text)
        return f"{getattr(self.theme, color)}{text}{self.theme.reset}"

    def label(self, text: str, color: str = "cyan") -> str:
        value = _pad(_clip(text, 11), 11)
        return self.style(value, color)

    def divider(self, char: str = "─") -> str:
        return char * self.width

    def row(self, text: Any = "") -> str:
        body_width = self.width - 4
        body = _middle(text, body_width)
        return f"│ {_pad(body, body_width)} │"

    def center(self, text: Any = "") -> str:
        body_width = self.width - 4
        plain = _middle(text, body_width)
        padding = max(0, body_width - _visible_len(plain))
        left = padding // 2
        right = padding - left
        return f"│ {' ' * left}{plain}{' ' * right} │"

    def panel(self, lines: list[str], strong: bool = False, title: str | None = None) -> str:
        horizontal = "═" if strong else "─"
        top = "╭" + horizontal * (self.width - 2) + "╮"
        if title:
            label = f" {title} "
            available = self.width - 2
            top = "╭" + label + horizontal * max(0, available - _visible_len(label)) + "╮"
        bottom = "╰" + horizontal * (self.width - 2) + "╯"
        return "\n".join([top, *[self.row(line) for line in lines], bottom])

    def kv_panel(self, title: str, rows: list[tuple[str, Any]]) -> str:
        label_width = max(8, min(14, max((_visible_len(str(label)) for label, _ in rows), default=8)))
        lines = [
            f"{_pad(str(label), label_width)}  {value}"
            for label, value in rows
        ]
        return self.panel(lines, title=title)

    def welcome(self, session: SessionRecord, input_mode: str, tools_count: int, max_turns: int) -> str:
        inner = self.width - 4
        lines = [
            self.style("code_agent", "bold") + "  " + self.style("local terminal agent", "dim"),
            "",
            "workspace  " + _middle(PROJECT_ROOT, inner - 11),
            "session    " + _middle(f"{session.title} ({session.id})", inner - 11),
            f"runtime    input={input_mode}  tools={tools_count}  max_turns={max_turns}",
            "",
            "commands   /help  /session  /clear  /@  exit",
        ]
        return self.panel(lines, strong=True, title="ready")

    def session_picker(self, sessions: list[SessionRecord]) -> str:
        lines = [
            "Select a conversation",
            "",
            "[0] new conversation",
        ]
        for index, session in enumerate(sessions, start=1):
            summary = session.summary or "无摘要"
            title = _clip(session.title, 22)
            body = _clip(summary, max(24, self.width - 44))
            lines.append(f"[{index}] {_pad(title, 22)}  {body}  {session.message_count} msgs")
        return self.panel(lines, strong=True, title="sessions")

    def help_text(self) -> str:
        return self.panel(
            [
                "/help      显示帮助",
                "/session   显示当前会话 id 和本地数据库",
                "/clear     清屏并重新显示顶部信息",
                "/@         选择 MCP server，并强制本轮优先使用它",
                "exit       退出 agent",
            ],
            title="commands",
        )

    def state_line(self, turn: int, phase: str, detail: str = "") -> str:
        body = f"turn={turn:<2} phase={phase}"
        if detail:
            body += f"  {detail}"
        return f"{self.label('state', 'blue')} {body}"

    def event_line(self, label: str, text: str = "", color: str = "cyan") -> str:
        return f"{self.label(label, color)} {text}".rstrip()

    def assistant_start(self) -> str:
        if self._assistant_open:
            return ""
        self._assistant_open = True
        return self.label("assistant", "green") + " "

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
