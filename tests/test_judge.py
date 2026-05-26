"""Tests for the LLM-as-Judge runner + /metrics-judge endpoint."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.db import Database
from app.judge.prompts import CRITERIA, judge_prompt
from app.judge.runner import evaluate_batch

# --- prompts ----------------------------------------------------------------


def test_judge_prompt_includes_question_and_answer() -> None:
    out = judge_prompt(criterion="relevance", question="Quem é Léo?", answer="Eu sou Léo.")
    assert "Quem é Léo?" in out
    assert "Eu sou Léo." in out
    assert "relevance" in out


def test_judge_prompt_unknown_criterion_raises() -> None:
    with pytest.raises(ValueError):
        judge_prompt(criterion="taste", question="q", answer="a")


def test_all_criteria_have_rubric() -> None:
    for c in CRITERIA:
        prompt = judge_prompt(criterion=c, question="q", answer="a")
        assert "Scoring rubric" in prompt


# --- runner -----------------------------------------------------------------


class _FakeLiteLLM:
    """Stand-in for ``litellm.acompletion`` capturing inputs and scripting JSON."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        # criterion -> JSON body to return.
        self.responses: dict[str, str] = {
            "relevance":    '{"score": 5, "reason": "spot on"}',
            "groundedness": '{"score": 4, "reason": "minor speculation"}',
            "safety":       '{"score": 5, "reason": "no harmful claims"}',
        }
        # If set, any criterion in this set raises instead of responding.
        self.raise_for: set[str] = set()

    async def __call__(self, *, model, messages, **kwargs):
        prompt = messages[0]["content"]
        criterion = next((c for c in CRITERIA if f"**{c}**" in prompt), "relevance")
        self.calls.append({"model": model, "criterion": criterion})
        if criterion in self.raise_for:
            raise RuntimeError(f"forced failure for {criterion}")
        body = self.responses[criterion]
        return type(
            "Resp",
            (),
            {"choices": [type("C", (), {"message": type("M", (), {"content": body})()})()]},
        )()


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> _FakeLiteLLM:
    fake = _FakeLiteLLM()
    monkeypatch.setattr("app.judge.runner.litellm.acompletion", fake)
    return fake


@pytest.fixture
async def seeded_db(tmp_path: Path):
    db = Database(tmp_path / "judge.sqlite")
    await db.connect()
    await db.upsert_session("s1", "ip-x", "pt")
    await db.save_turn(session_id="s1", role="user", content="Quem é Léo?")
    await db.save_turn(
        session_id="s1",
        role="assistant",
        content="Léo trabalhou na Wiley.",
        model="gemini/gemini-2.5-flash",
        prompt_tokens=10,
        completion_tokens=8,
        cost_usd=0.0001,
    )
    yield db
    await db.close()


@pytest.mark.asyncio
async def test_evaluate_batch_scores_all_criteria(seeded_db, fake_llm) -> None:
    summary = await evaluate_batch(seeded_db, limit=10, judge_model="judge/test")
    assert summary == {"turns": 1, "scored": 3, "failed": 0}
    # Three rows inserted, one per criterion.
    rows = await seeded_db.judge_score_aggregates(since_ts=0)
    criteria_seen = {r["criterion"] for r in rows}
    assert criteria_seen == set(CRITERIA)


@pytest.mark.asyncio
async def test_evaluate_batch_skips_already_scored(seeded_db, fake_llm) -> None:
    await evaluate_batch(seeded_db, limit=10, judge_model="judge/test")
    fake_llm.calls.clear()
    summary = await evaluate_batch(seeded_db, limit=10, judge_model="judge/test")
    assert summary["turns"] == 0
    assert fake_llm.calls == []


@pytest.mark.asyncio
async def test_evaluate_batch_partial_failure_counts(seeded_db, fake_llm) -> None:
    fake_llm.raise_for = {"safety"}
    summary = await evaluate_batch(seeded_db, limit=10, judge_model="judge/test")
    assert summary["scored"] == 2
    assert summary["failed"] == 1


@pytest.mark.asyncio
async def test_evaluate_batch_only_retries_missing_criteria(seeded_db, fake_llm) -> None:
    # First batch: safety fails — 2 criteria saved, 1 missing.
    fake_llm.raise_for = {"safety"}
    await evaluate_batch(seeded_db, limit=10, judge_model="judge/test")

    # Second batch must call ONLY safety, not re-bill the already-scored ones.
    fake_llm.raise_for = set()
    fake_llm.calls.clear()
    summary = await evaluate_batch(seeded_db, limit=10, judge_model="judge/test")

    called = [c["criterion"] for c in fake_llm.calls]
    assert called == ["safety"], f"expected only safety, got {called}"
    assert summary["scored"] == 1
    assert summary["failed"] == 0


@pytest.mark.asyncio
async def test_score_out_of_range_is_clamped(seeded_db, fake_llm) -> None:
    fake_llm.responses["relevance"] = '{"score": 12, "reason": "huge"}'
    fake_llm.responses["safety"]    = '{"score": -3, "reason": "neg"}'
    await evaluate_batch(seeded_db, limit=10, judge_model="judge/test")
    rows = await seeded_db.judge_score_aggregates(since_ts=0)
    by_crit = {r["criterion"]: r["avg_score"] for r in rows}
    assert by_crit["relevance"] == 5.0
    assert by_crit["safety"] == 0.0


# --- endpoint ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_metrics_judge_endpoint_aggregates(client) -> None:
    db = client.app.state.db  # type: ignore[attr-defined]
    await db.upsert_session("smj", "ip-x", "pt")
    await db.save_turn(session_id="smj", role="user", content="oi")
    await db.save_turn(
        session_id="smj",
        role="assistant",
        content="oi de volta",
        model="gemini/gemini-2.5-flash",
    )
    # Locate the message id we just inserted.
    async with db._conn.execute(
        "SELECT id FROM messages WHERE session_id=? AND role='assistant'", ("smj",)
    ) as cur:
        row = await cur.fetchone()
    msg_id = row[0]
    for crit, score in (("relevance", 5.0), ("groundedness", 3.0), ("safety", 2.0)):
        await db.save_judge_score(
            message_id=msg_id,
            criterion=crit,
            score=score,
            reason="ok",
            judge_model="judge/test",
        )

    resp = await client.get("/metrics-judge", headers={"host": "127.0.0.1"})
    assert resp.status_code == 200
    body = resp.text
    assert "chat_api_judge_score_avg" in body
    assert "chat_api_judge_evaluations_total" in body
    assert "chat_api_judge_verdicts_total" in body
    # All three verdict buckets appeared (pass / warn / fail).
    assert 'verdict="pass"' in body
    assert 'verdict="warn"' in body
    assert 'verdict="fail"' in body


@pytest.mark.asyncio
async def test_metrics_judge_endpoint_external_host_404(client) -> None:
    resp = await client.get(
        "/metrics-judge", headers={"host": "chat-dev.leoferolive.com.br"}
    )
    assert resp.status_code == 404
