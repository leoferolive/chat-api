"""Tests for request_id middleware and decision_hash labels."""

from __future__ import annotations

import pytest

from app.router import decision_hash


def test_decision_hash_is_order_insensitive() -> None:
    a = decision_hash(["b.md", "a.md", "c.md"])
    b = decision_hash(["c.md", "a.md", "b.md"])
    assert a == b
    assert len(a) == 8


def test_decision_hash_empty() -> None:
    assert decision_hash([]) == "empty"


def test_decision_hash_distinct_for_different_sets() -> None:
    assert decision_hash(["a.md"]) != decision_hash(["b.md"])


@pytest.mark.asyncio
async def test_request_id_is_echoed_in_response_header(client) -> None:
    resp = await client.get("/healthz", headers={"x-request-id": "client-supplied-42"})
    assert resp.status_code == 200
    assert resp.headers.get("X-Request-Id") == "client-supplied-42"


@pytest.mark.asyncio
async def test_request_id_generated_when_absent(client) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    rid = resp.headers.get("X-Request-Id")
    assert rid is not None
    assert len(rid) == 12
