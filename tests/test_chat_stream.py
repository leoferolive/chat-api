"""Integration tests for POST /chat/stream and GET /healthz."""

from __future__ import annotations

import asyncio
import json
import re

import pytest


def parse_sse_events(body: str) -> list[dict]:
    """Very small SSE parser tailored to our payload shape."""
    events: list[dict] = []
    for chunk in re.split(r"\n\n+", body.strip()):
        for line in chunk.splitlines():
            if line.startswith("data:"):
                payload = line[len("data:"):].strip()
                if not payload:
                    continue
                try:
                    events.append(json.loads(payload))
                except json.JSONDecodeError:
                    pass
    return events


@pytest.mark.asyncio
async def test_healthz(client) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_chat_stream_happy_path(client, mock_llm) -> None:
    body = {
        "sessionId": "sess-happy",
        "messages": [{"role": "user", "content": "Como foi o trabalho do Leonardo na Wiley?"}],
        "lang": "pt",
        "turnstileToken": None,
    }
    resp = await client.post("/chat/stream", json=body)
    assert resp.status_code == 200
    events = parse_sse_events(resp.text)
    types = [e["type"] for e in events]
    assert "token" in types
    assert types[-1] == "done"
    assert events[-1]["model"] == "mock/primary"
    # Cookie was issued on first message
    assert any(c.startswith("chat_session=") for c in resp.headers.get_list("set-cookie"))


@pytest.mark.asyncio
async def test_chat_stream_provider_fallback(client, mock_llm) -> None:
    mock_llm.behaviour["mock/primary"] = "raise_open"
    body = {
        "sessionId": "sess-fallback",
        "messages": [{"role": "user", "content": "Wiley?"}],
        "lang": "pt",
    }
    resp = await client.post("/chat/stream", json=body)
    assert resp.status_code == 200
    events = parse_sse_events(resp.text)
    done = [e for e in events if e["type"] == "done"][0]
    assert done["model"] == "mock/secondary"
    assert mock_llm.calls == ["mock/primary", "mock/secondary"]


@pytest.mark.asyncio
async def test_chat_stream_all_providers_fail(client, mock_llm) -> None:
    mock_llm.behaviour["mock/primary"] = "raise_open"
    mock_llm.behaviour["mock/secondary"] = "raise_open"
    body = {
        "sessionId": "sess-allfail",
        "messages": [{"role": "user", "content": "anything"}],
        "lang": "pt",
    }
    resp = await client.post("/chat/stream", json=body)
    # SSE response is still 200; the error event is in the body.
    assert resp.status_code == 200
    events = parse_sse_events(resp.text)
    assert any(e["type"] == "error" for e in events)


@pytest.mark.asyncio
async def test_chat_stream_turnstile_required_on_first(client, monkeypatch) -> None:
    # Re-enable turnstile for this test.
    from app.config import reset_settings_cache

    monkeypatch.setenv("TURNSTILE_DISABLED", "false")
    reset_settings_cache()
    # The dependency in the running app is captured at startup; force it via state.
    client.app.state.settings.turnstile_disabled = False  # type: ignore[attr-defined]

    body = {
        "sessionId": "sess-tt",
        "messages": [{"role": "user", "content": "hello"}],
        "lang": "pt",
        "turnstileToken": None,
    }
    resp = await client.post("/chat/stream", json=body)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_chat_stream_cost_gate(client) -> None:
    db = client.app.state.db  # type: ignore[attr-defined]
    settings = client.app.state.settings  # type: ignore[attr-defined]
    # Hammer counter past the limit.
    for _ in range(settings.daily_llm_call_limit):
        await db.increment_calls_today()

    body = {
        "sessionId": "sess-gate",
        "messages": [{"role": "user", "content": "hi"}],
        "lang": "pt",
    }
    resp = await client.post("/chat/stream", json=body)
    assert resp.status_code == 503
    events = parse_sse_events(resp.text)
    assert any(e["type"] == "error" for e in events)


@pytest.mark.asyncio
async def test_chat_stream_persists_messages(client) -> None:
    db = client.app.state.db  # type: ignore[attr-defined]
    body = {
        "sessionId": "sess-persist",
        "messages": [{"role": "user", "content": "Wiley?"}],
        "lang": "pt",
    }
    resp = await client.post("/chat/stream", json=body)
    assert resp.status_code == 200

    # Allow fire-and-forget save_turn tasks to flush.
    for _ in range(20):
        await asyncio.sleep(0.05)
        async with db._conn.execute(  # type: ignore[attr-defined]
            "SELECT role FROM messages WHERE session_id = ?", ("sess-persist",)
        ) as cur:
            rows = await cur.fetchall()
        if {r[0] for r in rows} >= {"user", "assistant"}:
            break
    roles = {r[0] for r in rows}
    assert "user" in roles
    assert "assistant" in roles
