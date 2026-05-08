"""Loads the markdown wiki and exposes parsed pages with caching.

The wiki layout follows the Karpathy "LLM Wiki" pattern:

  <root>/
    index.md          (catalog with one bullet per page)
    entities/*.md
    projects/*.md
    skills/*.md
    concepts/*.md

`index.md` lines that the loader understands look like:

    - [Title](relative/path.md) — short summary [tag1, tag2]

Anything else in the file is ignored, so prose around the catalog is fine.

Resolving ``<root>`` from ``WIKI_DIR``
--------------------------------------

In production the K8s init container clones the *entire* ``leoferolive-wiki``
repo into the ``WIKI_DIR`` volume, so the wiki pages live one level deeper:

    WIKI_DIR/
      wiki/             <- actual wiki root (index.md + entities/ + ...)
      AGENTS.md         <- repo metadata, NOT a wiki page
      README.md         <- repo metadata, NOT a wiki page
      raw/              <- ingest scratchpad, NOT wiki pages
      .git/

In dev/tests the fixture is already the wiki root itself (``WIKI_DIR`` points
straight at ``wiki-fixture/``).

To support both layouts without changing K8s config, the loader resolves the
"actual root" at load time: if ``<WIKI_DIR>/wiki/index.md`` exists we use
``<WIKI_DIR>/wiki`` as the root; otherwise we use ``<WIKI_DIR>`` directly.
This keeps everything outside the wiki subtree (``AGENTS.md``, ``raw/``…)
invisible to the retriever.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .models import WikiPage

_INDEX_LINE_RE = re.compile(
    r"""^\s*[-*]\s*                                # list bullet
        \[(?P<title>[^\]]+)\]                      # [Title]
        \((?P<path>[^)]+)\)                        # (path.md)
        (?:\s*[—\-:]\s*(?P<summary>[^\[]+?))?      # optional summary
        (?:\s*\[(?P<tags>[^\]]+)\])?               # optional [tag, tag]
        \s*$
    """,
    re.VERBOSE,
)


@dataclass
class WikiSnapshot:
    pages: dict[str, WikiPage] = field(default_factory=dict)
    index_hash: str = ""
    index_text: str = ""
    loaded_at: float = 0.0


class WikiLoader:
    """Reads and caches the wiki from disk.

    Thread-safe enough for the polling watcher; the FastAPI app keeps a
    single instance via the lifespan context.
    """

    # Subdirectory the init container creates inside WIKI_DIR. When the
    # cloned repo has its actual wiki under ``<repo>/wiki/``, we want the
    # loader scoped to that subtree only.
    _WIKI_SUBDIR = "wiki"

    def __init__(self, wiki_dir: Path, poll_seconds: int = 60) -> None:
        self.wiki_dir = Path(wiki_dir)
        self.poll_seconds = poll_seconds
        self._lock = threading.Lock()
        self._snapshot = WikiSnapshot()
        self._last_check = 0.0

    # --- public API -----------------------------------------------------

    def load(self, force: bool = False) -> WikiSnapshot:
        """Return the current snapshot, reloading if the index changed."""
        with self._lock:
            if force or self._needs_reload():
                self._snapshot = self._build_snapshot()
                self._last_check = time.time()
            return self._snapshot

    def all_pages(self) -> list[WikiPage]:
        return list(self.load().pages.values())

    def get_page(self, relative_path: str) -> WikiPage | None:
        return self.load().pages.get(relative_path)

    def index_text(self) -> str:
        """Return the raw content of ``index.md`` (empty if missing).

        Cached on the snapshot — refreshed together with the page set when
        ``index.md`` changes. Used by the LLM router to let the model
        browse the catalog itself.
        """
        return self.load().index_text

    # --- internals ------------------------------------------------------

    def _resolve_root(self) -> Path:
        """Return the actual wiki root inside ``wiki_dir``.

        Supports two layouts:
          * ``<wiki_dir>/wiki/index.md`` exists  -> ``<wiki_dir>/wiki``
            (prod: init container cloned the full repo into the volume)
          * otherwise                           -> ``<wiki_dir>``
            (dev / tests: WIKI_DIR points straight at the wiki root)
        """
        nested = self.wiki_dir / self._WIKI_SUBDIR
        if (nested / "index.md").is_file():
            return nested
        return self.wiki_dir

    def _needs_reload(self) -> bool:
        if not self._snapshot.pages:
            return True
        if self.poll_seconds <= 0:
            return False
        if time.time() - self._last_check < self.poll_seconds:
            return False
        index_hash = self._hash_index()
        return index_hash != self._snapshot.index_hash

    def _hash_index(self) -> str:
        index_path = self._resolve_root() / "index.md"
        if not index_path.exists():
            return ""
        return hashlib.sha256(index_path.read_bytes()).hexdigest()

    def _build_snapshot(self) -> WikiSnapshot:
        if not self.wiki_dir.exists():
            return WikiSnapshot(pages={}, index_hash="", loaded_at=time.time())

        root = self._resolve_root()
        if not root.exists():
            return WikiSnapshot(pages={}, index_hash="", loaded_at=time.time())

        index_path = root / "index.md"
        root_resolved = root.resolve()
        pages: dict[str, WikiPage] = {}
        index_text = index_path.read_text(encoding="utf-8") if index_path.is_file() else ""
        if index_path.exists():
            for entry in self._parse_index(index_path):
                page_file = (root / entry.path).resolve()
                # Refuse paths that escape the wiki root (defence in depth).
                # Anything outside ``<wiki_dir>/wiki`` (or ``<wiki_dir>`` in
                # dev) — e.g. ``../AGENTS.md`` or ``../raw/README.md`` —
                # is silently dropped here.
                try:
                    page_file.relative_to(root_resolved)
                except ValueError:
                    continue
                if page_file.exists():
                    entry.content = page_file.read_text(encoding="utf-8")
                pages[entry.path] = entry
        else:
            # Fall back: index every markdown file we find under the root.
            # Note: this is scoped to ``root`` (the wiki subtree), so files
            # outside it (e.g. AGENTS.md at the repo root in prod) are
            # never considered.
            for md in root.rglob("*.md"):
                rel = md.relative_to(root).as_posix()
                if rel == "index.md":
                    continue
                pages[rel] = WikiPage(
                    path=rel,
                    title=md.stem.replace("-", " ").title(),
                    summary="",
                    tags=[],
                    content=md.read_text(encoding="utf-8"),
                )

        return WikiSnapshot(
            pages=pages,
            index_hash=self._hash_index(),
            index_text=index_text,
            loaded_at=time.time(),
        )

    @staticmethod
    def _parse_index(index_path: Path) -> list[WikiPage]:
        entries: list[WikiPage] = []
        for raw in index_path.read_text(encoding="utf-8").splitlines():
            match = _INDEX_LINE_RE.match(raw)
            if not match:
                continue
            tags_raw = match.group("tags") or ""
            tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
            summary = (match.group("summary") or "").strip()
            entries.append(
                WikiPage(
                    path=match.group("path").strip(),
                    title=match.group("title").strip(),
                    summary=summary,
                    tags=tags,
                )
            )
        return entries
