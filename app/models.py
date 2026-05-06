"""Pydantic models for the chat API contract."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str


class ChatRequest(BaseModel):
    sessionId: str = Field(..., min_length=1, max_length=128)
    messages: list[ChatMessage] = Field(..., min_length=1)
    lang: Literal["pt", "en"] = "pt"
    turnstileToken: str | None = None


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
