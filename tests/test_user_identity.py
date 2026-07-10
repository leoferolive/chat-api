"""Tests for app.user_identity normalization."""

from __future__ import annotations

import re

import pytest

from app.user_identity import (
    ANONYMOUS_LABEL,
    LABEL_CAP,
    OVERFLOW_LABEL,
    cap_user_label,
    normalize_user_label,
    reset_label_bucket,
    sanitize_user_name,
)


@pytest.fixture(autouse=True)
def _reset_bucket():
    reset_label_bucket()
    yield
    reset_label_bucket()


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


def test_normalize_rejects_zero_width_and_format_chars() -> None:
    # Zero-width joiner (U+200D) and RTL mark (U+200F) must not survive
    # into the first-token portion — otherwise a client could amplify
    # cardinality by inserting invisible chars between the same name.
    base = normalize_user_label("Leo", salt="s")
    weird = normalize_user_label("Le‍o‏", salt="s")
    # The first-token portion (everything before #) must be the same as
    # the unadulterated "leo" form: invisible chars are filtered out.
    assert weird.split("#")[0] == base.split("#")[0] == "leo"


def test_cap_label_recycles_known_labels() -> None:
    a = cap_user_label("leo#aaaa")
    b = cap_user_label("leo#aaaa")
    assert a == b == "leo#aaaa"


def test_cap_label_overflows_to_other_when_cap_exhausted() -> None:
    # Fill the bucket up to the cap with synthetic labels.
    for i in range(LABEL_CAP):
        cap_user_label(f"u{i}#dead")
    overflow = cap_user_label("freshname#beef")
    assert overflow == OVERFLOW_LABEL


def test_cap_label_passes_anonymous_through_without_consuming_quota() -> None:
    for _ in range(10_000):
        assert cap_user_label(ANONYMOUS_LABEL) == ANONYMOUS_LABEL
    # Sanity: real labels still allocatable.
    assert cap_user_label("real#1234") == "real#1234"


def test_sanitize_user_name_trims_and_caps() -> None:
    assert sanitize_user_name(None) is None
    assert sanitize_user_name("") is None
    assert sanitize_user_name("  ") is None
    assert sanitize_user_name("  Léo  ") == "Léo"
    assert sanitize_user_name("x" * 100) == "x" * 40
