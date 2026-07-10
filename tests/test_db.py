"""Tests for aiosqlite persistence."""

from __future__ import annotations

import asyncio
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


# ---------- atomic daily counter (TOCTOU fix) --------------------------


@pytest.mark.asyncio
async def test_decrement_calls_today_undoes_increment(tmp_path: Path) -> None:
    db = Database(tmp_path / "dec.sqlite")
    await db.connect()
    try:
        await db.increment_calls_today()
        second = await db.increment_calls_today()
        assert second == 2
        after_decrement = await db.decrement_calls_today()
        assert after_decrement == 1
        assert (await db.count_calls_today()) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_decrement_calls_today_floors_at_zero(tmp_path: Path) -> None:
    db = Database(tmp_path / "floor.sqlite")
    await db.connect()
    try:
        await db.increment_calls_today()
        await db.decrement_calls_today()
        floored = await db.decrement_calls_today()
        assert floored == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_increment_calls_today_concurrent_never_exceeds_limit(tmp_path: Path) -> None:
    """Regression test for the cost-gate TOCTOU: N concurrent callers racing
    against a small limit must never let more than `limit` through, and the
    counter must settle exactly at `limit` (no permanent overshoot left
    behind by rejected attempts)."""
    db = Database(tmp_path / "race.sqlite")
    await db.connect()
    try:
        limit = 5
        n_requests = 25

        async def attempt() -> bool:
            count = await db.increment_calls_today()
            if count > limit:
                await db.decrement_calls_today()
                return False
            return True

        results = await asyncio.gather(*(attempt() for _ in range(n_requests)))
        accepted = sum(results)
        assert accepted == limit
        assert (await db.count_calls_today()) == limit
    finally:
        await db.close()


# ---------- atomic per-session reservation (TOCTOU fix) -----------------


@pytest.mark.asyncio
async def test_try_reserve_session_message_inserts_and_respects_limit(tmp_path: Path) -> None:
    db = Database(tmp_path / "reserve.sqlite")
    await db.connect()
    try:
        sid = "sess-1"
        first = await db.try_reserve_session_message(sid, "hello", limit=2)
        assert first is not None
        second = await db.try_reserve_session_message(sid, "hello2", limit=2)
        assert second is not None
        third = await db.try_reserve_session_message(sid, "hello3", limit=2)
        assert third is None
        assert (await db.count_user_messages_in_session(sid)) == 2
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_try_reserve_session_message_counts_pre_seeded_rows(tmp_path: Path) -> None:
    """The atomic reserve must respect messages inserted through any path
    (e.g. save_turn directly), not just its own prior calls — it counts the
    real `messages` table, it isn't a separate synthetic counter."""
    db = Database(tmp_path / "preseed.sqlite")
    await db.connect()
    try:
        sid = "sess-1"
        for i in range(3):
            await db.save_turn(session_id=sid, role="user", content=f"msg{i}")
        blocked = await db.try_reserve_session_message(sid, "over the cap", limit=3)
        assert blocked is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_delete_message_removes_row(tmp_path: Path) -> None:
    db = Database(tmp_path / "del.sqlite")
    await db.connect()
    try:
        sid = "sess-1"
        msg_id = await db.try_reserve_session_message(sid, "hello", limit=5)
        assert msg_id is not None
        await db.delete_message(msg_id)
        assert (await db.count_user_messages_in_session(sid)) == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_try_reserve_session_message_concurrent_never_exceeds_limit(
    tmp_path: Path,
) -> None:
    """Regression test for the per-session TOCTOU: concurrent reservations
    for the same sessionId must never insert more than `limit` rows."""
    db = Database(tmp_path / "session_race.sqlite")
    await db.connect()
    try:
        limit = 5
        n_requests = 25
        sid = "sess-race"

        async def attempt(i: int) -> bool:
            msg_id = await db.try_reserve_session_message(sid, f"msg-{i}", limit)
            return msg_id is not None

        results = await asyncio.gather(*(attempt(i) for i in range(n_requests)))
        accepted = sum(results)
        assert accepted == limit
        assert (await db.count_user_messages_in_session(sid)) == limit
    finally:
        await db.close()


# ---------- schema ------------------------------------------------------


@pytest.mark.asyncio
async def test_messages_session_role_index_exists(tmp_path: Path) -> None:
    db = Database(tmp_path / "idx.sqlite")
    await db.connect()
    try:
        async with db._conn.execute(  # type: ignore[attr-defined]
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_messages_session_role'"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
    finally:
        await db.close()
