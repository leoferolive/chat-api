"""Server-Sent Events helpers."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from sse_starlette.sse import EventSourceResponse


def sse_payload(data: dict) -> str:
    """Serialise a dict into the SSE `data:` line body."""
    return json.dumps(data, ensure_ascii=False)


async def to_sse(events: AsyncIterator[dict]) -> AsyncIterator[dict]:
    """Adapter from event dicts → sse-starlette compatible chunks."""
    async for ev in events:
        yield {"data": sse_payload(ev)}


def build_response(generator: AsyncIterator[dict]) -> EventSourceResponse:
    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return EventSourceResponse(generator, headers=headers)
