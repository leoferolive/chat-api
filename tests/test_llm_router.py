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


# ---- complete_once (non-streaming) -----------------------------------------


@pytest.mark.asyncio
async def test_complete_once_returns_text_and_attempts(mock_llm) -> None:
    from app.llm_router import complete_once

    mock_llm.router_response = '{"answer": "hi"}'
    result = await complete_once(
        [{"role": "user", "content": "hi"}],
        ["mock/primary", "mock/secondary"],
        max_tokens=50,
        response_format={"type": "json_object"},
    )
    assert result["text"] == '{"answer": "hi"}'
    assert result["model"] == "mock/primary"
    assert result["attempts"] == ["mock/primary"]
    assert result["tokens"] == {"prompt": 8, "completion": 3}


@pytest.mark.asyncio
async def test_complete_once_failover(mock_llm) -> None:
    from app.llm_router import complete_once

    mock_llm.router_behaviour["mock/primary"] = "raise_open"
    result = await complete_once(
        [{"role": "user", "content": "hi"}],
        ["mock/primary", "mock/secondary"],
        max_tokens=50,
    )
    assert result["model"] == "mock/secondary"
    assert result["attempts"] == ["mock/primary", "mock/secondary"]


@pytest.mark.asyncio
async def test_complete_once_all_fail_raises(mock_llm) -> None:
    from app.llm_router import complete_once

    mock_llm.router_behaviour["mock/primary"] = "raise_open"
    mock_llm.router_behaviour["mock/secondary"] = "raise_open"
    with pytest.raises(AllProvidersFailed):
        await complete_once(
            [{"role": "user", "content": "hi"}],
            ["mock/primary", "mock/secondary"],
        )


@pytest.mark.asyncio
async def test_complete_once_validator_failover(mock_llm) -> None:
    """When the validator rejects the primary's text, fail over to the next."""
    import json

    from app.llm_router import complete_once

    mock_llm.router_response_by_model["mock/primary"] = "Here is the JSON requested"
    mock_llm.router_response_by_model["mock/secondary"] = '{"paths": ["x.md"]}'
    result = await complete_once(
        [{"role": "user", "content": "hi"}],
        ["mock/primary", "mock/secondary"],
        validator=json.loads,
    )
    assert result["model"] == "mock/secondary"
    assert result["attempts"] == ["mock/primary", "mock/secondary"]
    assert result["validated"] == {"paths": ["x.md"]}
    # Both providers were called for the router phase.
    assert mock_llm.router_calls == ["mock/primary", "mock/secondary"]


@pytest.mark.asyncio
async def test_complete_once_validator_all_fail_raises(mock_llm) -> None:
    """Every provider returning bogus text exhausts the loop and raises."""
    import json

    from app.llm_router import complete_once

    mock_llm.router_response_by_model["mock/primary"] = "Here is the JSON"
    mock_llm.router_response_by_model["mock/secondary"] = "still no JSON"
    with pytest.raises(AllProvidersFailed) as ei:
        await complete_once(
            [{"role": "user", "content": "hi"}],
            ["mock/primary", "mock/secondary"],
            validator=json.loads,
        )
    # The exception must remember that the last failure was a validator one,
    # so callers (pick_paths) can choose between provider_error and parse_error
    # outcomes for dashboards.
    assert getattr(ei.value, "last_phase", None) == "validate"


@pytest.mark.asyncio
async def test_complete_once_passes_response_format(monkeypatch) -> None:
    from app import llm_router as llm_router_mod
    from app.llm_router import complete_once

    captured: dict = {}

    async def capturing(**kwargs):
        captured.update(kwargs)
        return {
            "choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    monkeypatch.setattr(llm_router_mod.litellm, "acompletion", capturing)
    await complete_once(
        [{"role": "user", "content": "hi"}],
        ["mock/primary"],
        response_format={"type": "json_object"},
    )
    assert captured["response_format"] == {"type": "json_object"}
    assert captured["stream"] is False
