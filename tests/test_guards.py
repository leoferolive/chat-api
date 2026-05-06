"""Tests for guards: turnstile, session JWT, cost gate."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.config import Settings
from app.db import Database
from app.guards import (
    CostGateExceeded,
    cost_gate_check,
    issue_session_token,
    verify_session_token,
    verify_turnstile,
)


def make_settings(**overrides) -> Settings:
    base = dict(
        env="test",
        wiki_dir=Path("./wiki-fixture"),
        db_path=Path("./_t.sqlite"),
        turnstile_secret="secret",
        turnstile_disabled=False,
        session_secret="jwt-secret",
        ip_hash_salt="salt",
    )
    base.update(overrides)
    return Settings(**base)


@pytest.mark.asyncio
async def test_turnstile_disabled_short_circuits() -> None:
    s = make_settings(turnstile_disabled=True)
    assert await verify_turnstile(None, s) is True


@pytest.mark.asyncio
async def test_turnstile_no_token_fails() -> None:
    s = make_settings()
    assert await verify_turnstile(None, s) is False


@pytest.mark.asyncio
async def test_turnstile_calls_cloudflare() -> None:
    s = make_settings()

    class FakeResp:
        def json(self) -> dict:
            return {"success": True}

    fake_client = AsyncMock()
    fake_client.post = AsyncMock(return_value=FakeResp())
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.guards.httpx.AsyncClient", return_value=fake_client):
        ok = await verify_turnstile("tok", s, remote_ip="1.2.3.4")
    assert ok is True
    fake_client.post.assert_awaited()


def test_session_token_round_trip() -> None:
    s = make_settings()
    tok = issue_session_token("sid-1", s)
    assert verify_session_token(tok, s) == "sid-1"


def test_session_token_rejects_tampered() -> None:
    s = make_settings()
    tok = issue_session_token("sid-1", s)
    bad = tok[:-2] + ("aa" if tok[-2:] != "aa" else "bb")
    assert verify_session_token(bad, s) is None


@pytest.mark.asyncio
async def test_cost_gate_passes_when_under_limit(tmp_path: Path) -> None:
    db = Database(tmp_path / "g.sqlite")
    await db.connect()
    try:
        count = await cost_gate_check(db, limit=10)
        assert count == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cost_gate_blocks_when_over_limit(tmp_path: Path) -> None:
    db = Database(tmp_path / "g2.sqlite")
    await db.connect()
    try:
        for _ in range(3):
            await db.increment_calls_today()
        with pytest.raises(CostGateExceeded):
            await cost_gate_check(db, limit=3)
    finally:
        await db.close()
