"""Tests for per-call USD cost calculation."""

from __future__ import annotations

from app.cost import compute_cost, provider_of


def test_provider_of_with_prefix() -> None:
    assert provider_of("openrouter/google/gemini-2.5-flash-lite") == "openrouter"
    assert provider_of("gemini/gemini-2.5-flash") == "gemini"
    assert provider_of("zai/glm-4.6") == "zai"


def test_provider_of_without_prefix() -> None:
    assert provider_of("gpt-4o") == "openai"


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


def test_cost_truly_unknown_openrouter_model_returns_zero() -> None:
    # No fallback possible (no native pricing) → 0.0 silently.
    cost = compute_cost("openrouter/imaginary/model-x", 1000, 500)
    assert cost == 0.0
