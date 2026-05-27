"""Per-turn USD cost calculation.

Delegates to ``litellm.cost_per_token`` which carries an embedded pricing map
covering most upstream providers. For custom/self-hosted models (e.g. Z.AI via
the openai-compatible endpoint) LiteLLM has no pricing and returns 0 — we
surface that as a debug log and a 0.0 cost rather than raising.
"""

from __future__ import annotations

import litellm
import structlog

logger = structlog.get_logger(__name__)


def provider_of(model: str) -> str:
    """Return the LiteLLM provider prefix from a model string.

    ``openrouter/google/gemini-2.5-flash-lite`` -> ``openrouter``
    ``gemini/gemini-2.5-flash``                 -> ``gemini``
    ``zai/glm-4.6``                             -> ``zai``
    ``gpt-4o``                                  -> ``openai`` (default)
    """
    if "/" not in model:
        return "openai"
    return model.split("/", 1)[0]


def _pricing_candidates(model: str) -> list[str]:
    """Yield candidate model strings to ask LiteLLM about.

    LiteLLM's pricing map covers each provider's *native* model string but
    not the OpenRouter-routed variant, even when the underlying model is
    the same Gemini/Anthropic/OpenAI weights. So when we're routing via
    OpenRouter, also try the equivalent native prefix as a fallback.
    Best-effort: OpenRouter may add a small markup (~5–15%); the dashboard
    is intended for order-of-magnitude FinOps, not invoice reconciliation.
    """
    candidates = [model]
    if model.startswith("openrouter/"):
        rest = model[len("openrouter/") :]
        # openrouter/google/<m> → also try gemini/<m> (LiteLLM uses gemini/
        # as the Google API prefix, not google/).
        if rest.startswith("google/"):
            candidates.append("gemini/" + rest[len("google/") :])
        # openrouter/anthropic/<m> → also try anthropic/<m>.
        # openrouter/openai/<m>    → also try openai/<m> (or just <m>).
        # General fallback: try the rest unchanged.
        candidates.append(rest)
    return candidates


def compute_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> float:
    """USD cost for a single LLM call. Returns 0.0 if pricing is unknown."""
    if not prompt_tokens and not completion_tokens:
        return 0.0
    last_err: Exception | None = None
    for candidate in _pricing_candidates(model):
        try:
            prompt_cost, completion_cost = litellm.cost_per_token(
                model=candidate,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
        except Exception as exc:  # noqa: BLE001 — pricing is best-effort
            last_err = exc
            continue
        return float((prompt_cost or 0.0) + (completion_cost or 0.0))
    logger.debug("cost_lookup_failed", model=model, err=str(last_err))
    return 0.0
