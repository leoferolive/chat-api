"""Tests for the Prometheus /metrics endpoint instrumentation."""

from __future__ import annotations

import re

import pytest


def metric_value(body: str, name: str, labels: dict[str, str] | None = None) -> float:
    """Return the value of a single Prometheus sample. 0.0 if absent."""
    if labels:
        pattern = rf"^{re.escape(name)}\{{([^}}]*)\}}\s+([\d.eE+-]+)"
        for line in body.splitlines():
            m = re.match(pattern, line)
            if not m:
                continue
            line_labels = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
            if all(line_labels.get(k) == v for k, v in labels.items()):
                return float(m.group(2))
        return 0.0
    pattern = rf"^{re.escape(name)}\s+([\d.eE+-]+)"
    for line in body.splitlines():
        m = re.match(pattern, line)
        if m:
            return float(m.group(1))
    return 0.0


@pytest.mark.asyncio
async def test_metrics_blocked_for_external_host(client) -> None:
    """Public Ingress would forward Host=chat-dev.leoferolive.com.br — must 404."""
    resp = await client.get("/metrics", headers={"host": "chat-dev.leoferolive.com.br"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_metrics_endpoint_exposes_chat_api_metrics(client) -> None:
    resp = await client.get("/metrics", headers={"host": "127.0.0.1"})
    assert resp.status_code == 200
    body = resp.text
    assert "# HELP chat_api_chats_total" in body
    assert "# TYPE chat_api_tokens_total counter" in body
    assert "# TYPE chat_api_chat_duration_seconds histogram" in body
    assert "chat_api_daily_calls" in body


@pytest.mark.asyncio
async def test_metrics_increment_on_successful_chat(client, mock_llm) -> None:
    before_body = (await client.get("/metrics", headers={"host": "127.0.0.1"})).text
    chats_before = metric_value(
        before_body,
        "chat_api_chats_total",
        {"status": "ok", "model": "mock/primary"},
    )
    p_tokens_before = metric_value(
        before_body,
        "chat_api_tokens_total",
        {"kind": "prompt", "model": "mock/primary"},
    )

    body = {
        "sessionId": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "messages": [{"role": "user", "content": "hello"}],
        "lang": "pt",
    }
    resp = await client.post("/chat/stream", json=body)
    assert resp.status_code == 200

    after_body = (await client.get("/metrics", headers={"host": "127.0.0.1"})).text
    chats_after = metric_value(
        after_body,
        "chat_api_chats_total",
        {"status": "ok", "model": "mock/primary"},
    )
    p_tokens_after = metric_value(
        after_body,
        "chat_api_tokens_total",
        {"kind": "prompt", "model": "mock/primary"},
    )
    c_tokens_after = metric_value(
        after_body,
        "chat_api_tokens_total",
        {"kind": "completion", "model": "mock/primary"},
    )
    assert chats_after - chats_before == pytest.approx(1.0)
    # mock_llm emits prompt=8 (router) + 12 (answer) on the same model.
    assert p_tokens_after - p_tokens_before == pytest.approx(20.0)
    assert c_tokens_after >= 7.0
    # Histogram observation now present
    assert "chat_api_chat_duration_seconds_bucket" in after_body


@pytest.mark.asyncio
async def test_metrics_cost_gate_counter(client) -> None:
    db = client.app.state.db  # type: ignore[attr-defined]
    settings = client.app.state.settings  # type: ignore[attr-defined]

    before = metric_value(
        (await client.get("/metrics", headers={"host": "127.0.0.1"})).text, "chat_api_cost_gate_hits_total"
    )
    for _ in range(settings.daily_llm_call_limit):
        await db.increment_calls_today()

    body = {
        "sessionId": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        "messages": [{"role": "user", "content": "hi"}],
        "lang": "pt",
    }
    resp = await client.post("/chat/stream", json=body)
    assert resp.status_code == 503

    after_body = (await client.get("/metrics", headers={"host": "127.0.0.1"})).text
    after = metric_value(after_body, "chat_api_cost_gate_hits_total")
    assert after - before >= 1.0
    # Gauge reflects current daily_calls
    daily = metric_value(after_body, "chat_api_daily_calls")
    assert daily >= settings.daily_llm_call_limit


@pytest.mark.asyncio
async def test_metrics_chat_user_label_set_when_userName_present(client, mock_llm) -> None:
    body = {
        "sessionId": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
        "messages": [{"role": "user", "content": "oi"}],
        "lang": "pt",
        "userName": "Léo Ferreira",
    }
    resp = await client.post("/chat/stream", json=body)
    assert resp.status_code == 200

    metrics_body = (await client.get("/metrics", headers={"host": "127.0.0.1"})).text
    # Find any chats_total line for status=ok and inspect the user label.
    user_label_seen = None
    for line in metrics_body.splitlines():
        if line.startswith("chat_api_chats_total{") and 'status="ok"' in line and 'lang="pt"' in line:
            m = re.search(r'user="([^"]+)"', line)
            if m and m.group(1) != "anonymous":
                user_label_seen = m.group(1)
                break
    assert user_label_seen is not None
    assert user_label_seen.startswith("leo#")


@pytest.mark.asyncio
async def test_metrics_chat_user_label_anonymous_when_userName_omitted(client, mock_llm) -> None:
    body = {
        "sessionId": "ffffffff-ffff-4fff-8fff-ffffffffffff",
        "messages": [{"role": "user", "content": "oi"}],
        "lang": "pt",
    }
    resp = await client.post("/chat/stream", json=body)
    assert resp.status_code == 200

    metrics_body = (await client.get("/metrics", headers={"host": "127.0.0.1"})).text
    found_anonymous = any(
        line.startswith("chat_api_chats_total{")
        and 'status="ok"' in line
        and 'user="anonymous"' in line
        for line in metrics_body.splitlines()
    )
    assert found_anonymous


@pytest.mark.asyncio
async def test_chat_request_rejects_malformed_userName(client) -> None:
    body = {
        "sessionId": "11111111-1111-4111-8111-111111111111",
        "messages": [{"role": "user", "content": "oi"}],
        "lang": "pt",
        "userName": "x" * 100,  # exceeds max_length=40
    }
    resp = await client.post("/chat/stream", json=body)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_db_persists_user_name_on_session(client, mock_llm) -> None:
    body = {
        "sessionId": "22222222-2222-4222-8222-222222222222",
        "messages": [{"role": "user", "content": "oi"}],
        "lang": "pt",
        "userName": "Maria",
    }
    resp = await client.post("/chat/stream", json=body)
    assert resp.status_code == 200

    db = client.app.state.db  # type: ignore[attr-defined]
    # upsert_session is fire-and-forget; give the loop a tick.
    import asyncio

    await asyncio.sleep(0.05)
    async with db._conn.execute(
        "SELECT user_name FROM sessions WHERE id = ?",
        ("22222222-2222-4222-8222-222222222222",),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "Maria"


@pytest.mark.asyncio
async def test_metrics_provider_failure_then_fallback(client, mock_llm) -> None:
    # Primary fails on the streaming (answer) phase. Router still picks via
    # primary, so we expect exactly one open-failure on primary (the answer
    # call) and a successful answer call on secondary.
    mock_llm.stream_behaviour["mock/primary"] = "raise_open"
    before = metric_value(
        (await client.get("/metrics", headers={"host": "127.0.0.1"})).text,
        "chat_api_provider_failures_total",
        {"model": "mock/primary", "phase": "open"},
    )

    body = {
        "sessionId": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        "messages": [{"role": "user", "content": "hi"}],
        "lang": "pt",
    }
    resp = await client.post("/chat/stream", json=body)
    assert resp.status_code == 200  # secondary picked up

    after_body = (await client.get("/metrics", headers={"host": "127.0.0.1"})).text
    after = metric_value(
        after_body,
        "chat_api_provider_failures_total",
        {"model": "mock/primary", "phase": "open"},
    )
    assert after - before == pytest.approx(1.0)
    success = metric_value(
        after_body,
        "chat_api_provider_attempts_total",
        {"model": "mock/secondary", "result": "success"},
    )
    assert success >= 1.0
