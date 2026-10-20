"""Tests for per-call USD cost calculation."""

from __future__ import annotations

import litellm
import pytest

from app.cost import compute_cost, provider_of


def test_provider_of_with_prefix() -> None:
    assert provider_of("openrouter/google/gemini-2.5-flash-lite") == "openrouter"
    assert provider_of("gemini/gemini-2.5-flash") == "gemini"
    assert provider_of("zai/glm-4.6") == "zai"


def test_provider_of_without_prefix() -> None:
    assert provider_of("gpt-4o") == "openai"


def test_provider_of_alias_in_pricing_table_is_litellm_proxy(monkeypatch) -> None:
    """Alias do proxy com preço em LLM_ALIAS_PRICING é roteado pelo LiteLLM proxy."""
    from app import config as config_mod

    monkeypatch.setenv("LLM_ALIAS_PRICING", '{"company-code": {"input": 0.30, "output": 1.20}}')
    config_mod.reset_settings_cache()
    try:
        assert provider_of("company-code") == "litellm-proxy"
        assert provider_of("gpt-4o") == "openai"
        assert provider_of("openrouter/google/gemini-2.5-flash-lite") == "openrouter"
    finally:
        config_mod.reset_settings_cache()


def test_provider_of_bare_model_is_litellm_proxy_when_proxy_configured(monkeypatch) -> None:
    """Com LITELLM_PROXY_URL definido, todo modelo sem prefixo passa pelo proxy."""
    from app import config as config_mod

    monkeypatch.setenv("LITELLM_PROXY_URL", "http://litellm.ai.svc:4000")
    config_mod.reset_settings_cache()
    try:
        assert provider_of("company-reasoning") == "litellm-proxy"
        assert provider_of("zai/glm-4.6") == "zai"
    finally:
        config_mod.reset_settings_cache()


def test_cost_zero_when_no_tokens() -> None:
    assert compute_cost("gpt-4o-mini", 0, 0) == 0.0


def test_cost_unknown_model_returns_zero() -> None:
    # Custom self-hosted model not in LiteLLM pricing map.
    assert compute_cost("zai/some-private-model", 100, 50) == 0.0


def test_cost_known_model_returns_positive() -> None:
    # gpt-4o-mini is in LiteLLM's pricing map; any nonzero token count
    # should produce a positive cost. We don't assert exact $ to avoid
    # coupling tests to LiteLLM's pricing updates.
    cost = compute_cost("gpt-4o-mini", 1000, 500)
    assert cost > 0


def test_cost_openrouter_falls_back_to_native_prefix() -> None:
    # LiteLLM doesn't map openrouter/google/gemini-2.5-flash-lite directly,
    # but it does map gemini/gemini-2.5-flash-lite. The fallback in
    # _pricing_candidates retries with the native prefix and returns a
    # positive cost rather than 0.0.
    cost = compute_cost("openrouter/google/gemini-2.5-flash-lite", 1000, 500)
    assert cost > 0


def test_cost_skips_zero_priced_candidate(monkeypatch) -> None:
    # LiteLLM devolve (0, 0) — e não exceção — para o candidato OpenRouter.
    # Isso é "preço desconhecido": o cálculo deve seguir para o nativo.
    calls: list[str] = []

    def fake_cost_per_token(model, prompt_tokens, completion_tokens):
        calls.append(model)
        if model.startswith("gemini/"):
            return 1.5, 2.5
        return 0.0, 0.0

    monkeypatch.setattr(litellm, "cost_per_token", fake_cost_per_token)
    cost = compute_cost("openrouter/google/gemini-2.5-flash-lite", 1000, 500)
    assert cost == pytest.approx(4.0)
    assert calls == [
        "openrouter/google/gemini-2.5-flash-lite",
        "gemini/gemini-2.5-flash-lite",
    ]


def test_cost_all_candidates_zero_returns_zero(monkeypatch) -> None:
    monkeypatch.setattr(
        litellm,
        "cost_per_token",
        lambda model, prompt_tokens, completion_tokens: (0.0, 0.0),
    )
    assert compute_cost("openrouter/google/gemini-2.5-flash-lite", 1000, 500) == 0.0


def test_cost_truly_unknown_openrouter_model_returns_zero() -> None:
    # No fallback possible (no native pricing) → 0.0 silently.
    cost = compute_cost("openrouter/imaginary/model-x", 1000, 500)
    assert cost == 0.0


def test_cost_from_alias_pricing(monkeypatch) -> None:
    """Proxy aliases are priced from LLM_ALIAS_PRICING (US$ per 1M tokens)."""
    from app import config as config_mod

    monkeypatch.setenv("LLM_ALIAS_PRICING", '{"company-code": {"input": 0.30, "output": 1.20}}')
    config_mod.reset_settings_cache()
    try:
        assert compute_cost("company-code", 1_000_000, 500_000) == pytest.approx(0.90)
        assert compute_cost("company-reasoning", 1000, 1000) == 0.0  # not in table
    finally:
        config_mod.reset_settings_cache()


def test_cost_alias_pricing_invalid_json_is_ignored(monkeypatch) -> None:
    from app import config as config_mod

    monkeypatch.setenv("LLM_ALIAS_PRICING", "not-json")
    config_mod.reset_settings_cache()
    try:
        assert compute_cost("company-code", 1000, 1000) == 0.0
    finally:
        config_mod.reset_settings_cache()
