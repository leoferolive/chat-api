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
        "sessionId": "11111111-1111-4111-8111-111111111111",
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
    # Primary fails on the (streaming) answer phase only — router still
    # picks pages successfully via primary.
    mock_llm.stream_behaviour["mock/primary"] = "raise_open"
    body = {
        "sessionId": "22222222-2222-4222-8222-222222222222",
        "messages": [{"role": "user", "content": "Wiley?"}],
        "lang": "pt",
    }
    resp = await client.post("/chat/stream", json=body)
    assert resp.status_code == 200
    events = parse_sse_events(resp.text)
    done = [e for e in events if e["type"] == "done"][0]
    assert done["model"] == "mock/secondary"
    # Answer phase: primary failed-open before producing tokens, secondary
    # succeeded. `stream_calls` only records reaches that produced a stream.
    assert mock_llm.stream_calls == ["mock/secondary"]
    # The full call list includes the failed primary attempt on the answer.
    assert mock_llm.calls.count("mock/primary") == 2  # router OK + answer fail
    assert mock_llm.calls.count("mock/secondary") == 1  # answer success


@pytest.mark.asyncio
async def test_chat_stream_all_stream_providers_fail(client, mock_llm) -> None:
    # Router succeeds (default), but every provider fails on the answer
    # streaming phase. We expect an `error` SSE event.
    mock_llm.stream_behaviour["mock/primary"] = "raise_open"
    mock_llm.stream_behaviour["mock/secondary"] = "raise_open"
    body = {
        "sessionId": "33333333-3333-4333-8333-333333333333",
        "messages": [{"role": "user", "content": "anything"}],
        "lang": "pt",
    }
    resp = await client.post("/chat/stream", json=body)
    assert resp.status_code == 200
    events = parse_sse_events(resp.text)
    assert any(e["type"] == "error" for e in events)


@pytest.mark.asyncio
async def test_chat_stream_router_refuses_out_of_scope(client, mock_llm) -> None:
    # Router decides nothing in the wiki is relevant — we should refuse
    # without invoking the answer LLM at all.
    mock_llm.router_response = '{"paths": []}'
    body = {
        "sessionId": "55555555-5555-4555-8555-555555555555",
        "messages": [{"role": "user", "content": "qual a capital da frança?"}],
        "lang": "pt",
    }
    resp = await client.post("/chat/stream", json=body)
    assert resp.status_code == 200
    events = parse_sse_events(resp.text)
    types = [e["type"] for e in events]
    assert types == ["token", "done"]  # refusal token + done, no error
    # No streaming call was made.
    assert mock_llm.stream_calls == []
    # Refusal text is the persona's fallback line.
    refusal = events[0]["value"]
    assert "não tenho essa informação" in refusal.lower()
    # Router still consumed a provider call — daily counter must reflect it,
    # otherwise off-topic floods would never trip the cost gate.
    db = client.app.state.db  # type: ignore[attr-defined]
    assert (await db.count_calls_today()) >= 1


@pytest.mark.asyncio
async def test_chat_stream_turnstile_required_on_first(client, monkeypatch) -> None:
    # Re-enable turnstile for this test.
    from app.config import reset_settings_cache

    monkeypatch.setenv("TURNSTILE_DISABLED", "false")
    reset_settings_cache()
    # The dependency in the running app is captured at startup; force it via state.
    client.app.state.settings.turnstile_disabled = False  # type: ignore[attr-defined]

    body = {
        "sessionId": "44444444-4444-4444-8444-444444444444",
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
        "sessionId": "55555555-5555-4555-8555-555555555555",
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
        "sessionId": "66666666-6666-4666-8666-666666666666",
        "messages": [{"role": "user", "content": "Wiley?"}],
        "lang": "pt",
    }
    resp = await client.post("/chat/stream", json=body)
    assert resp.status_code == 200

    # Allow fire-and-forget save_turn tasks to flush.
    for _ in range(20):
        await asyncio.sleep(0.05)
        async with db._conn.execute(  # type: ignore[attr-defined]
            "SELECT role FROM messages WHERE session_id = ?", ("66666666-6666-4666-8666-666666666666",)
        ) as cur:
            rows = await cur.fetchall()
        if {r[0] for r in rows} >= {"user", "assistant"}:
            break
    roles = {r[0] for r in rows}
    assert "user" in roles
    assert "assistant" in roles


# ---------- input validation hard limits ------------------------------


@pytest.mark.asyncio
async def test_chat_stream_rejects_oversized_message(client) -> None:
    """Pydantic validator rejects content > MAX_USER_MESSAGE_CHARS."""
    body = {
        "sessionId": "77777777-7777-4777-8777-777777777777",
        "messages": [{"role": "user", "content": "x" * 1000}],
        "lang": "pt",
    }
    resp = await client.post("/chat/stream", json=body)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_chat_stream_rejects_too_many_messages(client) -> None:
    """Pydantic validator caps the messages array length."""
    body = {
        "sessionId": "88888888-8888-4888-8888-888888888888",
        "messages": [{"role": "user", "content": "ok"}] * 25,
        "lang": "pt",
    }
    resp = await client.post("/chat/stream", json=body)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_chat_stream_rejects_non_uuid_session(client) -> None:
    body = {
        "sessionId": "not-a-uuid",
        "messages": [{"role": "user", "content": "oi"}],
        "lang": "pt",
    }
    resp = await client.post("/chat/stream", json=body)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_chat_stream_rejects_invalid_lang(client) -> None:
    body = {
        "sessionId": "99999999-9999-4999-8999-999999999999",
        "messages": [{"role": "user", "content": "oi"}],
        "lang": "fr",
    }
    resp = await client.post("/chat/stream", json=body)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_chat_stream_session_message_cap(client, monkeypatch) -> None:
    """11th user message in same sessionId must return 429 session_limit_reached."""
    sid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    db = client.app.state.db  # type: ignore[attr-defined]
    # Pre-seed 10 user messages directly in the DB.
    for i in range(10):
        await db.save_turn(session_id=sid, role="user", content=f"msg{i}")

    body = {
        "sessionId": sid,
        "messages": [{"role": "user", "content": "11th"}],
        "lang": "pt",
    }
    resp = await client.post("/chat/stream", json=body)
    assert resp.status_code == 429
    events = parse_sse_events(resp.text)
    assert any(e.get("message") == "session_limit_reached" for e in events)
