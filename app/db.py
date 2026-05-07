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
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # --- API ------------------------------------------------------------

    async def upsert_session(self, session_id: str, ip_hash: str, lang: str) -> None:
        assert self._conn is not None
        await self._conn.execute(
            """
            INSERT INTO sessions(id, ip_hash, lang, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET lang=excluded.lang
            """,
            (session_id, ip_hash, lang, _now_ts()),
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
    ) -> None:
        assert self._conn is not None
        await self._conn.execute(
            """
            INSERT INTO messages
                (session_id, role, content, model,
                 prompt_tokens, completion_tokens, latency_ms, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                role,
                content,
                model,
                prompt_tokens,
                completion_tokens,
                latency_ms,
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
