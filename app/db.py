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
    ) -> None:
        assert self._conn is not None
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
