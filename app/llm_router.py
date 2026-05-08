"""Streams completions from one of several LLM providers, with fallback.

This wraps `litellm.acompletion(stream=True)` and walks through a list of
configured models in order. The first provider that yields its first
token "wins" — subsequent failures while streaming are surfaced to the
caller (we don't restart mid-stream).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import litellm
import structlog

from .config import get_settings
from .metrics import (
    PROVIDER_ATTEMPTS_TOTAL,
    PROVIDER_FAILURES_TOTAL,
    TOKENS_TOTAL,
)

logger = structlog.get_logger(__name__)


def _provider_kwargs(model: str) -> tuple[str, dict]:
    """Translate `<prefix>/<model>` into LiteLLM call kwargs.

    Most providers Just Work via their own prefix (`gemini/`, `openrouter/`).
    For OpenAI-compatible endpoints we expose explicit prefixes that point
    LiteLLM at the right base url + api key.

    Returns the (model_string_passed_to_litellm, extra_kwargs).
    """
    settings = get_settings()
    if model.startswith("zai/"):
        bare = model[len("zai/") :]
        return f"openai/{bare}", {
            "api_base": settings.zai_base_url,
            "api_key": settings.zai_api_key or "",
        }
    return model, {}


@dataclass
class StreamResult:
    """Aggregated stream output (yielded incrementally to callers)."""
    model: str
    text: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    provider_attempts: list[str] = None  # type: ignore[assignment]


class AllProvidersFailed(RuntimeError):
    """Raised when every configured provider errored before producing a token."""


async def stream_completion(
    messages: list[dict],
    providers: list[str],
    *,
    temperature: float = 0.3,
    max_tokens: int | None = None,
) -> AsyncIterator[dict]:
    """Yield events as the LLM streams.

    Yielded shapes:
        {"type": "start",  "model": "<model>"}
        {"type": "token",  "value": "<text>"}
        {"type": "done",   "model": "<model>", "tokens": {...}, "attempts": [...]}

    On total failure raises `AllProvidersFailed`.
    """
    if not providers:
        raise AllProvidersFailed("no providers configured")

    if max_tokens is None:
        max_tokens = get_settings().llm_max_tokens

    attempts: list[str] = []
    last_err: Exception | None = None

    for model in providers:
        attempts.append(model)
        litellm_model, extra = _provider_kwargs(model)
        try:
            stream = await litellm.acompletion(
                model=litellm_model,
                messages=messages,
                stream=True,
                stream_options={"include_usage": True},
                temperature=temperature,
                max_tokens=max_tokens,
                **extra,
            )
        except Exception as exc:  # noqa: BLE001 — try next provider
            logger.warning("llm_open_failed", model=model, err=str(exc))
            PROVIDER_FAILURES_TOTAL.labels(model=model, phase="open").inc()
            PROVIDER_ATTEMPTS_TOTAL.labels(model=model, result="failure").inc()
            last_err = exc
            continue

        # Got a stream. From here, errors propagate (don't switch providers
        # mid-response — the user already has tokens flowing).
        text_buf: list[str] = []
        prompt_tokens = 0
        completion_tokens = 0
        first_chunk_ok = False
        try:
            yield {"type": "start", "model": model}
            async for chunk in stream:
                first_chunk_ok = True
                token, p_tok, c_tok = _extract_chunk(chunk)
                if p_tok:
                    prompt_tokens = p_tok
                if c_tok:
                    completion_tokens = c_tok
                if token:
                    text_buf.append(token)
                    yield {"type": "token", "value": token}
        except Exception as exc:  # noqa: BLE001
            if not first_chunk_ok:
                # Treat early-stream failure as still recoverable.
                logger.warning("llm_stream_open_errored", model=model, err=str(exc))
                PROVIDER_FAILURES_TOTAL.labels(model=model, phase="early").inc()
                PROVIDER_ATTEMPTS_TOTAL.labels(model=model, result="failure").inc()
                last_err = exc
                continue
            logger.error("llm_mid_stream_failure", model=model, err=str(exc))
            PROVIDER_FAILURES_TOTAL.labels(model=model, phase="mid").inc()
            PROVIDER_ATTEMPTS_TOTAL.labels(model=model, result="failure").inc()
            raise

        full_text = "".join(text_buf)
        if not completion_tokens:
            # Rough estimate when provider doesn't return usage.
            completion_tokens = max(1, len(full_text.split()))
        PROVIDER_ATTEMPTS_TOTAL.labels(model=model, result="success").inc()
        if prompt_tokens:
            TOKENS_TOTAL.labels(kind="prompt", model=model).inc(prompt_tokens)
        if completion_tokens:
            TOKENS_TOTAL.labels(kind="completion", model=model).inc(completion_tokens)
        yield {
            "type": "done",
            "model": model,
            "tokens": {
                "prompt": prompt_tokens,
                "completion": completion_tokens,
            },
            "attempts": attempts,
            "text": full_text,
        }
        return

    raise AllProvidersFailed(
        f"all providers failed: attempts={attempts} last_err={last_err!r}"
    )


async def complete_once(
    messages: list[dict],
    providers: list[str],
    *,
    temperature: float = 0.0,
    max_tokens: int = 200,
    response_format: dict | None = None,
) -> dict:
    """Run a single non-streaming completion with provider failover.

    Used for short classification-style calls (e.g. the LLM-based router).
    Returns ``{"text", "model", "tokens": {...}, "attempts": [...]}``.
    Raises ``AllProvidersFailed`` if no provider produces a response.
    """
    if not providers:
        raise AllProvidersFailed("no providers configured")

    attempts: list[str] = []
    last_err: Exception | None = None

    for model in providers:
        attempts.append(model)
        litellm_model, extra = _provider_kwargs(model)
        kwargs: dict = {
            "model": litellm_model,
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
            **extra,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        try:
            resp = await litellm.acompletion(**kwargs)
        except Exception as exc:  # noqa: BLE001 — try next provider
            logger.warning("llm_complete_once_failed", model=model, err=str(exc))
            PROVIDER_FAILURES_TOTAL.labels(model=model, phase="open").inc()
            PROVIDER_ATTEMPTS_TOTAL.labels(model=model, result="failure").inc()
            last_err = exc
            continue

        text, prompt_tokens, completion_tokens = _extract_response(resp)
        PROVIDER_ATTEMPTS_TOTAL.labels(model=model, result="success").inc()
        if prompt_tokens:
            TOKENS_TOTAL.labels(kind="prompt", model=model).inc(prompt_tokens)
        if completion_tokens:
            TOKENS_TOTAL.labels(kind="completion", model=model).inc(completion_tokens)
        return {
            "text": text,
            "model": model,
            "tokens": {
                "prompt": prompt_tokens,
                "completion": completion_tokens,
            },
            "attempts": attempts,
        }

    raise AllProvidersFailed(
        f"all providers failed: attempts={attempts} last_err={last_err!r}"
    )


def _extract_response(resp: object) -> tuple[str, int, int]:
    """Pull (text, prompt_tokens, completion_tokens) from a non-streamed reply."""
    text = ""
    prompt_tokens = 0
    completion_tokens = 0

    choices = getattr(resp, "choices", None)
    if choices is None and isinstance(resp, dict):
        choices = resp.get("choices")

    if choices:
        first = choices[0]
        message = getattr(first, "message", None)
        if message is None and isinstance(first, dict):
            message = first.get("message")
        if message is not None:
            content = getattr(message, "content", None)
            if content is None and isinstance(message, dict):
                content = message.get("content")
            if content:
                text = content

    usage = getattr(resp, "usage", None)
    if usage is None and isinstance(resp, dict):
        usage = resp.get("usage")
    if usage:
        prompt_tokens = (
            getattr(usage, "prompt_tokens", None)
            or (usage.get("prompt_tokens") if isinstance(usage, dict) else 0)
            or 0
        )
        completion_tokens = (
            getattr(usage, "completion_tokens", None)
            or (usage.get("completion_tokens") if isinstance(usage, dict) else 0)
            or 0
        )

    return text, prompt_tokens, completion_tokens


def _extract_chunk(chunk: object) -> tuple[str, int, int]:
    """Pull (text, prompt_tokens, completion_tokens) from a litellm chunk.

    LiteLLM normalises chunks to OpenAI-style dicts/objects. We try a few
    shapes defensively so unit tests can feed us plain dicts.
    """
    text = ""
    prompt_tokens = 0
    completion_tokens = 0

    # Object form (litellm.ModelResponse)
    choices = getattr(chunk, "choices", None)
    if choices is None and isinstance(chunk, dict):
        choices = chunk.get("choices")

    if choices:
        first = choices[0]
        delta = getattr(first, "delta", None)
        if delta is None and isinstance(first, dict):
            delta = first.get("delta") or first.get("message")
        if delta is not None:
            content = getattr(delta, "content", None)
            if content is None and isinstance(delta, dict):
                content = delta.get("content")
            if content:
                text = content

    usage = getattr(chunk, "usage", None)
    if usage is None and isinstance(chunk, dict):
        usage = chunk.get("usage")
    if usage:
        prompt_tokens = (
            getattr(usage, "prompt_tokens", None)
            or (usage.get("prompt_tokens") if isinstance(usage, dict) else 0)
            or 0
        )
        completion_tokens = (
            getattr(usage, "completion_tokens", None)
            or (usage.get("completion_tokens") if isinstance(usage, dict) else 0)
            or 0
        )

    return text, prompt_tokens, completion_tokens
