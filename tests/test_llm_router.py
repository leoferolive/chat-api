"""Tests for the LLM router fallback behaviour."""

from __future__ import annotations

import pytest

from app.llm_router import AllProvidersFailed, stream_completion


@pytest.mark.asyncio
async def test_primary_provider_succeeds(mock_llm) -> None:
    events = []
    async for ev in stream_completion(
        [{"role": "user", "content": "hi"}],
        ["mock/primary", "mock/secondary"],
    ):
        events.append(ev)

    assert mock_llm.calls == ["mock/primary"]
    types = [e["type"] for e in events]
    assert types[0] == "start"
    assert "token" in types
    assert types[-1] == "done"
    done = events[-1]
    assert done["model"] == "mock/primary"
    assert done["tokens"]["prompt"] == 12
    assert done["tokens"]["completion"] == 7


@pytest.mark.asyncio
async def test_falls_back_when_primary_open_fails(mock_llm) -> None:
    mock_llm.behaviour["mock/primary"] = "raise_open"
    events = []
    async for ev in stream_completion(
        [{"role": "user", "content": "hi"}],
        ["mock/primary", "mock/secondary"],
    ):
        events.append(ev)
    assert mock_llm.calls == ["mock/primary", "mock/secondary"]
    assert events[-1]["model"] == "mock/secondary"
    assert events[-1]["attempts"] == ["mock/primary", "mock/secondary"]


@pytest.mark.asyncio
async def test_all_providers_fail(mock_llm) -> None:
    mock_llm.behaviour["mock/primary"] = "raise_open"
    mock_llm.behaviour["mock/secondary"] = "raise_open"
    with pytest.raises(AllProvidersFailed):
        async for _ev in stream_completion(
            [{"role": "user", "content": "hi"}],
            ["mock/primary", "mock/secondary"],
        ):
            pass


@pytest.mark.asyncio
async def test_no_providers_raises(mock_llm) -> None:
    with pytest.raises(AllProvidersFailed):
        async for _ev in stream_completion([{"role": "user", "content": "hi"}], []):
            pass
