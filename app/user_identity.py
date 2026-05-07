"""Normalize user-supplied display names into low-cardinality metric labels.

The raw name is what the user typed (kept in the DB for product-level views);
the label is what we export to Prometheus. The label is shaped as
``primeiro#hash4`` so two visitors named "Léo" don't collapse into the same
ranking row, and bumping a per-deploy salt rotates the bucket if needed.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

ANONYMOUS_LABEL = "anonymous"
_FIRST_TOKEN_RE = re.compile(r"[^\w]+", re.UNICODE)


def _strip_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", value)
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def normalize_user_label(raw: str | None, *, salt: str) -> str:
    if raw is None:
        return ANONYMOUS_LABEL
    cleaned = raw.strip()
    if not cleaned:
        return ANONYMOUS_LABEL

    full_norm = _strip_accents(cleaned).lower()
    tokens = [t for t in _FIRST_TOKEN_RE.split(full_norm) if t]
    if not tokens:
        return ANONYMOUS_LABEL
    first = tokens[0][:24]
    digest = hashlib.sha256(f"{salt}:{full_norm}".encode()).hexdigest()[:4]
    return f"{first}#{digest}"


def sanitize_user_name(raw: str | None) -> str | None:
    """Return a trimmed, length-capped raw name for DB persistence, or None."""
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    return cleaned[:40]
