"""Tests for sessions_created counter + /metrics-traffic gauges."""

from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_upsert_session_reports_first_insert(client) -> None:
    db = client.app.state.db  # type: ignore[attr-defined]
    created_first = await db.upsert_session("sess-a", "ip-a", "pt")
    created_again = await db.upsert_session("sess-a", "ip-a", "pt")
    assert created_first is True
    assert created_again is False


@pytest.mark.asyncio
async def test_sessions_created_counter_increments_only_on_new(client, mock_llm) -> None:
    # Prometheus counters are process-globals; other tests share the same
    # registry. Read the before/after delta — both legal langs accumulate
    # across the suite, but the delta for one fresh sessionId must be exactly 1.
    from tests.test_metrics import metric_value

    base = {"sessionId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "lang": "en"}
    body1 = {**base, "messages": [{"role": "user", "content": "oi"}]}
    body2 = {
        **base,
        "messages": [
            {"role": "user", "content": "oi"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "de novo"},
        ],
    }
    before_body = (await client.get("/metrics", headers={"host": "127.0.0.1"})).text
    before = metric_value(
        before_body, "chat_api_sessions_created_total", {"lang": "en"}
    )
    r1 = await client.post("/chat/stream", json=body1)
    assert r1.status_code == 200
    r2 = await client.post("/chat/stream", json=body2)
    assert r2.status_code == 200
    await asyncio.sleep(0.05)

    after_body = (await client.get("/metrics", headers={"host": "127.0.0.1"})).text
    after = metric_value(
        after_body, "chat_api_sessions_created_total", {"lang": "en"}
    )
    assert after - before == pytest.approx(1.0), (
        "exactly one new session must have been counted for this label"
    )


@pytest.mark.asyncio
async def test_metrics_traffic_endpoint(client) -> None:
    db = client.app.state.db  # type: ignore[attr-defined]
    await db.upsert_session("s1", "ip-1", "pt")
    await db.upsert_session("s2", "ip-2", "en")
    await db.upsert_session("s3", "ip-2", "pt")  # same ip, distinct session
    await db.save_turn(session_id="s1", role="user", content="oi")
    await db.save_turn(session_id="s2", role="user", content="hi")

    resp = await client.get("/metrics-traffic", headers={"host": "127.0.0.1"})
    assert resp.status_code == 200
    body = resp.text
    assert "chat_api_unique_sessions" in body
    assert "chat_api_unique_ips" in body
    assert "chat_api_sessions_by_lang" in body
    # Sessions with user messages today
    assert any(
        line.startswith('chat_api_unique_sessions{window="today"}')
        and float(line.split()[-1]) >= 2.0
        for line in body.splitlines()
    )
    # Distinct IPs today (2, because two sessions share an IP)
    assert any(
        line.startswith('chat_api_unique_ips{window="today"}')
        and float(line.split()[-1]) == 2.0
        for line in body.splitlines()
    )


@pytest.mark.asyncio
async def test_metrics_traffic_external_host_404(client) -> None:
    resp = await client.get(
        "/metrics-traffic", headers={"host": "chat-dev.leoferolive.com.br"}
    )
    assert resp.status_code == 404
