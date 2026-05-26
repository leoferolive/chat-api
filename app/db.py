"""Async SQLite persistence (sessions + messages + daily call counter)."""

from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from .metrics import DAILY_CALLS

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    ip_hash TEXT,
    lang TEXT,
    user_name TEXT,
    created_at INTEGER
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT REFERENCES sessions(id),
    role TEXT CHECK(role IN ('user','assistant')),
    content TEXT,
    model TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    latency_ms INTEGER,
    cost_usd REAL DEFAULT 0,
    created_at INTEGER
);

CREATE TABLE IF NOT EXISTS daily_calls (
    day TEXT PRIMARY KEY,
    count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS judge_scores (
    message_id INTEGER NOT NULL REFERENCES messages(id),
    criterion TEXT NOT NULL,
    score REAL NOT NULL,
    reason TEXT,
    judge_model TEXT,
    created_at INTEGER,
    PRIMARY KEY (message_id, criterion)
);

CREATE INDEX IF NOT EXISTS idx_judge_scores_created_at
    ON judge_scores(created_at);
"""


def _now_ts() -> int:
    return int(time.time())


def _today_key() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def hash_ip(ip: str | None, salt: str) -> str:
    if not ip:
        return ""
    return hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()


class Database:
    """Thin async wrapper over aiosqlite.

    A single connection is kept open for the app lifecycle. SQLite handles
    serialised access fine for our load profile.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._conn: aiosqlite.Connection | None = None

    # --- lifecycle ------------------------------------------------------

    async def connect(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        await self._conn.executescript(_SCHEMA)
        await self._migrate_add_user_name()
        await self._migrate_add_cost_usd()
        await self._conn.commit()

    async def _migrate_add_user_name(self) -> None:
        # In-place migration: bases criadas antes desta feature não têm a
        # coluna. CREATE TABLE IF NOT EXISTS é no-op se a tabela existe, então
        # precisamos inspecionar PRAGMA e adicionar a coluna manualmente.
        assert self._conn is not None
        async with self._conn.execute("PRAGMA table_info(sessions)") as cur:
            cols = {row[1] async for row in cur}
        if "user_name" not in cols:
            await self._conn.execute("ALTER TABLE sessions ADD COLUMN user_name TEXT")

    async def _migrate_add_cost_usd(self) -> None:
        assert self._conn is not None
        async with self._conn.execute("PRAGMA table_info(messages)") as cur:
            cols = {row[1] async for row in cur}
        if "cost_usd" not in cols:
            await self._conn.execute(
                "ALTER TABLE messages ADD COLUMN cost_usd REAL DEFAULT 0"
            )

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # --- API ------------------------------------------------------------

    async def upsert_session(
        self,
        session_id: str,
        ip_hash: str,
        lang: str,
        user_name: str | None = None,
    ) -> bool:
        """Insert or update a session. Returns True iff a NEW session was created.

        The boolean is the only reliable signal we have to drive a
        ``sessions_created_total`` counter — SQLite's ``ON CONFLICT DO UPDATE``
        succeeds in both branches, so we probe with ``SELECT 1`` first.
        Best-effort: two concurrent first-message requests for the same
        sessionId could both see "not yet there" and double-count, but
        that's vanishingly rare and acceptable for analytics.
        """
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
        ) as cur:
            existed = await cur.fetchone() is not None
        await self._conn.execute(
            """
            INSERT INTO sessions(id, ip_hash, lang, user_name, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                lang=excluded.lang,
                user_name=COALESCE(excluded.user_name, sessions.user_name)
            """,
            (session_id, ip_hash, lang, user_name, _now_ts()),
        )
        await self._conn.commit()
        return not existed

    async def save_turn(
        self,
        *,
        session_id: str,
        role: str,
        content: str,
        model: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        assert self._conn is not None
        await self._conn.execute(
            """
            INSERT INTO messages
                (session_id, role, content, model,
                 prompt_tokens, completion_tokens, latency_ms, cost_usd, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                role,
                content,
                model,
                prompt_tokens,
                completion_tokens,
                latency_ms,
                cost_usd,
                _now_ts(),
            ),
        )
        await self._conn.commit()

    async def count_calls_today(self) -> int:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT count FROM daily_calls WHERE day = ?", (_today_key(),)
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def increment_calls_today(self) -> int:
        """Atomically bump today's counter and return the new value."""
        assert self._conn is not None
        day = _today_key()
        await self._conn.execute(
            """
            INSERT INTO daily_calls(day, count) VALUES(?, 1)
            ON CONFLICT(day) DO UPDATE SET count = count + 1
            """,
            (day,),
        )
        await self._conn.commit()
        async with self._conn.execute(
            "SELECT count FROM daily_calls WHERE day = ?", (day,)
        ) as cur:
            row = await cur.fetchone()
        count = int(row[0]) if row else 0
        DAILY_CALLS.set(count)
        return count

    async def count_user_messages_in_session(self, session_id: str) -> int:
        """Count how many user-role messages we've stored for a sessionId."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ? AND role = 'user'",
            (session_id,),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    # --- judge ----------------------------------------------------------

    async def fetch_unscored_assistant_turns(
        self, criteria: list[str], limit: int = 50
    ) -> list[dict]:
        """Return assistant turns that lack at least one of the given scores.

        For each row we also pull the immediately preceding user turn in the
        same session (the question being answered) and the model that
        produced the answer — both are needed by the judge prompt.
        """
        assert self._conn is not None
        if not criteria:
            return []
        placeholders = ",".join("?" for _ in criteria)
        async with self._conn.execute(
            f"""
            SELECT
                a.id AS assistant_id,
                a.session_id,
                a.content AS answer,
                a.model,
                a.created_at,
                (
                    SELECT u.content
                    FROM messages u
                    WHERE u.session_id = a.session_id
                      AND u.role = 'user'
                      AND u.created_at <= a.created_at
                    ORDER BY u.created_at DESC, u.id DESC
                    LIMIT 1
                ) AS question
            FROM messages a
            WHERE a.role = 'assistant'
              AND a.content IS NOT NULL
              AND length(a.content) > 0
              AND (
                  SELECT COUNT(DISTINCT js.criterion)
                  FROM judge_scores js
                  WHERE js.message_id = a.id
                    AND js.criterion IN ({placeholders})
              ) < ?
            ORDER BY a.id DESC
            LIMIT ?
            """,
            (*criteria, len(criteria), limit),
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "assistant_id": row[0],
                "session_id": row[1],
                "answer": row[2],
                "model": row[3],
                "created_at": row[4],
                "question": row[5],
            }
            for row in rows
        ]

    async def save_judge_score(
        self,
        *,
        message_id: int,
        criterion: str,
        score: float,
        reason: str,
        judge_model: str,
    ) -> None:
        assert self._conn is not None
        await self._conn.execute(
            """
            INSERT INTO judge_scores
                (message_id, criterion, score, reason, judge_model, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id, criterion) DO UPDATE SET
                score = excluded.score,
                reason = excluded.reason,
                judge_model = excluded.judge_model,
                created_at = excluded.created_at
            """,
            (message_id, criterion, score, reason, judge_model, _now_ts()),
        )
        await self._conn.commit()

    async def judge_score_aggregates(self, since_ts: int) -> list[dict]:
        """Aggregate judge scores recorded after ``since_ts`` for /metrics-judge."""
        assert self._conn is not None
        async with self._conn.execute(
            """
            SELECT
                js.criterion,
                COALESCE(js.judge_model, 'unknown') AS judge_model,
                COALESCE(m.model, 'unknown')       AS answer_model,
                AVG(js.score)                      AS avg_score,
                COUNT(*)                           AS n
            FROM judge_scores js
            LEFT JOIN messages m ON m.id = js.message_id
            WHERE js.created_at >= ?
            GROUP BY js.criterion, judge_model, answer_model
            """,
            (since_ts,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "criterion": row[0],
                "judge_model": row[1],
                "answer_model": row[2],
                "avg_score": float(row[3] or 0.0),
                "count": int(row[4]),
            }
            for row in rows
        ]

    async def judge_verdict_counts(self, since_ts: int) -> list[dict]:
        """Bucketed counts (pass|warn|fail) for /metrics-judge."""
        assert self._conn is not None
        async with self._conn.execute(
            """
            SELECT
                js.criterion,
                CASE
                    WHEN js.score >= 4 THEN 'pass'
                    WHEN js.score >= 3 THEN 'warn'
                    ELSE 'fail'
                END AS verdict,
                COUNT(*) AS n
            FROM judge_scores js
            WHERE js.created_at >= ?
            GROUP BY js.criterion, verdict
            """,
            (since_ts,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            {"criterion": row[0], "verdict": row[1], "count": int(row[2])}
            for row in rows
        ]

    # --- traffic counters ---------------------------------------------

    async def distinct_sessions_since(self, since_ts: int) -> int:
        """Distinct sessionIds that received at least one user message after ``since_ts``."""
        assert self._conn is not None
        async with self._conn.execute(
            """
            SELECT COUNT(DISTINCT m.session_id)
            FROM messages m
            WHERE m.role = 'user' AND m.created_at >= ?
            """,
            (since_ts,),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def distinct_ips_since(self, since_ts: int) -> int:
        """Distinct IP hashes that opened a session after ``since_ts``."""
        assert self._conn is not None
        async with self._conn.execute(
            """
            SELECT COUNT(DISTINCT s.ip_hash)
            FROM sessions s
            WHERE s.ip_hash IS NOT NULL
              AND s.ip_hash != ''
              AND s.created_at >= ?
            """,
            (since_ts,),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def sessions_by_lang_since(self, since_ts: int) -> list[dict]:
        """Per-language session counts in the window for a Grafana bar chart."""
        assert self._conn is not None
        async with self._conn.execute(
            """
            SELECT COALESCE(lang, 'unknown') AS lang, COUNT(*) AS n
            FROM sessions
            WHERE created_at >= ?
            GROUP BY lang
            """,
            (since_ts,),
        ) as cur:
            rows = await cur.fetchall()
        return [{"lang": row[0], "count": int(row[1])} for row in rows]

    # --- legacy / per-IP -----------------------------------------------

    async def count_calls_today_by_ip(self, ip_hash: str) -> int:
        """Count assistant turns served to this IP today (UTC)."""
        assert self._conn is not None
        if not ip_hash:
            return 0
        # Treat assistant rows as the "served call" marker. Day boundary
        # uses the same UTC truncation as the global counter.
        day_start = int(
            datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        )
        async with self._conn.execute(
            """
            SELECT COUNT(*) FROM messages m
            JOIN sessions s ON s.id = m.session_id
            WHERE s.ip_hash = ? AND m.role = 'assistant' AND m.created_at >= ?
            """,
            (ip_hash, day_start),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0
