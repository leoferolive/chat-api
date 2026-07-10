"""Pydantic models for the chat API contract."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .config import get_settings


def _max_chars() -> int:
    return get_settings().max_user_message_chars


def _max_messages() -> int:
    return get_settings().max_messages_per_request


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    # NOTE: `Field(...)` evaluates its arguments once, at class-definition
    # time (module import) — Pydantic v2 does NOT re-call `_max_chars()` on
    # every validation. The limit below is `get_settings().max_user_message_chars`
    # frozen at the moment `app.models` is first imported; setting the env
    # var afterwards has no effect on it. No test currently overrides this
    # limit via env, so that's fine as-is — if per-test overrides are ever
    # needed, switch to a `field_validator` that calls `get_settings()` at
    # validation time instead.
    content: str = Field(..., min_length=1, max_length=_max_chars())


class ChatRequest(BaseModel):
    # UUID-shaped sessionId only (the frontend uses crypto.randomUUID).
    # Reject arbitrary strings — they show up in logs and the DB and
    # we don't want injection or XSS-via-log surfaces.
    sessionId: str = Field(
        ...,
        pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
    )
    messages: list[ChatMessage] = Field(..., min_length=1, max_length=_max_messages())
    lang: Literal["pt", "en"] = "pt"
    turnstileToken: str | None = Field(default=None, max_length=4096)
    # Optional self-declared display name. Trimmed/normalised server-side
    # before persistence; the Prometheus label is derived (hashed) so the
    # raw value never reaches the metric.
    userName: str | None = Field(
        default=None,
        max_length=40,
        pattern=r"^[\w \-'.À-ɏ]+$",
    )


class ChatTokenChunk(BaseModel):
    type: Literal["token"] = "token"
    value: str


class ChatDoneChunk(BaseModel):
    type: Literal["done"] = "done"
    model: str
    tokens: dict[str, int]


class ChatErrorChunk(BaseModel):
    type: Literal["error"] = "error"
    message: str


class WikiPage(BaseModel):
    path: str
    title: str
    summary: str = ""
    tags: list[str] = Field(default_factory=list)
    content: str = ""
    score: float = 0.0
