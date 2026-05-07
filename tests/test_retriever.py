"""Tests for the keyword retriever."""

from __future__ import annotations

from pathlib import Path

from app.retriever import Retriever
from app.wiki_loader import WikiLoader


def test_picks_relevant_page_by_title(temp_wiki: Path) -> None:
    retr = Retriever(WikiLoader(temp_wiki, poll_seconds=0))
    pages = retr.pick("Como foi o trabalho na Wiley?", lang="pt", top_n=2)
    assert pages
    assert pages[0].path == "entities/wiley.md"


def test_picks_by_tag(temp_wiki: Path) -> None:
    retr = Retriever(WikiLoader(temp_wiki, poll_seconds=0))
    pages = retr.pick("python fastapi", lang="en", top_n=2)
    assert pages
    assert pages[0].path == "skills/backend.md"


def test_falls_back_when_no_match(temp_wiki: Path) -> None:
    retr = Retriever(WikiLoader(temp_wiki, poll_seconds=0))
    pages = retr.pick("zzzzz noise terms only", lang="pt", top_n=5)
    # Falls back to alphabetical pages so the LLM still has grounding.
    assert pages
    assert all(p.score == 0.0 for p in pages)


def test_empty_wiki_returns_empty(tmp_path: Path) -> None:
    loader = WikiLoader(tmp_path / "missing", poll_seconds=0)
    retr = Retriever(loader)
    assert retr.pick("anything", lang="pt") == []


def test_does_not_pick_files_outside_wiki(temp_wiki: Path) -> None:
    """Regression: prod logs showed ``AGENTS.md`` and ``raw/README.md``
    being returned alongside real wiki pages. The retriever must scope
    to ``<WIKI_DIR>/wiki/`` only.
    """
    retr = Retriever(WikiLoader(temp_wiki, poll_seconds=0))
    # Use a query that matches plausible noise content as well as wiki
    # content, so the retriever has to actively filter.
    pages = retr.pick("README agents wiley backend", lang="pt", top_n=10)
    paths = {p.path for p in pages}
    assert "AGENTS.md" not in paths
    assert "README.md" not in paths
    assert "raw/README.md" not in paths
    # And the real wiki pages are still reachable.
    assert "entities/wiley.md" in paths or "skills/backend.md" in paths
