"""Normalize user-supplied display names into low-cardinality metric labels.

The raw name is what the user typed (kept in the DB for product-level views);
the label is what we export to Prometheus. The label is shaped as
``primeiro#hash4`` purely to *disambiguate* two visitors who share the same
first name in the dashboard — it is **not** a privacy mechanism (the raw
name lives in ``sessions.user_name`` regardless, and a 16-bit suffix is
trivially invertible if the salt leaks).

Cardinality is double-bounded:
1. The Pydantic regex on ``ChatRequest.userName`` blocks anything outside a
   conservative Unicode letter/space/punct alphabet.
2. ``cap_user_label`` enforces a per-process upper bound (``LABEL_CAP``)
   on distinct labels emitted to Prometheus. Beyond the cap, all new
   labels collapse to ``OVERFLOW_LABEL`` so a malicious client cannot
   inflate the TSDB by rotating ``userName`` per request.
"""

from __future__ import annotations

import hashlib
import threading
import unicodedata

ANONYMOUS_LABEL = "anonymous"
OVERFLOW_LABEL = "other"

# Per-process cap on distinct user labels exposed to Prometheus. Picked so
# that even with the worst-case fan-out from the regex, a single pod can't
# stamp more than O(LABEL_CAP) series for the chat_api_chats_total counter.
LABEL_CAP = 500

_seen_lock = threading.Lock()
_seen_labels: set[str] = {ANONYMOUS_LABEL, OVERFLOW_LABEL}


def reset_label_bucket() -> None:
    """Clear the seen-labels set. Tests use this between cases."""
    with _seen_lock:
        _seen_labels.clear()
        _seen_labels.update({ANONYMOUS_LABEL, OVERFLOW_LABEL})


def _strip_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", value)
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def _is_safe_name_char(ch: str) -> bool:
    """Whitelist by Unicode category to keep label content predictable.

    Allows letters and decimal digits only. Excludes combining marks (post
    accent strip), zero-width joiners/spaces, RTL/LTR marks, format chars
    and homoglyph categories that would let a client amplify cardinality
    or spoof other users in dashboards.
    """
    if not ch:
        return False
    cat = unicodedata.category(ch)
    return cat[0] == "L" or cat == "Nd"


def normalize_user_label(raw: str | None, *, salt: str) -> str:
    if raw is None:
        return ANONYMOUS_LABEL
    cleaned = raw.strip()
    if not cleaned:
        return ANONYMOUS_LABEL

    full_norm = _strip_accents(cleaned).lower()
    # Whitelist-filter: drop anything that isn't a letter/digit/space.
    # Mapping disallowed chars to "" (instead of space) is deliberate —
    # otherwise a client could insert ZWJ/RTL marks inside a name to
    # split tokens and amplify the label cardinality.
    safe_chars = [
        ch if _is_safe_name_char(ch) else (" " if ch.isspace() else "")
        for ch in full_norm
    ]
    safe_str = "".join(safe_chars).strip()
    tokens = [t for t in safe_str.split() if t]
    if not tokens:
        return ANONYMOUS_LABEL
    first = tokens[0][:24]
    # Hash the *post-filter* string so two visually identical names that
    # differ only in zero-width / format chars collapse to the same label.
    canonical = " ".join(tokens)
    digest = hashlib.sha256(f"{salt}:{canonical}".encode()).hexdigest()[:4]
    return f"{first}#{digest}"


def cap_user_label(label: str) -> str:
    """Return the label as-is if seen before or under the cap; else overflow.

    Bounds the per-process Prometheus series count for the ``user`` label
    on ``chat_api_chats_total``. The seen set is process-local — multi-pod
    deployments converge to slightly different views (acceptable: rankings
    are dashboard-level, not per-pod).
    """
    if label in (ANONYMOUS_LABEL, OVERFLOW_LABEL):
        return label
    with _seen_lock:
        if label in _seen_labels:
            return label
        if len(_seen_labels) >= LABEL_CAP + 2:  # +2 for the reserved pair
            return OVERFLOW_LABEL
        _seen_labels.add(label)
        return label


def sanitize_user_name(raw: str | None) -> str | None:
    """Return a trimmed, length-capped raw name for DB persistence, or None."""
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    return cleaned[:40]
