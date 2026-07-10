"""Tests for guards: turnstile, session JWT, cost gate."""

from __future__ import annotations

import asyncio
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import Response

from app.config import Settings
from app.db import Database
from app.guards import (
    SESSION_COOKIE,
    CostGateExceeded,
    cost_gate_check,
    issue_session_token,
    set_session_cookie,
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
    header, payload, signature = tok.split(".")
    # Flip a char in the middle of the payload so the HMAC signature no
    # longer matches. Flipping the signature tail is flaky: 2 base64url
    # chars encode only ~10-12 bits, so a fixed literal replacement has a
    # real chance of landing on the original signature's bits and
    # producing a "tampered" token that still verifies successfully.
    mid = len(payload) // 2
    flipped_char = "A" if payload[mid] != "A" else "B"
    tampered_payload = payload[:mid] + flipped_char + payload[mid + 1 :]
    bad = ".".join([header, tampered_payload, signature])
    assert verify_session_token(bad, s) is None

    # Also cover the complementary case: intact payload, tampered signature.
    # Flip a char in the middle of the signature (not the tail, which is
    # where the flakiness described above lives) for a deterministic mismatch.
    sig_mid = len(signature) // 2
    flipped_sig_char = "A" if signature[sig_mid] != "A" else "B"
    tampered_signature = signature[:sig_mid] + flipped_sig_char + signature[sig_mid + 1 :]
    bad_signature = ".".join([header, payload, tampered_signature])
    assert verify_session_token(bad_signature, s) is None


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


@pytest.mark.asyncio
async def test_cost_gate_check_does_not_inflate_counter_on_rejection(tmp_path: Path) -> None:
    """A rejected call must not leave the daily counter higher than it was
    before the attempt — cost_gate_check increments-then-decrements on
    overshoot, it doesn't just tack on a rejected attempt forever."""
    db = Database(tmp_path / "g3.sqlite")
    await db.connect()
    try:
        for _ in range(3):
            await db.increment_calls_today()
        for _ in range(5):
            with pytest.raises(CostGateExceeded):
                await cost_gate_check(db, limit=3)
        assert (await db.count_calls_today()) == 3
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cost_gate_check_concurrent_never_exceeds_limit(tmp_path: Path) -> None:
    """Regression test for the daily cost-gate TOCTOU: previously the gate
    did a plain SELECT here and the real increment happened much later in
    the request (after the session/IP checks), so N concurrent requests
    could all observe "under the limit" before any of them counted,
    blowing straight through daily_llm_call_limit. cost_gate_check now
    folds the check into the atomic increment itself, so firing many
    requests at once against a small limit must let through *exactly*
    `limit` of them — never more."""
    db = Database(tmp_path / "gate_race.sqlite")
    await db.connect()
    try:
        limit = 5
        n_requests = 25

        async def attempt() -> bool:
            try:
                await cost_gate_check(db, limit=limit)
            except CostGateExceeded:
                return False
            return True

        results = await asyncio.gather(*(attempt() for _ in range(n_requests)))
        accepted = sum(results)
        assert accepted == limit
        assert (await db.count_calls_today()) == limit
    finally:
        await db.close()


def _parsed_cookie(response: Response) -> SimpleCookie:
    raw = response.headers.get("set-cookie")
    assert raw is not None
    cookie: SimpleCookie = SimpleCookie()
    cookie.load(raw)
    return cookie


def test_set_session_cookie_issues_a_verifiable_token() -> None:
    s = make_settings(env="dev")
    response = Response()
    set_session_cookie(response, "sid-1", s)

    cookie = _parsed_cookie(response)
    morsel = cookie[SESSION_COOKIE]
    assert verify_session_token(morsel.value, s) == "sid-1"


def test_set_session_cookie_attributes_match_settings() -> None:
    s = make_settings(env="dev")
    response = Response()
    set_session_cookie(response, "sid-1", s)

    morsel = _parsed_cookie(response)[SESSION_COOKIE]
    assert morsel["httponly"] is True
    assert morsel["samesite"] == "lax"
    assert morsel["max-age"] == str(s.session_ttl_seconds)
    # secure is only forced when settings.env == "prod"
    assert not morsel["secure"]


def test_set_session_cookie_is_secure_in_prod() -> None:
    s = make_settings(env="prod")
    response = Response()
    set_session_cookie(response, "sid-1", s)

    morsel = _parsed_cookie(response)[SESSION_COOKIE]
    assert morsel["secure"] is True
