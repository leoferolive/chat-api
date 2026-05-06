"""Selects the most relevant wiki pages for a user query.

v1: simple keyword overlap scoring against title + tags + summary. Cheap
and good enough until the wiki grows past ~100 pages.
"""

from __future__ import annotations

import re

from .models import WikiPage
from .wiki_loader import WikiLoader

_TOKEN_RE = re.compile(r"[\w\-]+", re.UNICODE)

# Stopwords removed from queries before scoring (PT + EN).
_STOPWORDS = {
    # PT
    "a", "o", "as", "os", "um", "uma", "de", "do", "da", "dos", "das",
    "para", "por", "com", "sem", "que", "qual", "quais", "quem", "como",
    "onde", "quando", "porque", "porquê", "porquê?", "pra", "no", "na",
    "nos", "nas", "ao", "à", "às", "é", "são", "foi", "ser", "tem",
    "teve", "tinha", "ele", "ela", "isso", "isto", "aquilo", "se",
    "sobre", "mais", "menos", "muito", "pouco",
    # EN
    "the", "an", "of", "in", "on", "at", "to", "for", "with",
    "and", "or", "but", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "does", "did", "what", "who",
    "where", "when", "why", "how", "about", "this", "that", "these",
    "those", "it", "its",
}


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def _query_tokens(query: str) -> set[str]:
    return {t for t in _tokenize(query) if t not in _STOPWORDS and len(t) > 1}


def _score_page(page: WikiPage, q_tokens: set[str]) -> float:
    if not q_tokens:
        return 0.0
    title_tokens = set(_tokenize(page.title))
    tag_tokens = {t.lower() for t in page.tags}
    summary_tokens = set(_tokenize(page.summary))

    score = 0.0
    score += 3.0 * len(q_tokens & title_tokens)
    score += 2.0 * len(q_tokens & tag_tokens)
    score += 1.0 * len(q_tokens & summary_tokens)
    # Soft body match (cap so a long doc doesn't dominate).
    if page.content:
        body_hits = sum(1 for t in q_tokens if t in page.content.lower())
        score += min(body_hits, 4) * 0.5
    return score


class Retriever:
    """Picks top-N wiki pages relevant to a query."""

    def __init__(self, loader: WikiLoader) -> None:
        self.loader = loader

    def pick(self, query: str, lang: str = "pt", top_n: int = 5) -> list[WikiPage]:
        q_tokens = _query_tokens(query)
        all_pages = self.loader.all_pages()
        if not all_pages:
            return []

        scored: list[WikiPage] = []
        for page in all_pages:
            score = _score_page(page, q_tokens)
            if score <= 0:
                continue
            scored.append(page.model_copy(update={"score": score}))

        scored.sort(key=lambda p: p.score, reverse=True)
        if scored:
            return scored[:top_n]

        # Fallback: return up to top_n pages by title alphabetical so the
        # LLM still has *some* grounding rather than an empty context.
        sorted_default = sorted(all_pages, key=lambda p: p.title)[:top_n]
        return [p.model_copy(update={"score": 0.0}) for p in sorted_default]
