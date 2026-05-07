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
async def test_metrics_endpoint_exposes_chat_api_metrics(client) -> None:
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    body = resp.text
    assert "# HELP chat_api_chats_total" in body
    assert "# TYPE chat_api_tokens_total counter" in body
    assert "# TYPE chat_api_chat_duration_seconds histogram" in body
    assert "chat_api_daily_calls" in body


@pytest.mark.asyncio
async def test_metrics_increment_on_successful_chat(client, mock_llm) -> None:
    before_body = (await client.get("/metrics")).text
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
        "sessionId": "sess-metrics-ok",
        "messages": [{"role": "user", "content": "hello"}],
        "lang": "pt",
    }
    resp = await client.post("/chat/stream", json=body)
    assert resp.status_code == 200

    after_body = (await client.get("/metrics")).text
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
    # mock_llm emits prompt=12, completion=7 in its usage chunk
    assert p_tokens_after - p_tokens_before == pytest.approx(12.0)
    assert c_tokens_after >= 7.0
    # Histogram observation now present
    assert "chat_api_chat_duration_seconds_bucket" in after_body


@pytest.mark.asyncio
async def test_metrics_cost_gate_counter(client) -> None:
    db = client.app.state.db  # type: ignore[attr-defined]
    settings = client.app.state.settings  # type: ignore[attr-defined]

    before = metric_value(
        (await client.get("/metrics")).text, "chat_api_cost_gate_hits_total"
    )
    for _ in range(settings.daily_llm_call_limit):
        await db.increment_calls_today()

    body = {
        "sessionId": "sess-metrics-gate",
        "messages": [{"role": "user", "content": "hi"}],
        "lang": "pt",
    }
    resp = await client.post("/chat/stream", json=body)
    assert resp.status_code == 503

    after_body = (await client.get("/metrics")).text
    after = metric_value(after_body, "chat_api_cost_gate_hits_total")
    assert after - before >= 1.0
    # Gauge reflects current daily_calls
    daily = metric_value(after_body, "chat_api_daily_calls")
    assert daily >= settings.daily_llm_call_limit


@pytest.mark.asyncio
async def test_metrics_provider_failure_then_fallback(client, mock_llm) -> None:
    mock_llm.behaviour["mock/primary"] = "raise_open"
    before = metric_value(
        (await client.get("/metrics")).text,
        "chat_api_provider_failures_total",
        {"model": "mock/primary", "phase": "open"},
    )

    body = {
        "sessionId": "sess-metrics-fail",
        "messages": [{"role": "user", "content": "hi"}],
        "lang": "pt",
    }
    resp = await client.post("/chat/stream", json=body)
    assert resp.status_code == 200  # secondary picked up

    after_body = (await client.get("/metrics")).text
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
