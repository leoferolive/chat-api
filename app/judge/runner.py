"""Async batch runner for the LLM judge."""

from __future__ import annotations

import json

import litellm
import structlog

from ..db import Database
from ..router import _parse_router_json
from .prompts import CRITERIA, judge_prompt

logger = structlog.get_logger(__name__)

# Route the judge through OpenRouter instead of the Google API direct.
# The Gemini free tier (5 RPM) was throttling the batch hard — only ~34
# scores landed in 11h. OpenRouter has its own pricing on this model but
# no per-minute cap at our scale.
DEFAULT_JUDGE_MODEL = "openrouter/google/gemini-2.5-flash-lite"


def _coerce_score(parsed: dict) -> tuple[float, str]:
    raw_score = parsed.get("score")
    try:
        score = float(raw_score)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"judge returned non-numeric score: {raw_score!r}") from exc
    if score < 0:
        score = 0.0
    if score > 5:
        score = 5.0
    reason = str(parsed.get("reason", "") or "")[:500]
    return score, reason


async def _score_one(
    *,
    criterion: str,
    question: str,
    answer: str,
    judge_model: str,
) -> tuple[float, str]:
    prompt = judge_prompt(criterion=criterion, question=question, answer=answer)
    resp = await litellm.acompletion(
        model=judge_model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
        max_tokens=200,
        response_format={"type": "json_object"},
    )
    # Re-use the router's defensive JSON extractor — judges suffer the same
    # "Here is the JSON requested: {...}" preamble problem in prod.
    text = resp.choices[0].message.content or ""
    try:
        parsed = _parse_router_json(text)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"judge JSON parse failed: {exc}") from exc
    return _coerce_score(parsed)


async def evaluate_batch(
    db: Database,
    *,
    limit: int = 50,
    judge_model: str = DEFAULT_JUDGE_MODEL,
) -> dict:
    """Score up to ``limit`` unevaluated assistant turns. Returns a summary."""
    turns = await db.fetch_unscored_assistant_turns(list(CRITERIA), limit=limit)
    scored = 0
    failed = 0
    for turn in turns:
        # Only the criteria that still lack a score for this turn — a
        # previous batch may have succeeded on some and failed on others.
        for criterion in turn.get("missing_criteria", list(CRITERIA)):
            try:
                score, reason = await _score_one(
                    criterion=criterion,
                    question=turn["question"] or "",
                    answer=turn["answer"],
                    judge_model=judge_model,
                )
            except Exception as exc:  # noqa: BLE001 — never abort the batch
                logger.warning(
                    "judge_score_failed",
                    message_id=turn["assistant_id"],
                    criterion=criterion,
                    err=str(exc),
                )
                failed += 1
                continue
            await db.save_judge_score(
                message_id=turn["assistant_id"],
                criterion=criterion,
                score=score,
                reason=reason,
                judge_model=judge_model,
            )
            scored += 1
    logger.info(
        "judge_batch_done",
        turns=len(turns),
        scored=scored,
        failed=failed,
        judge_model=judge_model,
    )
    return {"turns": len(turns), "scored": scored, "failed": failed}
