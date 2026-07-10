"""Tests for the shared JSON-salvage helpers (app/json_utils.py).

The behaviour here used to live as private helpers on ``app.router``
(``_extract_json_object`` / ``_parse_router_json``); ``tests/test_router.py``
still exercises the full salvage/validation contract through those
router-level aliases. This module covers the shared implementation
directly, plus the fact that ``app.judge.runner`` consumes it too.
"""

from __future__ import annotations

import pytest

from app.json_utils import extract_json_object, parse_json_object


class TestExtractJsonObject:
    def test_clean_object(self) -> None:
        assert extract_json_object('{"score": 5}') == '{"score": 5}'

    def test_preamble_then_object(self) -> None:
        text = 'Here is the JSON requested: {"score": 5}'
        assert extract_json_object(text) == '{"score": 5}'

    def test_no_object_raises(self) -> None:
        with pytest.raises(ValueError):
            extract_json_object("nothing here")


class TestParseJsonObject:
    def test_parses_clean_object(self) -> None:
        assert parse_json_object('{"score": 5}') == {"score": 5}

    def test_salvages_from_preamble(self) -> None:
        assert parse_json_object('ok: {"score": 5}') == {"score": 5}

    def test_non_object_json_rejected(self) -> None:
        for bogus in ("null", "[]", "42", '"just a string"'):
            with pytest.raises((ValueError, TypeError)):
                parse_json_object(bogus)

    def test_no_json_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            parse_json_object("no json in here")


def test_judge_runner_uses_shared_parser_not_private_router_symbol() -> None:
    """Guards the point of the refactor: judge/runner.py must depend on the
    shared public module, not reach into app.router's private namespace."""
    import ast
    import inspect

    from app.judge import runner

    source = inspect.getsource(runner)
    tree = ast.parse(source)
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "_parse_router_json" not in imported_names
    assert "parse_json_object" in imported_names
