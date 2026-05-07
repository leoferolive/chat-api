"""Tests for app.user_identity normalization."""

from __future__ import annotations

import re

from app.user_identity import (
    ANONYMOUS_LABEL,
    normalize_user_label,
    sanitize_user_name,
)

LABEL_RE = re.compile(r"^[a-z0-9_]{1,24}#[0-9a-f]{4}$")


def test_normalize_anonymous_for_none_or_empty() -> None:
    assert normalize_user_label(None, salt="s") == ANONYMOUS_LABEL
    assert normalize_user_label("", salt="s") == ANONYMOUS_LABEL
    assert normalize_user_label("   ", salt="s") == ANONYMOUS_LABEL
    assert normalize_user_label("---", salt="s") == ANONYMOUS_LABEL


def test_normalize_strips_accents_and_lowercases() -> None:
    label = normalize_user_label("Léo Ferreira", salt="s")
    assert LABEL_RE.match(label), label
    assert label.startswith("leo#")


def test_normalize_takes_first_token() -> None:
    label = normalize_user_label("Maria da Silva", salt="s")
    assert label.startswith("maria#")


def test_normalize_is_deterministic_with_same_salt() -> None:
    a = normalize_user_label("Léo Ferreira", salt="salt-a")
    b = normalize_user_label("Léo Ferreira", salt="salt-a")
    assert a == b


def test_normalize_changes_with_different_salt() -> None:
    a = normalize_user_label("Léo Ferreira", salt="salt-a")
    b = normalize_user_label("Léo Ferreira", salt="salt-b")
    assert a != b
    assert a.split("#")[0] == b.split("#")[0]  # same first token


def test_normalize_disambiguates_homonyms_by_full_name() -> None:
    # Same first name, different surname → different hash bucket.
    a = normalize_user_label("Léo Ferreira", salt="s")
    b = normalize_user_label("Léo Souza", salt="s")
    assert a != b
    assert a.startswith("leo#") and b.startswith("leo#")


def test_normalize_truncates_very_long_first_name() -> None:
    label = normalize_user_label("a" * 200, salt="s")
    first, _ = label.split("#")
    assert len(first) <= 24


def test_sanitize_user_name_trims_and_caps() -> None:
    assert sanitize_user_name(None) is None
    assert sanitize_user_name("") is None
    assert sanitize_user_name("  ") is None
    assert sanitize_user_name("  Léo  ") == "Léo"
    assert sanitize_user_name("x" * 100) == "x" * 40
