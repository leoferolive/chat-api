"""Loads the markdown wiki and exposes parsed pages with caching.

The wiki layout follows the Karpathy "LLM Wiki" pattern:

  WIKI_DIR/
    index.md          (catalog with one bullet per page)
    entities/*.md
    projects/*.md
    skills/*.md
    concepts/*.md

`index.md` lines that the loader understands look like:

    - [Title](relative/path.md) — short summary [tag1, tag2]

Anything else in the file is ignored, so prose around the catalog is fine.
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
    loaded_at: float = 0.0


class WikiLoader:
    """Reads and caches the wiki from disk.

    Thread-safe enough for the polling watcher; the FastAPI app keeps a
    single instance via the lifespan context.
    """

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

    # --- internals ------------------------------------------------------

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
        index_path = self.wiki_dir / "index.md"
        if not index_path.exists():
            return ""
        return hashlib.sha256(index_path.read_bytes()).hexdigest()

    def _build_snapshot(self) -> WikiSnapshot:
        if not self.wiki_dir.exists():
            return WikiSnapshot(pages={}, index_hash="", loaded_at=time.time())

        index_path = self.wiki_dir / "index.md"
        pages: dict[str, WikiPage] = {}
        if index_path.exists():
            for entry in self._parse_index(index_path):
                page_file = (self.wiki_dir / entry.path).resolve()
                if not str(page_file).startswith(str(self.wiki_dir.resolve())):
                    # Refuse paths that escape WIKI_DIR (defence in depth).
                    continue
                if page_file.exists():
                    entry.content = page_file.read_text(encoding="utf-8")
                pages[entry.path] = entry
        else:
            # Fall back: index every markdown file we find.
            for md in self.wiki_dir.rglob("*.md"):
                rel = md.relative_to(self.wiki_dir).as_posix()
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
