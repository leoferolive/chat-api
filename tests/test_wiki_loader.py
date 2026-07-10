"""Tests for the wiki loader."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

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


def test_ignores_files_outside_wiki_subtree(temp_wiki: Path) -> None:
    """Noise files (AGENTS.md, raw/README.md, ...) at the WIKI_DIR root
    must never be returned as wiki pages.

    This mirrors the prod layout where the init container clones the
    full ``leoferolive-wiki`` repo into the volume. Only files under
    ``<WIKI_DIR>/wiki/`` are real pages.
    """
    loader = WikiLoader(temp_wiki, poll_seconds=0)
    paths = {p.path for p in loader.all_pages()}

    # Real wiki pages are present...
    assert "entities/wiley.md" in paths
    assert "skills/backend.md" in paths

    # ...and the noise files are absent (no matter how they might be spelled).
    forbidden = {
        "AGENTS.md",
        "README.md",
        "raw/README.md",
        "../AGENTS.md",
        "wiki/AGENTS.md",
    }
    assert paths.isdisjoint(forbidden), f"retriever leaked non-wiki files: {paths & forbidden}"


def test_fallback_scopes_to_wiki_subdir(tmp_path: Path) -> None:
    """When ``<WIKI_DIR>/wiki/`` exists *without* an ``index.md``, the
    fallback rglob must still stay inside that subtree and ignore noise
    siblings at the volume root.
    """
    repo = tmp_path / "vol"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("noise", encoding="utf-8")
    (repo / "raw").mkdir()
    (repo / "raw" / "README.md").write_text("noise", encoding="utf-8")
    wiki = repo / "wiki"
    wiki.mkdir()
    (wiki / "index.md").write_text(
        "- [Wiley](entities/wiley.md) — body [wiley]\n", encoding="utf-8"
    )
    (wiki / "entities").mkdir()
    (wiki / "entities" / "wiley.md").write_text("# Wiley", encoding="utf-8")

    loader = WikiLoader(repo, poll_seconds=0)
    paths = {p.path for p in loader.all_pages()}
    assert "entities/wiley.md" in paths
    assert "AGENTS.md" not in paths
    assert "raw/README.md" not in paths


# --- async accessors (event-loop-safe reload) --------------------------


@pytest.mark.asyncio
async def test_aload_matches_sync_load(temp_wiki: Path) -> None:
    loader = WikiLoader(temp_wiki, poll_seconds=0)
    snap = await loader.aload(force=True)
    assert "entities/wiley.md" in snap.pages
    assert "skills/backend.md" in snap.pages


@pytest.mark.asyncio
async def test_aget_page_and_aall_pages_and_aindex_text(temp_wiki: Path) -> None:
    loader = WikiLoader(temp_wiki, poll_seconds=0)
    page = await loader.aget_page("entities/wiley.md")
    assert page is not None
    assert "Wiley" in page.title

    pages = await loader.aall_pages()
    assert any(p.path == "entities/wiley.md" for p in pages)

    index_text = await loader.aindex_text()
    assert "Wiley" in index_text


@pytest.mark.asyncio
async def test_aload_does_not_block_event_loop(tmp_path: Path) -> None:
    """A reload does synchronous disk I/O; ``aload`` must offload it so the
    event loop keeps servicing other coroutines while it runs.

    Simulated by making ``_build_snapshot`` sleep (blocking, not
    ``asyncio.sleep``) and asserting a concurrently-scheduled coroutine
    still completes well before the reload does.
    """
    wiki = tmp_path / "w"
    wiki.mkdir()
    (wiki / "index.md").write_text(
        "- [Wiley](entities/wiley.md) — body [wiley]\n", encoding="utf-8"
    )
    (wiki / "entities").mkdir()
    (wiki / "entities" / "wiley.md").write_text("# Wiley", encoding="utf-8")

    loader = WikiLoader(wiki, poll_seconds=0)
    real_build_snapshot = loader._build_snapshot

    def slow_build_snapshot():
        time.sleep(0.3)  # blocking sleep — simulates slow disk I/O
        return real_build_snapshot()

    loader._build_snapshot = slow_build_snapshot  # type: ignore[method-assign]

    other_done_at = None

    async def other_coro() -> None:
        nonlocal other_done_at
        await asyncio.sleep(0.05)
        other_done_at = time.monotonic()

    started = time.monotonic()
    _, _ = await asyncio.gather(loader.aload(force=True), other_coro())
    reload_done_at = time.monotonic()

    assert other_done_at is not None
    # The unrelated coroutine finished well before the (artificially slow)
    # reload — proof the event loop wasn't stuck waiting on the disk read.
    assert other_done_at - started < 0.2
    assert reload_done_at - started >= 0.3


@pytest.mark.asyncio
async def test_concurrent_aload_reloads_only_once(tmp_path: Path) -> None:
    """Two coroutines racing the *initial* (unforced) reload must not both
    rebuild the snapshot — the existing ``threading.Lock`` inside ``load()``
    still serializes the actual disk work even when both callers arrive via
    separate worker threads (``asyncio.to_thread``): both coroutines see the
    fast-path ``_needs_reload() -> True`` (empty snapshot) at the same time
    and hop to a thread, but only the winner of the lock actually rebuilds —
    the other blocks briefly then returns that fresh snapshot instead of
    building a second one.
    """
    wiki = tmp_path / "w"
    wiki.mkdir()
    (wiki / "index.md").write_text(
        "- [Wiley](entities/wiley.md) — body [wiley]\n", encoding="utf-8"
    )
    (wiki / "entities").mkdir()
    (wiki / "entities" / "wiley.md").write_text("# Wiley", encoding="utf-8")

    # poll_seconds=0 means "never reload again once loaded" (see
    # _needs_reload), so this is a clean single-race scenario: both calls
    # start from the initial (never-loaded) empty snapshot.
    loader = WikiLoader(wiki, poll_seconds=0)
    real_build_snapshot = loader._build_snapshot
    build_count = 0

    def counting_build_snapshot():
        nonlocal build_count
        build_count += 1
        time.sleep(0.1)
        return real_build_snapshot()

    loader._build_snapshot = counting_build_snapshot  # type: ignore[method-assign]

    snap1, snap2 = await asyncio.gather(loader.aload(), loader.aload())

    assert build_count == 1
    assert snap1 is snap2
    assert "entities/wiley.md" in snap1.pages
