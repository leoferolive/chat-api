"""Shared fixtures: temp wiki, temp DB, mocked LLM."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app import config as config_mod
from app import llm_router as llm_router_mod
from app.config import Settings

WIKI_FIXTURE_INDEX = """# Test Wiki

- [Wiley](entities/wiley.md) — Leonardo na Wiley [wiley, backend]
- [Backend](skills/backend.md) — skills backend do Leonardo [backend, python]
"""

WIKI_PAGES = {
    "entities/wiley.md": "# Wiley\n\nLeonardo foi engenheiro backend na Wiley.",
    "skills/backend.md": "# Backend Skills\n\nJava, Spring, Python, FastAPI.",
}

# Noise files that exist *outside* the wiki/ subtree. In production the
# init container clones the full ``leoferolive-wiki`` repo into the volume,
# so files like ``AGENTS.md`` and ``raw/README.md`` end up next to the
# real wiki root. They must NEVER be considered wiki pages.
WIKI_NOISE_FILES = {
    "AGENTS.md": "# AGENTS\n\nRepo metadata, not a wiki page.",
    "README.md": "# leoferolive-wiki\n\nRepo readme, not a wiki page.",
    "raw/README.md": "# raw ingest\n\nScratchpad for ingestion, not a wiki page.",
}


@pytest.fixture
def temp_wiki(tmp_path: Path) -> Path:
    """Mirror the production volume layout.

    ``WIKI_DIR`` points at a directory that contains the *whole* repo
    (because the init container clones it as-is). The actual wiki lives
    under ``<WIKI_DIR>/wiki/``; everything else at the root is noise.
    """
    repo = tmp_path / "wiki"  # acts as WIKI_DIR
    repo.mkdir()
    # Noise outside the wiki subtree.
    for rel, content in WIKI_NOISE_FILES.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    # The real wiki, nested one level deep.
    wiki = repo / "wiki"
    wiki.mkdir()
    (wiki / "index.md").write_text(WIKI_FIXTURE_INDEX, encoding="utf-8")
    for rel, content in WIKI_PAGES.items():
        target = wiki / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return repo


@pytest.fixture
def temp_db_path(tmp_path: Path) -> Path:
    return tmp_path / "chat.sqlite"


@pytest.fixture
def settings(temp_wiki: Path, temp_db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("WIKI_DIR", str(temp_wiki))
    monkeypatch.setenv("DB_PATH", str(temp_db_path))
    monkeypatch.setenv("TURNSTILE_DISABLED", "true")
    monkeypatch.setenv("ENV", "test")
    monkeypatch.setenv("ALLOWED_ORIGINS", "http://test")
    monkeypatch.setenv("LLM_PROVIDERS", "mock/primary,mock/secondary")
    monkeypatch.setenv("RATE_LIMIT_PER_IP", "100/1minute")
    monkeypatch.setenv("DAILY_LLM_CALL_LIMIT", "500")
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    monkeypatch.setenv("IP_HASH_SALT", "test-salt")
    monkeypatch.setenv("USER_HASH_SALT", "test-user-salt")
    monkeypatch.setenv("WIKI_POLL_SECONDS", "0")
    config_mod.reset_settings_cache()
    s = config_mod.get_settings()
    yield s
    config_mod.reset_settings_cache()


# ---- LLM mock ------------------------------------------------------------


class MockState:
    """Lets tests script provider behaviour."""

    def __init__(self) -> None:
        # provider -> "ok" | "raise_open" | "raise_mid"
        self.behaviour: dict[str, str] = {}
        # tokens to emit when ok
        self.tokens: list[str] = ["Hello ", "world", "!"]
        self.calls: list[str] = []

    def reset(self) -> None:
        self.behaviour.clear()
        self.tokens = ["Hello ", "world", "!"]
        self.calls.clear()


@pytest.fixture
def mock_llm(monkeypatch: pytest.MonkeyPatch) -> MockState:
    state = MockState()

    async def fake_acompletion(*, model: str, messages, stream=True, **kwargs):
        state.calls.append(model)
        behaviour = state.behaviour.get(model, "ok")
        if behaviour == "raise_open":
            raise RuntimeError(f"open-failure for {model}")

        async def gen():
            if behaviour == "raise_mid":
                # First yield a token, then explode.
                yield {"choices": [{"delta": {"content": "partial"}}]}
                raise RuntimeError(f"mid-stream failure for {model}")
            for tok in state.tokens:
                yield {"choices": [{"delta": {"content": tok}}]}
            # final usage chunk
            yield {
                "choices": [{"delta": {}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 7},
            }

        return gen()

    monkeypatch.setattr(llm_router_mod.litellm, "acompletion", fake_acompletion)
    return state


# ---- ASGI client ---------------------------------------------------------


@pytest_asyncio.fixture
async def client(settings: Settings, mock_llm: MockState) -> AsyncIterator[AsyncClient]:
    """Async client with FastAPI lifespan manually triggered."""
    # Lazy import so the monkeypatched settings/env take effect first.

    from app.main import create_app

    app = create_app(settings)
    # Run the lifespan context manager manually around the test body.
    lifespan_cm = app.router.lifespan_context(app)
    await lifespan_cm.__aenter__()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            # expose the app for tests that need state access
            c.app = app  # type: ignore[attr-defined]
            yield c
    finally:
        await lifespan_cm.__aexit__(None, None, None)
