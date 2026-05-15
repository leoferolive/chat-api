"""Tests for the LLM-based router (app/router.py)."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.models import ChatMessage
from app.router import (
    HISTORY_TURNS,
    MAX_PATHS,
    _extract_json_object,
    _parse_router_json,
    pick_paths,
)
from app.wiki_loader import WikiLoader


@pytest.fixture
def loader(settings: Settings) -> WikiLoader:
    return WikiLoader(settings.wiki_dir, poll_seconds=0)


class TestExtractJsonObject:
    """Direct unit coverage for the brace-balanced salvage scanner."""

    def test_preamble_then_object(self) -> None:
        text = 'Here is the JSON requested: {"paths": ["a.md"]}'
        assert _extract_json_object(text) == '{"paths": ["a.md"]}'

    def test_markdown_fence(self) -> None:
        text = '```json\n{"paths": ["a.md"]}\n```'
        assert _extract_json_object(text) == '{"paths": ["a.md"]}'

    def test_trailing_prose(self) -> None:
        text = '{"paths": []}\nLet me know if you need more.'
        assert _extract_json_object(text) == '{"paths": []}'

    def test_nested_object_balanced(self) -> None:
        text = 'note: {"paths": [], "meta": {"k": "v"}} done'
        assert _extract_json_object(text) == '{"paths": [], "meta": {"k": "v"}}'

    def test_brace_inside_string_value(self) -> None:
        # A '{' or '}' inside a JSON string must NOT move the brace depth,
        # otherwise a perfectly valid object is discarded and we waste a
        # failover round-trip (the whole point of salvage is to avoid that).
        text = 'Resposta: {"paths": ["a.md"], "note": "usa {chaves} aqui"} fim'
        assert (
            _extract_json_object(text)
            == '{"paths": ["a.md"], "note": "usa {chaves} aqui"}'
        )

    def test_escaped_quote_inside_string(self) -> None:
        text = r'{"paths": [], "q": "ele disse \"oi\" e } foi"}'
        assert _extract_json_object(text) == text

    def test_first_of_multiple_objects(self) -> None:
        text = '{"paths": ["a.md"]} {"paths": ["b.md"]}'
        assert _extract_json_object(text) == '{"paths": ["a.md"]}'

    def test_no_object_raises(self) -> None:
        with pytest.raises(ValueError):
            _extract_json_object("Here is the JSON requested")

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError):
            _extract_json_object("")


class TestParseRouterJson:
    """The strict object contract must survive the salvage path."""

    def test_salvages_object_with_brace_in_string(self) -> None:
        parsed = _parse_router_json('ok: {"paths": ["a.md"], "x": "b { c"}')
        assert parsed == {"paths": ["a.md"], "x": "b { c"}

    def test_non_object_still_rejected(self) -> None:
        for bogus in ("null", "[]", "42", '"just a string"', "[1, 2, 3]"):
            with pytest.raises((ValueError, TypeError)):
                _parse_router_json(bogus)

    def test_no_json_still_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _parse_router_json("Here is the JSON requested")


@pytest.mark.asyncio
async def test_returns_validated_paths(
    settings: Settings, loader: WikiLoader, mock_llm
) -> None:
    mock_llm.router_response = '{"paths": ["entities/wiley.md", "skills/backend.md"]}'
    paths = await pick_paths(
        question="qual a stack do leonardo?",
        history=[ChatMessage(role="user", content="qual a stack do leonardo?")],
        lang="pt",
        loader=loader,
        providers=settings.provider_list,
        settings=settings,
    )
    assert paths == ["entities/wiley.md", "skills/backend.md"]
    # exactly one router call (no failover needed)
    assert len(mock_llm.router_calls) == 1


@pytest.mark.asyncio
async def test_empty_paths_when_router_says_so(
    settings: Settings, loader: WikiLoader, mock_llm
) -> None:
    mock_llm.router_response = '{"paths": []}'
    paths = await pick_paths(
        question="qual a capital da frança?",
        history=[ChatMessage(role="user", content="qual a capital da frança?")],
        lang="pt",
        loader=loader,
        providers=settings.provider_list,
        settings=settings,
    )
    assert paths == []


@pytest.mark.asyncio
async def test_malformed_json_returns_empty(
    settings: Settings, loader: WikiLoader, mock_llm
) -> None:
    mock_llm.router_response = "not json at all"
    paths = await pick_paths(
        question="qualquer pergunta",
        history=[ChatMessage(role="user", content="qualquer pergunta")],
        lang="pt",
        loader=loader,
        providers=settings.provider_list,
        settings=settings,
    )
    assert paths == []


@pytest.mark.asyncio
async def test_drops_paths_unknown_to_loader(
    settings: Settings, loader: WikiLoader, mock_llm
) -> None:
    # only entities/wiley.md exists in the fixture; the other two are bogus
    mock_llm.router_response = (
        '{"paths": ["entities/wiley.md", "ghost/page.md", "../escape.md"]}'
    )
    paths = await pick_paths(
        question="me fala sobre wiley",
        history=[ChatMessage(role="user", content="me fala sobre wiley")],
        lang="pt",
        loader=loader,
        providers=settings.provider_list,
        settings=settings,
    )
    assert paths == ["entities/wiley.md"]


@pytest.mark.asyncio
async def test_invalid_paths_outcome_when_all_paths_bogus(
    settings: Settings, loader: WikiLoader, mock_llm
) -> None:
    # Router returns paths that all fail validation — distinct from "empty".
    from prometheus_client import REGISTRY

    def value_for(outcome: str) -> float:
        return REGISTRY.get_sample_value(
            "chat_api_router_outcome_total", {"outcome": outcome}
        ) or 0.0

    before = value_for("invalid_paths")
    mock_llm.router_response = '{"paths": ["ghost/x.md", "ghost/y.md"]}'
    paths = await pick_paths(
        question="qualquer coisa",
        history=[ChatMessage(role="user", content="qualquer coisa")],
        lang="pt",
        loader=loader,
        providers=settings.provider_list,
        settings=settings,
    )
    assert paths == []
    assert value_for("invalid_paths") - before == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_caps_at_max_paths(
    settings: Settings, loader: WikiLoader, mock_llm
) -> None:
    # Repeats are deduped first; pad with valid paths up to the cap.
    real = ["entities/wiley.md", "skills/backend.md"]
    # request 8 paths (some duplicates) — only valid + unique survive,
    # then capped at MAX_PATHS.
    flood = real + real + real + real
    mock_llm.router_response = f'{{"paths": {flood!r}}}'.replace("'", '"')
    paths = await pick_paths(
        question="tudo sobre o leonardo",
        history=[ChatMessage(role="user", content="tudo sobre o leonardo")],
        lang="pt",
        loader=loader,
        providers=settings.provider_list,
        settings=settings,
    )
    assert paths == real  # deduped to two — well under MAX_PATHS
    assert len(paths) <= MAX_PATHS


@pytest.mark.asyncio
async def test_provider_failover(
    settings: Settings, loader: WikiLoader, mock_llm
) -> None:
    # primary fails, secondary returns valid JSON
    mock_llm.behaviour["mock/primary"] = "raise_open"
    mock_llm.router_response = '{"paths": ["skills/backend.md"]}'
    paths = await pick_paths(
        question="skills do leonardo",
        history=[ChatMessage(role="user", content="skills do leonardo")],
        lang="pt",
        loader=loader,
        providers=settings.provider_list,
        settings=settings,
    )
    assert paths == ["skills/backend.md"]
    # primary attempted (and failed-open), secondary succeeded
    assert "mock/primary" in mock_llm.calls
    assert mock_llm.router_calls == ["mock/secondary"]


@pytest.mark.asyncio
async def test_falls_back_when_primary_returns_non_json(
    settings: Settings, loader: WikiLoader, mock_llm
) -> None:
    """Reproduces the prod bug: Gemini answered 'Here is the JSON requested'
    with no actual JSON. We must try the next provider instead of refusing."""
    mock_llm.router_response_by_model["mock/primary"] = "Here is the JSON requested"
    mock_llm.router_response_by_model["mock/secondary"] = (
        '{"paths": ["entities/wiley.md"]}'
    )
    paths = await pick_paths(
        question="me fala sobre wiley",
        history=[ChatMessage(role="user", content="me fala sobre wiley")],
        lang="pt",
        loader=loader,
        providers=settings.provider_list,
        settings=settings,
    )
    assert paths == ["entities/wiley.md"]
    # Both router calls happened (primary then secondary).
    assert mock_llm.router_calls == ["mock/primary", "mock/secondary"]


@pytest.mark.asyncio
async def test_salvages_json_from_preamble_without_failover(
    settings: Settings, loader: WikiLoader, mock_llm
) -> None:
    """When the model emits a prose preamble or markdown fence *followed by*
    valid JSON (Gemini's actual behaviour: 'Here is the JSON requested:
    {...}'), we must extract and use it on the FIRST provider — not waste a
    failover round-trip (Gemini free-tier is 5 req/min)."""
    salvageable = (
        'Here is the JSON requested: {"paths": ["entities/wiley.md"]}',
        '```json\n{"paths": ["entities/wiley.md"]}\n```',
        '```\n{"paths": ["entities/wiley.md"]}\n```',
        'Sure!\n{"paths": ["entities/wiley.md"]}\nLet me know if you need more.',
    )
    for resp in salvageable:
        mock_llm.reset()
        mock_llm.router_response_by_model["mock/primary"] = resp
        mock_llm.router_response_by_model["mock/secondary"] = (
            '{"paths": ["skills/ai.md"]}'
        )
        paths = await pick_paths(
            question="me fala sobre wiley",
            history=[ChatMessage(role="user", content="me fala sobre wiley")],
            lang="pt",
            loader=loader,
            providers=settings.provider_list,
            settings=settings,
        )
        assert paths == ["entities/wiley.md"], f"failed to salvage {resp!r}"
        assert mock_llm.router_calls == ["mock/primary"], (
            f"failover fired instead of salvaging {resp!r}"
        )


@pytest.mark.asyncio
async def test_falls_back_when_primary_returns_non_object_json(
    settings: Settings, loader: WikiLoader, mock_llm
) -> None:
    """`"null"`, `"[]"`, `"42"` are valid JSON but don't match the router's
    `{"paths": [...]}` contract. They must be treated as a parse failure so
    the failover kicks in instead of silently refusing."""
    for bogus in ('"null"', "null", "[]", "42", '"just a string"'):
        mock_llm.reset()
        mock_llm.router_response_by_model["mock/primary"] = bogus
        mock_llm.router_response_by_model["mock/secondary"] = (
            '{"paths": ["entities/wiley.md"]}'
        )
        paths = await pick_paths(
            question=f"qualquer coisa ({bogus})",
            history=[ChatMessage(role="user", content="qualquer coisa")],
            lang="pt",
            loader=loader,
            providers=settings.provider_list,
            settings=settings,
        )
        assert paths == ["entities/wiley.md"], f"failed for bogus={bogus!r}"
        assert mock_llm.router_calls == ["mock/primary", "mock/secondary"], (
            f"failover did not fire for bogus={bogus!r}"
        )


@pytest.mark.asyncio
async def test_parse_error_outcome_only_when_all_providers_fail_validation(
    settings: Settings, loader: WikiLoader, mock_llm
) -> None:
    """`outcome=parse_error` should fire only when *every* provider returned
    non-JSON. A primary that fails validation but a secondary that succeeds
    must report `outcome=ok`."""
    from prometheus_client import REGISTRY

    def value_for(outcome: str) -> float:
        return REGISTRY.get_sample_value(
            "chat_api_router_outcome_total", {"outcome": outcome}
        ) or 0.0

    parse_before = value_for("parse_error")
    ok_before = value_for("ok")

    mock_llm.router_response_by_model["mock/primary"] = "garbage"
    mock_llm.router_response_by_model["mock/secondary"] = (
        '{"paths": ["skills/backend.md"]}'
    )
    paths = await pick_paths(
        question="skills do leonardo",
        history=[ChatMessage(role="user", content="skills do leonardo")],
        lang="pt",
        loader=loader,
        providers=settings.provider_list,
        settings=settings,
    )
    assert paths == ["skills/backend.md"]
    assert value_for("parse_error") - parse_before == pytest.approx(0.0)
    assert value_for("ok") - ok_before == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_all_providers_fail_returns_empty(
    settings: Settings, loader: WikiLoader, mock_llm
) -> None:
    mock_llm.behaviour["mock/primary"] = "raise_open"
    mock_llm.behaviour["mock/secondary"] = "raise_open"
    paths = await pick_paths(
        question="qualquer",
        history=[ChatMessage(role="user", content="qualquer")],
        lang="pt",
        loader=loader,
        providers=settings.provider_list,
        settings=settings,
    )
    assert paths == []


@pytest.mark.asyncio
async def test_history_truncated_to_last_n_turns(
    settings: Settings, loader: WikiLoader, mock_llm
) -> None:
    # Build 10 turns; router should only see the last HISTORY_TURNS.
    turns: list[ChatMessage] = []
    for i in range(10):
        role = "user" if i % 2 == 0 else "assistant"
        turns.append(ChatMessage(role=role, content=f"msg-{i}"))
    captured_messages: list[list[dict]] = []

    import app.llm_router as llm_router_mod

    real = llm_router_mod.litellm.acompletion

    async def spy(**kwargs):
        if kwargs.get("stream") is False:
            captured_messages.append(list(kwargs["messages"]))
        return await real(**kwargs)

    import pytest as _pytest  # noqa: F401 — for monkeypatch typing

    monkeypatch = _pytest.MonkeyPatch()
    monkeypatch.setattr(llm_router_mod.litellm, "acompletion", spy)
    try:
        await pick_paths(
            question="msg-final",
            history=turns,
            lang="pt",
            loader=loader,
            providers=settings.provider_list,
            settings=settings,
        )
    finally:
        monkeypatch.undo()

    assert captured_messages, "router should have called the LLM"
    sent = captured_messages[0]
    # 1 system + last HISTORY_TURNS user/assistant + 1 trailing user (the question)
    assert sent[0]["role"] == "system"
    assert sent[-1]["role"] == "user"
    assert sent[-1]["content"] == "msg-final"
    middle = sent[1:-1]
    assert len(middle) == HISTORY_TURNS


@pytest.mark.asyncio
async def test_index_text_in_system_prompt(
    settings: Settings, loader: WikiLoader, mock_llm
) -> None:
    captured: list[list[dict]] = []
    import app.llm_router as llm_router_mod

    real = llm_router_mod.litellm.acompletion

    async def spy(**kwargs):
        if kwargs.get("stream") is False:
            captured.append(list(kwargs["messages"]))
        return await real(**kwargs)

    import pytest as _pytest

    monkeypatch = _pytest.MonkeyPatch()
    monkeypatch.setattr(llm_router_mod.litellm, "acompletion", spy)
    try:
        await pick_paths(
            question="me fala da wiley",
            history=[ChatMessage(role="user", content="me fala da wiley")],
            lang="pt",
            loader=loader,
            providers=settings.provider_list,
            settings=settings,
        )
    finally:
        monkeypatch.undo()

    system = captured[0][0]["content"]
    assert "entities/wiley.md" in system  # raw index.md content embedded
