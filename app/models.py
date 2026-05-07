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
    # max_length is read at validation time so tests can override via env
    # without freezing the constant at module import.
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
