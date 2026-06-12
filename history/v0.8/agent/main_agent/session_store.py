from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.main_agent.config import SESSION_DB_PATH


@dataclass(frozen=True)
class SessionRecord:
    id: str
    title: str
    summary: str
    created_at: float
    updated_at: float
    message_count: int


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


class SessionStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or SESSION_DB_PATH

    async def setup(self) -> None:
        await asyncio.to_thread(self._setup_sync)

    def _setup_sync(self) -> None:
        with _connect(self.db_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    message_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS session_messages (
                    session_id TEXT NOT NULL,
                    message_index INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (session_id, message_index),
                    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
                )
                """
            )

    async def list_sessions(self, limit: int = 12) -> list[SessionRecord]:
        return await asyncio.to_thread(self._list_sessions_sync, limit)

    def _list_sessions_sync(self, limit: int) -> list[SessionRecord]:
        with _connect(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT id, title, summary, created_at, updated_at, message_count
                FROM sessions
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            SessionRecord(
                id=str(row["id"]),
                title=str(row["title"]),
                summary=str(row["summary"]),
                created_at=float(row["created_at"]),
                updated_at=float(row["updated_at"]),
                message_count=int(row["message_count"]),
            )
            for row in rows
        ]

    async def create_session(self) -> SessionRecord:
        return await asyncio.to_thread(self._create_session_sync)

    def _create_session_sync(self) -> SessionRecord:
        now = time.time()
        session_id = uuid.uuid4().hex[:12]
        record = SessionRecord(
            id=session_id,
            title="新会话",
            summary="还没有开始对话。",
            created_at=now,
            updated_at=now,
            message_count=0,
        )
        with _connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO sessions (id, title, summary, created_at, updated_at, message_count)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.title,
                    record.summary,
                    record.created_at,
                    record.updated_at,
                    record.message_count,
                ),
            )
        return record

    async def load_messages(self, session_id: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._load_messages_sync, session_id)

    def _load_messages_sync(self, session_id: str) -> list[dict[str, Any]]:
        with _connect(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT payload
                FROM session_messages
                WHERE session_id = ?
                ORDER BY message_index ASC
                """,
                (session_id,),
            ).fetchall()
        messages: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(str(row["payload"]))
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                messages.append(payload)
        return messages

    async def save_messages(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        await asyncio.to_thread(self._save_messages_sync, session_id, messages)

    def _save_messages_sync(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        now = time.time()
        rows = [
            (
                session_id,
                index,
                json.dumps(message, ensure_ascii=False),
                float(message.get("created_at") or now),
            )
            for index, message in enumerate(messages)
        ]
        with _connect(self.db_path) as connection:
            connection.execute("DELETE FROM session_messages WHERE session_id = ?", (session_id,))
            connection.executemany(
                """
                INSERT INTO session_messages (session_id, message_index, payload, created_at)
                VALUES (?, ?, ?, ?)
                """,
                rows,
            )
            connection.execute(
                """
                UPDATE sessions
                SET updated_at = ?, message_count = ?
                WHERE id = ?
                """,
                (now, len(messages), session_id),
            )

    async def update_summary(self, session_id: str, title: str, summary: str) -> None:
        await asyncio.to_thread(self._update_summary_sync, session_id, title, summary)

    def _update_summary_sync(self, session_id: str, title: str, summary: str) -> None:
        with _connect(self.db_path) as connection:
            connection.execute(
                """
                UPDATE sessions
                SET title = ?, summary = ?, updated_at = ?
                WHERE id = ?
                """,
                (title.strip()[:80] or "未命名会话", summary.strip()[:240], time.time(), session_id),
            )
