"""Tests for aiosqlite persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.db import Database, hash_ip


@pytest.mark.asyncio
async def test_save_turn_and_count(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    try:
        await db.upsert_session("sess-1", "ip-hash", "pt")
        await db.save_turn(
            session_id="sess-1",
            role="user",
            content="hello",
        )
        await db.save_turn(
            session_id="sess-1",
            role="assistant",
            content="hi there",
            model="mock/primary",
            prompt_tokens=10,
            completion_tokens=5,
            latency_ms=42,
        )
        c = await db.count_calls_today()
        assert c == 0
        new = await db.increment_calls_today()
        assert new == 1
        new = await db.increment_calls_today()
        assert new == 2
    finally:
        await db.close()


def test_hash_ip_stable() -> None:
    h1 = hash_ip("1.2.3.4", "salt")
    h2 = hash_ip("1.2.3.4", "salt")
    h3 = hash_ip("1.2.3.4", "other")
    assert h1 == h2
    assert h1 != h3
    assert hash_ip(None, "salt") == ""


@pytest.mark.asyncio
async def test_upsert_session_updates_lang(tmp_path: Path) -> None:
    db = Database(tmp_path / "u.sqlite")
    await db.connect()
    try:
        await db.upsert_session("sess-1", "h", "pt")
        await db.upsert_session("sess-1", "h", "en")
    finally:
        await db.close()
