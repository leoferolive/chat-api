"""Shared JSON-salvage helpers for LLM responses that must be a JSON object.

Both the router (``app/router.py``) and the LLM judge (``app/judge/runner.py``)
ask a model for a strict ``{...}`` response and have to cope with the same
real-world failure mode: providers (Gemini in particular) routinely wrap the
JSON in a prose preamble or a markdown fence despite the system prompt. This
module holds the salvage/validation logic once so both callers share it
instead of one importing a private symbol from the other.
"""

from __future__ import annotations

import json


def extract_json_object(text: str) -> str:
    """Pull the first balanced ``{...}`` block out of ``text``.

    Models routinely wrap the JSON in a prose preamble ("Here is the JSON
    requested: {...}") or a markdown fence (```json\n{...}\n```) despite the
    system prompt — Gemini's actual prod behaviour. Salvaging it here means
    the first provider's answer is used instead of burning a failover
    round-trip (Gemini free-tier is only 5 req/min).

    The scanner is string-aware: ``{``/``}`` inside a JSON string literal
    (and backslash-escaped quotes) do not move the brace depth, so a valid
    object whose values contain braces isn't discarded.

    Raises ``ValueError`` if no brace-balanced object is found, so genuinely
    JSON-free responses still fall through to the next provider.
    """
    depth = 0
    start = -1
    in_str = False
    escaped = False
    for i, ch in enumerate(text):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
    raise ValueError("no JSON object found in response")


def parse_json_object(text: str) -> dict:
    """Strict validator for "the model must return a JSON object" contracts.

    `json.loads` alone happily accepts `"null"`, `"[]"`, `"42"` — none of
    which match a `{...}` contract. Treat anything that isn't a JSON object
    at the top level as a parse failure so a failover loop can try the next
    provider instead of silently accepting garbage.

    A clean ``{...}`` is parsed directly; anything else is salvaged via
    `extract_json_object` (prose preamble, markdown fence, trailing text)
    before giving up and raising.
    """
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        parsed = json.loads(extract_json_object(text))
    if not isinstance(parsed, dict):
        raise ValueError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed
