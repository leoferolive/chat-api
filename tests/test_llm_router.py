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


@pytest.mark.asyncio
async def test_usage_chunk_populates_prompt_tokens(mock_llm) -> None:
    """The mock emits a final chunk with `usage`; we must surface it."""
    events = []
    async for ev in stream_completion(
        [{"role": "user", "content": "hi"}],
        ["mock/primary"],
    ):
        events.append(ev)
    done = events[-1]
    assert done["type"] == "done"
    assert done["tokens"]["prompt"] == 12
    assert done["tokens"]["completion"] == 7


@pytest.mark.asyncio
async def test_stream_options_include_usage_passed_to_litellm(
    mock_llm, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`stream_options={'include_usage': True}` must reach litellm.acompletion."""
    from app import llm_router as llm_router_mod

    captured: dict = {}

    async def capturing(*, model, messages, stream, **kwargs):
        captured["kwargs"] = kwargs

        async def gen():
            yield {"choices": [{"delta": {"content": "hi"}}]}

        return gen()

    monkeypatch.setattr(llm_router_mod.litellm, "acompletion", capturing)
    async for _ev in stream_completion(
        [{"role": "user", "content": "hi"}], ["mock/primary"]
    ):
        pass
    assert captured["kwargs"].get("stream_options") == {"include_usage": True}


@pytest.mark.asyncio
async def test_zai_prefix_routes_to_openai_compatible(
    mock_llm, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`zai/<model>` becomes `openai/<model>` + api_base + api_key extras."""
    from app import config as config_mod
    from app import llm_router as llm_router_mod

    monkeypatch.setenv("ZAI_API_KEY", "test-zai-key")
    monkeypatch.setenv("ZAI_BASE_URL", "https://api.z.ai/api/paas/v4/")
    config_mod.reset_settings_cache()

    captured: dict = {}

    async def capturing_acompletion(*, model, messages, stream, **kwargs):
        captured["model"] = model
        captured["kwargs"] = kwargs

        async def gen():
            yield {"choices": [{"delta": {"content": "ok"}}]}
            yield {
                "choices": [{"delta": {}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }

        return gen()

    monkeypatch.setattr(llm_router_mod.litellm, "acompletion", capturing_acompletion)

    async for _ev in stream_completion(
        [{"role": "user", "content": "hi"}], ["zai/glm-4.6"]
    ):
        pass

    assert captured["model"] == "openai/glm-4.6"
    assert captured["kwargs"]["api_base"] == "https://api.z.ai/api/paas/v4/"
    assert captured["kwargs"]["api_key"] == "test-zai-key"
