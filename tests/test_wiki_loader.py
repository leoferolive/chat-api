"""Tests for the wiki loader."""

from __future__ import annotations

from pathlib import Path

from app.wiki_loader import WikiLoader


def test_loads_index_entries(temp_wiki: Path) -> None:
    loader = WikiLoader(temp_wiki, poll_seconds=0)
    pages = loader.all_pages()
    paths = {p.path for p in pages}
    assert "entities/wiley.md" in paths
    assert "skills/backend.md" in paths


def test_parses_summary_and_tags(temp_wiki: Path) -> None:
    loader = WikiLoader(temp_wiki, poll_seconds=0)
    pages = {p.path: p for p in loader.all_pages()}
    wiley = pages["entities/wiley.md"]
    assert "Wiley" in wiley.title
    assert "wiley" in wiley.tags
    assert "backend" in wiley.tags
    assert "Wiley" in wiley.content


def test_reload_when_index_changes(tmp_path: Path) -> None:
    wiki = tmp_path / "w"
    wiki.mkdir()
    (wiki / "index.md").write_text(
        "- [Wiley](entities/wiley.md) — first [wiley]\n", encoding="utf-8"
    )
    (wiki / "entities").mkdir()
    (wiki / "entities" / "wiley.md").write_text("first body", encoding="utf-8")

    loader = WikiLoader(wiki, poll_seconds=0)
    snap1 = loader.load(force=True)
    assert "first" in snap1.pages["entities/wiley.md"].summary

    (wiki / "index.md").write_text(
        "- [Wiley](entities/wiley.md) — updated [wiley]\n", encoding="utf-8"
    )
    snap2 = loader.load(force=True)
    assert "updated" in snap2.pages["entities/wiley.md"].summary


def test_missing_dir_returns_empty(tmp_path: Path) -> None:
    loader = WikiLoader(tmp_path / "does-not-exist", poll_seconds=0)
    assert loader.all_pages() == []


def test_fallback_when_no_index(tmp_path: Path) -> None:
    wiki = tmp_path / "w"
    wiki.mkdir()
    (wiki / "loose.md").write_text("# loose\nbody", encoding="utf-8")
    loader = WikiLoader(wiki, poll_seconds=0)
    pages = loader.all_pages()
    assert any(p.path == "loose.md" for p in pages)
