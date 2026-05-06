"""Streams completions from one of several LLM providers, with fallback.

This wraps `litellm.acompletion(stream=True)` and walks through a list of
configured models in order. The first provider that yields its first
token "wins" — subsequent failures while streaming are surfaced to the
caller (we don't restart mid-stream).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

import litellm

logger = logging.getLogger(__name__)


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
    max_tokens: int = 1024,
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

    attempts: list[str] = []
    last_err: Exception | None = None

    for model in providers:
        attempts.append(model)
        try:
            stream = await litellm.acompletion(
                model=model,
                messages=messages,
                stream=True,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as exc:  # noqa: BLE001 — try next provider
            logger.warning("llm_router: failed to open stream", extra={"model": model, "err": str(exc)})
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
                logger.warning(
                    "llm_router: stream errored before any token",
                    extra={"model": model, "err": str(exc)},
                )
                last_err = exc
                continue
            logger.error("llm_router: mid-stream failure", extra={"model": model, "err": str(exc)})
            raise

        full_text = "".join(text_buf)
        if not completion_tokens:
            # Rough estimate when provider doesn't return usage.
            completion_tokens = max(1, len(full_text.split()))
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
