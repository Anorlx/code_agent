from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from agent.main_agent.config import MEMORY_DB_PATH, MEMORY_USER_ID
from agent.memory_system.openai_memory import MemoryModelError, complete_json, redact_payload, redact_sensitive


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _now() -> float:
    return time.time()


def _event_id(session_id: str, message: dict[str, Any]) -> str:
    stable = {
        "session_id": session_id,
        "role": message.get("role"),
        "content": message.get("content") or "",
        "tool_calls": message.get("tool_calls") or [],
        "tool_call_id": message.get("tool_call_id"),
        "name": message.get("name"),
        "created_at": message.get("created_at"),
        "uuid": message.get("uuid") or message.get("id"),
    }
    return "evt_" + hashlib.sha256(_json(stable).encode("utf-8")).hexdigest()[:24]


def _text(value: Any, limit: int = 12_000) -> str:
    if isinstance(value, str):
        return value[:limit]
    if value is None:
        return ""
    return _json(value)[:limit]


def _tokens(text: str) -> list[str]:
    return [token.lower() for token in re.findall(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]", text.lower())]


def _score(query: str, text: str, created_at: float) -> float:
    query_tokens = _tokens(query)
    if not query_tokens:
        return 0.1
    haystack = text.lower()
    exact = sum(1 for token in query_tokens if token in haystack)
    recency_days = max(0.0, (_now() - created_at) / 86400)
    recency = 1.0 / (1.0 + recency_days / 30.0)
    return exact / max(1, len(query_tokens)) + recency * 0.08


def _context_prefix(
    *,
    session_id: str,
    role: str,
    before: str,
    after: str,
) -> str:
    parts = [f"session={session_id}", f"role={role}"]
    if before:
        parts.append(f"previous context: {before[:220]}")
    if after:
        parts.append(f"following context: {after[:220]}")
    return "[" + " | ".join(parts) + "]"


class PersonalMemorySystem:
    """Durable personal memory: raw events, candidates, versioned cards and local RAG."""

    def __init__(self, db_path: Path | None = None, user_id: str = MEMORY_USER_ID) -> None:
        self.db_path = db_path or MEMORY_DB_PATH
        self.user_id = user_id
        self.setup()

    def setup(self) -> None:
        with _connect(self.db_path) as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_events (
                    event_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    occurred_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    processed_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_memory_events_pending
                    ON memory_events(user_id, session_id, processed_at, occurred_at);

                CREATE TABLE IF NOT EXISTS memory_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    memory_class TEXT NOT NULL,
                    category TEXT NOT NULL,
                    content TEXT NOT NULL,
                    person TEXT NOT NULL,
                    relationship TEXT NOT NULL,
                    importance TEXT NOT NULL,
                    stability TEXT NOT NULL,
                    sensitivity TEXT NOT NULL,
                    evidence_event_ids_json TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    decision TEXT NOT NULL DEFAULT '',
                    decision_reason TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    decided_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_memory_candidates_status
                    ON memory_candidates(user_id, status, created_at);

                CREATE TABLE IF NOT EXISTS memory_decisions (
                    decision_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    old_memory_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(candidate_id) REFERENCES memory_candidates(candidate_id)
                );

                CREATE TABLE IF NOT EXISTS memory_cards (
                    memory_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    memory_type TEXT NOT NULL,
                    category TEXT NOT NULL,
                    person TEXT NOT NULL,
                    relationship TEXT NOT NULL,
                    fact TEXT NOT NULL,
                    backstory TEXT NOT NULL,
                    valid_from TEXT NOT NULL,
                    valid_until TEXT,
                    status TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    sensitivity TEXT NOT NULL,
                    evidence_event_ids_json TEXT NOT NULL,
                    last_verified_at TEXT NOT NULL,
                    supersedes_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memory_cards_current
                    ON memory_cards(user_id, status, category, updated_at);

                CREATE TABLE IF NOT EXISTS event_memories (
                    memory_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    occurred_at REAL NOT NULL,
                    evidence_event_ids_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memory_chunks (
                    chunk_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    context_prefix TEXT NOT NULL,
                    content TEXT NOT NULL,
                    occurred_at REAL NOT NULL,
                    FOREIGN KEY(event_id) REFERENCES memory_events(event_id)
                );
                """
            )
            try:
                db.execute(
                    """CREATE VIRTUAL TABLE IF NOT EXISTS memory_chunks_fts USING fts5(
                        chunk_id UNINDEXED, context_prefix, content
                    )"""
                )
            except sqlite3.OperationalError:
                # The Python SQLite build may not include FTS5. The search method has a lexical fallback.
                pass

    def record_messages(self, session_id: str, messages: list[dict[str, Any]]) -> list[str]:
        if not session_id or not messages:
            return []
        self.setup()
        inserted: list[str] = []
        now = _now()
        with _connect(self.db_path) as db:
            for index, message in enumerate(messages):
                if not isinstance(message, dict):
                    continue
                safe_message = redact_payload(message)
                event_id = _event_id(session_id, safe_message)
                content = _text(safe_message.get("content"))
                occurred_at = float(message.get("created_at") or now)
                metadata = dict(safe_message)
                metadata.pop("content", None)
                cursor = db.execute(
                    """INSERT OR IGNORE INTO memory_events
                    (event_id,user_id,session_id,role,content,metadata_json,occurred_at,created_at)
                    VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        event_id,
                        self.user_id,
                        session_id,
                        str(safe_message.get("role") or "unknown"),
                        content,
                        _json(metadata),
                        occurred_at,
                        now,
                    ),
                )
                if cursor.rowcount == 0:
                    continue
                inserted.append(event_id)
                before = redact_sensitive(_text(messages[index - 1].get("content"))) if index else ""
                after = redact_sensitive(_text(messages[index + 1].get("content"))) if index + 1 < len(messages) else ""
                prefix = _context_prefix(
                    session_id=session_id,
                    role=str(message.get("role") or "unknown"),
                    before=before,
                    after=after,
                )
                chunk_id = "chunk_" + event_id[4:]
                db.execute(
                    """INSERT OR IGNORE INTO memory_chunks
                    (chunk_id,event_id,user_id,session_id,context_prefix,content,occurred_at)
                    VALUES (?,?,?,?,?,?,?)""",
                    (chunk_id, event_id, self.user_id, session_id, prefix, content, occurred_at),
                )
                try:
                    db.execute(
                        "INSERT OR IGNORE INTO memory_chunks_fts(chunk_id,context_prefix,content) VALUES (?,?,?)",
                        (chunk_id, prefix, content),
                    )
                except sqlite3.OperationalError:
                    pass
        return inserted

    def _pending_events(self, session_id: str) -> list[sqlite3.Row]:
        with _connect(self.db_path) as db:
            return db.execute(
                """SELECT * FROM memory_events
                WHERE user_id=? AND session_id=? AND processed_at IS NULL
                ORDER BY occurred_at, created_at""",
                (self.user_id, session_id),
            ).fetchall()

    def _candidate_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "candidates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "memory_class": {"type": "string", "enum": ["card", "event"]},
                            "category": {"type": "string"},
                            "content": {"type": "string"},
                            "person": {"type": "string"},
                            "relationship": {"type": "string"},
                            "importance": {"type": "string", "enum": ["low", "medium", "high"]},
                            "stability": {"type": "string", "enum": ["low", "medium", "high"]},
                            "sensitivity": {"type": "string", "enum": ["low", "medium", "high"]},
                            "evidence_event_ids": {"type": "array", "items": {"type": "string"}},
                            "timestamp": {"type": "string"},
                        },
                        "required": [
                            "memory_class", "category", "content", "person", "relationship",
                            "importance", "stability", "sensitivity", "evidence_event_ids", "timestamp",
                        ],
                    },
                }
            },
            "required": ["candidates"],
        }

    async def extract_candidates(self, session_id: str) -> dict[str, Any]:
        events = self._pending_events(session_id)
        if not events:
            return {"saved": 0, "pending": 0, "reason": "no_new_events"}
        event_payload = [
            {
                "event_id": row["event_id"],
                "role": row["role"],
                "content": row["content"],
                "occurred_at": row["occurred_at"],
                "metadata": _loads(row["metadata_json"], {}),
            }
            for row in events
        ]
        instructions = """你是个人长期记忆候选抽取器。只从证据中提取跨会话仍有价值的信息。
不要保存一次性任务、代码路径、可从项目重新推导的事实、临时状态或凭证。
用户没有明确表达的内容不要推测。每条候选必须引用提供的 evidence_event_ids。
稳定且高价值的信息用 card；普通事件和低稳定性细节用 event。
敏感事实标为 high，不要因为敏感而编造或扩展内容；后续决策层会处理确认。
没有合格候选时返回空数组。"""
        try:
            result = await complete_json(
                instructions=instructions,
                payload={"session_id": session_id, "events": event_payload},
                schema_name="memory_candidates",
                schema=self._candidate_schema(),
            )
        except MemoryModelError as exc:
            return {"saved": 0, "pending": len(events), "reason": str(exc)}

        allowed = {row["event_id"] for row in events}
        now = _now()
        saved = 0
        with _connect(self.db_path) as db:
            for raw in result.get("candidates", []):
                if not isinstance(raw, dict):
                    continue
                evidence = [item for item in raw.get("evidence_event_ids", []) if item in allowed]
                content = str(raw.get("content") or "").strip()
                if not content or not evidence:
                    continue
                candidate_id = "cand_" + uuid.uuid4().hex[:24]
                db.execute(
                    """INSERT INTO memory_candidates
                    (candidate_id,user_id,session_id,memory_class,category,content,person,relationship,
                     importance,stability,sensitivity,evidence_event_ids_json,timestamp,source,status,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        candidate_id, self.user_id, session_id,
                        str(raw.get("memory_class") or "event"),
                        str(raw.get("category") or "general"),
                        content,
                        str(raw.get("person") or "user"),
                        str(raw.get("relationship") or "self"),
                        str(raw.get("importance") or "medium"),
                        str(raw.get("stability") or "medium"),
                        str(raw.get("sensitivity") or "low"),
                        _json(evidence),
                        str(raw.get("timestamp") or ""),
                        "automatic",
                        "pending",
                        now,
                    ),
                )
                saved += 1
            placeholders = ",".join("?" for _ in events)
            db.execute(
                f"UPDATE memory_events SET processed_at=? WHERE event_id IN ({placeholders})",
                (now, *[row["event_id"] for row in events]),
            )
        return {"saved": saved, "pending": saved, "reason": "candidates_extracted"}

    def _decision_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "decisions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "candidate_id": {"type": "string"},
                            "decision": {"type": "string", "enum": ["ADD", "SUPERSEDE", "MERGE", "NOOP", "NEEDS_CONFIRMATION"]},
                            "old_memory_id": {"type": "string"},
                            "fact": {"type": "string"},
                            "backstory": {"type": "string"},
                            "valid_from": {"type": "string"},
                            "valid_until": {"type": "string"},
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            "reason": {"type": "string"},
                        },
                        "required": [
                            "candidate_id", "decision", "old_memory_id", "fact", "backstory",
                            "valid_from", "valid_until", "confidence", "reason",
                        ],
                    },
                }
            },
            "required": ["decisions"],
        }

    def _current_cards(self) -> list[dict[str, Any]]:
        with _connect(self.db_path) as db:
            rows = db.execute(
                """SELECT * FROM memory_cards WHERE user_id=? AND status='current'
                ORDER BY updated_at DESC LIMIT 100""",
                (self.user_id,),
            ).fetchall()
        return [
            {
                "memory_id": row["memory_id"],
                "category": row["category"],
                "person": row["person"],
                "relationship": row["relationship"],
                "fact": row["fact"],
                "confidence": row["confidence"],
                "evidence_event_ids": _loads(row["evidence_event_ids_json"], []),
            }
            for row in rows
        ]

    def _pending_candidates(self, session_id: str | None = None) -> list[sqlite3.Row]:
        with _connect(self.db_path) as db:
            if session_id:
                return db.execute(
                    "SELECT * FROM memory_candidates WHERE user_id=? AND session_id=? AND status='pending' ORDER BY created_at",
                    (self.user_id, session_id),
                ).fetchall()
            return db.execute(
                "SELECT * FROM memory_candidates WHERE user_id=? AND status='pending' ORDER BY created_at",
                (self.user_id,),
            ).fetchall()

    def list_candidates(self, limit: int = 20) -> list[dict[str, Any]]:
        with _connect(self.db_path) as db:
            rows = db.execute(
                """SELECT * FROM memory_candidates
                WHERE user_id=? AND status='needs_confirmation'
                ORDER BY created_at DESC LIMIT ?""",
                (self.user_id, max(1, min(100, limit))),
            ).fetchall()
        return [
            {
                "candidate_id": row["candidate_id"],
                "category": row["category"],
                "content": row["content"],
                "importance": row["importance"],
                "sensitivity": row["sensitivity"],
                "evidence_event_ids": _loads(row["evidence_event_ids_json"], []),
                "status": row["status"],
            }
            for row in rows
        ]

    def confirm_candidate(self, candidate_id: str, approved: bool) -> bool:
        with _connect(self.db_path) as db:
            row = db.execute(
                "SELECT * FROM memory_candidates WHERE user_id=? AND candidate_id=? AND status='needs_confirmation'",
                (self.user_id, candidate_id),
            ).fetchone()
        if row is None:
            return False
        decision = {
            "fact": row["content"],
            "backstory": "Explicitly confirmed by the user after candidate review.",
            "valid_from": row["timestamp"],
            "valid_until": "",
            "confidence": 0.95,
            "reason": "User explicitly confirmed this candidate.",
        }
        self._apply_decision(row, "ADD" if approved else "NOOP", "", decision)
        return True

    async def decide_candidates(self, session_id: str | None = None) -> dict[str, Any]:
        candidates = self._pending_candidates(session_id)
        if not candidates:
            return {"decided": 0, "pending": 0, "reason": "no_pending_candidates"}
        payload_candidates = [
            {
                "candidate_id": row["candidate_id"],
                "memory_class": row["memory_class"],
                "category": row["category"],
                "content": row["content"],
                "person": row["person"],
                "relationship": row["relationship"],
                "importance": row["importance"],
                "stability": row["stability"],
                "sensitivity": row["sensitivity"],
                "evidence_event_ids": _loads(row["evidence_event_ids_json"], []),
                "timestamp": row["timestamp"],
            }
            for row in candidates
        ]
        instructions = """你是个人长期记忆决策器。逐条审核候选和现有 current Cards。
ADD 表示没有相同事实；SUPERSEDE 表示新事实取代旧事实；MERGE 表示需要合并；NOOP 表示不值得保存或重复；NEEDS_CONFIRMATION 表示敏感或不确定，不能自动进入正式记忆。
不要凭空增加事实。fact 必须来自 candidate，backstory 只能引用候选证据。
高敏感度的自动候选必须 NEEDS_CONFIRMATION。old_memory_id 只能填写给定 Cards 中的 memory_id，否则填写空字符串。"""
        try:
            result = await complete_json(
                instructions=instructions,
                payload={"candidates": payload_candidates, "current_cards": self._current_cards()},
                schema_name="memory_decisions",
                schema=self._decision_schema(),
            )
        except MemoryModelError as exc:
            return {"decided": 0, "pending": len(candidates), "reason": str(exc)}

        allowed_candidates = {row["candidate_id"]: row for row in candidates}
        allowed_cards = {card["memory_id"] for card in self._current_cards()}
        decisions = {item.get("candidate_id"): item for item in result.get("decisions", []) if isinstance(item, dict)}
        decided = 0
        for candidate_id, row in allowed_candidates.items():
            decision = decisions.get(candidate_id)
            if not decision:
                continue
            action = str(decision.get("decision") or "NOOP")
            old_id = str(decision.get("old_memory_id") or "")
            if old_id not in allowed_cards:
                old_id = ""
            if row["source"] == "automatic" and row["sensitivity"] == "high":
                action = "NEEDS_CONFIRMATION"
            self._apply_decision(row, action, old_id, decision)
            decided += 1
        return {"decided": decided, "pending": len(candidates) - decided, "reason": "decisions_applied"}

    def _apply_decision(
        self,
        candidate: sqlite3.Row,
        action: str,
        old_memory_id: str,
        decision: dict[str, Any],
    ) -> None:
        now = _now()
        candidate_id = candidate["candidate_id"]
        evidence = _loads(candidate["evidence_event_ids_json"], [])
        fact = str(decision.get("fact") or candidate["content"]).strip()
        backstory = str(decision.get("backstory") or "Extracted from cited conversation events.").strip()
        valid_from = str(decision.get("valid_from") or candidate["timestamp"] or "").strip()
        valid_until = str(decision.get("valid_until") or "").strip() or None
        confidence = max(0.0, min(1.0, float(decision.get("confidence") or 0.7)))
        reason = str(decision.get("reason") or "").strip()
        with _connect(self.db_path) as db:
            if action in {"ADD", "SUPERSEDE", "MERGE"} and fact:
                if old_memory_id and action in {"SUPERSEDE", "MERGE"}:
                    db.execute(
                        "UPDATE memory_cards SET status='superseded', updated_at=? WHERE memory_id=? AND status='current'",
                        (now, old_memory_id),
                    )
                if candidate["memory_class"] == "card" or candidate["stability"] == "high":
                    memory_id = "mem_" + uuid.uuid4().hex[:24]
                    db.execute(
                        """INSERT INTO memory_cards
                        (memory_id,user_id,memory_type,category,person,relationship,fact,backstory,
                         valid_from,valid_until,status,confidence,sensitivity,evidence_event_ids_json,
                         last_verified_at,supersedes_id,created_at,updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            memory_id, self.user_id, "semantic", candidate["category"], candidate["person"],
                            candidate["relationship"], fact, backstory, valid_from, valid_until, "current",
                            confidence, candidate["sensitivity"], _json(evidence), valid_from or str(now),
                            old_memory_id or None, now, now,
                        ),
                    )
                else:
                    memory_id = "evt_mem_" + uuid.uuid4().hex[:24]
                    db.execute(
                        """INSERT INTO event_memories
                        (memory_id,user_id,session_id,summary,occurred_at,evidence_event_ids_json,created_at)
                        VALUES (?,?,?,?,?,?,?)""",
                        (memory_id, self.user_id, candidate["session_id"], fact, now, _json(evidence), now),
                    )
                final_status = "accepted"
            elif action == "NEEDS_CONFIRMATION":
                final_status = "needs_confirmation"
            else:
                final_status = "rejected" if action == "NOOP" else "pending"
            db.execute(
                """UPDATE memory_candidates SET status=?, decision=?, decision_reason=?, decided_at=?
                WHERE candidate_id=?""",
                (final_status, action, reason, now, candidate_id),
            )
            db.execute(
                """INSERT INTO memory_decisions
                (decision_id,candidate_id,decision,old_memory_id,reason,created_at)
                VALUES (?,?,?,?,?,?)""",
                ("dec_" + uuid.uuid4().hex[:24], candidate_id, action, old_memory_id, reason, now),
            )

    async def process_session(self, session_id: str) -> dict[str, Any]:
        extracted = await self.extract_candidates(session_id)
        decided = await self.decide_candidates(session_id)
        return {"extraction": extracted, "decision": decided}

    def save_explicit(
        self,
        arguments: dict[str, Any],
        *,
        session_id: str = "explicit",
        messages: list[dict[str, Any]] | None = None,
    ) -> str:
        if messages:
            self.record_messages(session_id, messages)
        evidence = self._recent_event_ids(session_id)
        now = _now()
        memory_id = "mem_" + uuid.uuid4().hex[:24]
        fact = str(arguments.get("content") or arguments.get("description") or "").strip()
        if not fact:
            raise ValueError("Explicit memory content is required.")
        with _connect(self.db_path) as db:
            db.execute(
                """INSERT INTO memory_cards
                (memory_id,user_id,memory_type,category,person,relationship,fact,backstory,
                 valid_from,valid_until,status,confidence,sensitivity,evidence_event_ids_json,
                 last_verified_at,supersedes_id,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    memory_id, self.user_id, "semantic", str(arguments.get("type") or "general"),
                    "user", "self", fact,
                    str(arguments.get("description") or "Explicitly saved by the user or main agent."),
                    str(now), None, "current", 1.0, "low", _json(evidence), str(now), None, now, now,
                ),
            )
        return memory_id

    def _recent_event_ids(self, session_id: str, limit: int = 12) -> list[str]:
        with _connect(self.db_path) as db:
            rows = db.execute(
                "SELECT event_id FROM memory_events WHERE user_id=? AND session_id=? ORDER BY occurred_at DESC LIMIT ?",
                (self.user_id, session_id, limit),
            ).fetchall()
        return [str(row["event_id"]) for row in reversed(rows)]

    def delete_memory(self, memory_id: str) -> bool:
        with _connect(self.db_path) as db:
            cursor = db.execute(
                "UPDATE memory_cards SET status='deleted', updated_at=? WHERE memory_id=? AND user_id=?",
                (_now(), memory_id, self.user_id),
            )
        return cursor.rowcount > 0

    def prune(self) -> dict[str, int]:
        now_text = time.strftime("%Y-%m-%d", time.localtime())
        with _connect(self.db_path) as db:
            cursor = db.execute(
                "UPDATE memory_cards SET status='expired', updated_at=? WHERE user_id=? AND status='current' AND valid_until IS NOT NULL AND valid_until != '' AND valid_until < ?",
                (_now(), self.user_id, now_text),
            )
        return {"expired_cards": cursor.rowcount}

    def search(self, query: str, *, session_id: str | None = None, limit: int = 8) -> dict[str, Any]:
        self.setup()
        query = str(query or "").strip()
        with _connect(self.db_path) as db:
            card_rows = db.execute(
                "SELECT * FROM memory_cards WHERE user_id=? AND status='current' ORDER BY updated_at DESC LIMIT 200",
                (self.user_id,),
            ).fetchall()
            event_rows = db.execute(
                "SELECT * FROM memory_chunks WHERE user_id=? ORDER BY occurred_at DESC LIMIT 1000",
                (self.user_id,),
            ).fetchall()
            fts_ids: set[str] = set()
            terms = _tokens(query)
            if terms:
                match_query = " OR ".join('"' + term.replace('"', ' ') + '"' for term in terms)
                try:
                    fts_rows = db.execute(
                        "SELECT chunk_id FROM memory_chunks_fts WHERE memory_chunks_fts MATCH ? LIMIT 200",
                        (match_query,),
                    ).fetchall()
                    fts_ids = {str(row["chunk_id"]) for row in fts_rows}
                except sqlite3.OperationalError:
                    pass
        cards = []
        for row in card_rows:
            text = " ".join(str(row[key]) for key in ("category", "person", "relationship", "fact", "backstory"))
            score = _score(query, text, float(row["updated_at"]))
            if not query or score > 0.08:
                cards.append(
                    {
                        "memory_id": row["memory_id"],
                        "category": row["category"],
                        "person": row["person"],
                        "relationship": row["relationship"],
                        "fact": row["fact"],
                        "backstory": row["backstory"],
                        "confidence": row["confidence"],
                        "evidence_event_ids": _loads(row["evidence_event_ids_json"], []),
                        "score": score,
                    }
                )
        chunks = []
        for row in event_rows:
            if session_id and row["session_id"] != session_id:
                continue
            text = f"{row['context_prefix']} {row['content']}"
            score = _score(query, text, float(row["occurred_at"]))
            if row["chunk_id"] in fts_ids:
                score += 0.2
            if not query or score > 0.08:
                chunks.append(
                    {
                        "chunk_id": row["chunk_id"],
                        "session_id": row["session_id"],
                        "context_prefix": row["context_prefix"],
                        "content": row["content"],
                        "occurred_at": row["occurred_at"],
                        "score": score,
                    }
                )
        cards.sort(key=lambda item: item["score"], reverse=True)
        chunks.sort(key=lambda item: item["score"], reverse=True)
        return {"cards": cards[:limit], "events": chunks[:limit]}

    def context(self, query: str, *, session_id: str | None = None) -> dict[str, Any]:
        result = self.search(query, session_id=session_id, limit=6)
        return {
            "cards": result["cards"],
            "events": result["events"],
            "source": "personal_memory.sqlite3; cards are versioned and event snippets retain evidence context",
        }
