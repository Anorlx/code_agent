from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.main_agent.config import SESSION_DB_PATH

UNFINISHED_STATUSES = {
    "running",
    "aborted",
    "failed",
    "needs_review",
    "unknown_outcome",
}


@dataclass(frozen=True)
class CheckpointRecord:
    id: str
    session_id: str
    run_id: str
    turn: int
    phase: str
    state: dict[str, Any]
    status: str
    created_at: float
    updated_at: float


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _record_from_row(row: sqlite3.Row) -> CheckpointRecord:
    try:
        state = json.loads(str(row["state_json"]))
    except json.JSONDecodeError:
        state = {}
    if not isinstance(state, dict):
        state = {}
    return CheckpointRecord(
        id=str(row["id"]),
        session_id=str(row["session_id"]),
        run_id=str(row["run_id"]),
        turn=int(row["turn"]),
        phase=str(row["phase"]),
        state=state,
        status=str(row["status"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


class CheckpointStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or SESSION_DB_PATH

    async def setup(self) -> None:
        await asyncio.to_thread(self._setup_sync)

    def _setup_sync(self) -> None:
        with _connect(self.db_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS run_checkpoints (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    turn INTEGER NOT NULL,
                    phase TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_run_checkpoints_session_status
                ON run_checkpoints (session_id, status, updated_at)
                """
            )

    async def save_checkpoint(
        self,
        *,
        session_id: str,
        run_id: str,
        turn: int,
        phase: str,
        state: dict[str, Any],
        status: str = "running",
    ) -> None:
        await asyncio.to_thread(
            self._save_checkpoint_sync,
            session_id,
            run_id,
            turn,
            phase,
            state,
            status,
        )

    def _save_checkpoint_sync(
        self,
        session_id: str,
        run_id: str,
        turn: int,
        phase: str,
        state: dict[str, Any],
        status: str,
    ) -> None:
        now = time.time()
        checkpoint_id = run_id
        state_json = json.dumps(_json_safe(state), ensure_ascii=False)
        with _connect(self.db_path) as connection:
            existing = connection.execute(
                "SELECT created_at FROM run_checkpoints WHERE id = ?",
                (checkpoint_id,),
            ).fetchone()
            created_at = float(existing["created_at"]) if existing else now
            connection.execute(
                """
                INSERT INTO run_checkpoints (
                    id, session_id, run_id, turn, phase, state_json, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    turn = excluded.turn,
                    phase = excluded.phase,
                    state_json = excluded.state_json,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (
                    checkpoint_id,
                    session_id,
                    run_id,
                    int(turn),
                    phase,
                    state_json,
                    status,
                    created_at,
                    now,
                ),
            )

    async def latest_unfinished(self, session_id: str) -> CheckpointRecord | None:
        return await asyncio.to_thread(self._latest_unfinished_sync, session_id)

    def _latest_unfinished_sync(self, session_id: str) -> CheckpointRecord | None:
        placeholders = ",".join("?" for _ in UNFINISHED_STATUSES)
        with _connect(self.db_path) as connection:
            row = connection.execute(
                f"""
                SELECT id, session_id, run_id, turn, phase, state_json, status, created_at, updated_at
                FROM run_checkpoints
                WHERE session_id = ?
                  AND status IN ({placeholders})
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (session_id, *sorted(UNFINISHED_STATUSES)),
            ).fetchone()
        return _record_from_row(row) if row else None

    async def mark_status(
        self,
        run_id: str,
        status: str,
        *,
        phase: str | None = None,
        state: dict[str, Any] | None = None,
    ) -> None:
        await asyncio.to_thread(self._mark_status_sync, run_id, status, phase, state)

    def _mark_status_sync(
        self,
        run_id: str,
        status: str,
        phase: str | None,
        state: dict[str, Any] | None,
    ) -> None:
        now = time.time()
        with _connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT phase, state_json FROM run_checkpoints WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return
            next_phase = phase or str(row["phase"])
            next_state_json = (
                json.dumps(_json_safe(state), ensure_ascii=False)
                if state is not None
                else str(row["state_json"])
            )
            connection.execute(
                """
                UPDATE run_checkpoints
                SET status = ?, phase = ?, state_json = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (status, next_phase, next_state_json, now, run_id),
            )

    async def mark_completed(self, run_id: str) -> None:
        await self.mark_status(run_id, "completed")

    async def mark_failed(self, run_id: str, reason: str) -> None:
        await self._mark_with_reason(run_id, "failed", reason)

    async def mark_aborted(self, run_id: str, reason: str) -> None:
        await self._mark_with_reason(run_id, "aborted", reason)

    async def mark_discarded(self, run_id: str) -> None:
        await self.mark_status(run_id, "discarded")

    async def _mark_with_reason(self, run_id: str, status: str, reason: str) -> None:
        record = await self.get(run_id)
        if record is None:
            return
        state = dict(record.state)
        state["checkpoint_error"] = reason
        await self.mark_status(run_id, status, state=state)

    async def get(self, run_id: str) -> CheckpointRecord | None:
        return await asyncio.to_thread(self._get_sync, run_id)

    def _get_sync(self, run_id: str) -> CheckpointRecord | None:
        with _connect(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT id, session_id, run_id, turn, phase, state_json, status, created_at, updated_at
                FROM run_checkpoints
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        return _record_from_row(row) if row else None

    async def cleanup_old(self, keep_days: int = 7) -> int:
        return await asyncio.to_thread(self._cleanup_old_sync, keep_days)

    def _cleanup_old_sync(self, keep_days: int) -> int:
        cutoff = time.time() - max(1, keep_days) * 86400
        with _connect(self.db_path) as connection:
            cursor = connection.execute(
                """
                DELETE FROM run_checkpoints
                WHERE updated_at < ?
                  AND status IN ('completed', 'discarded', 'failed', 'aborted')
                """,
                (cutoff,),
            )
            return int(cursor.rowcount or 0)
