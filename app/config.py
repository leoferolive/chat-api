"""Application settings loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Centralised application configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    env: str = "dev"
    allowed_origins: str = "http://localhost:5173"

    wiki_dir: Path = Path("./wiki-fixture")
    db_path: Path = Path("./data/chat.sqlite")

    turnstile_secret: str = "dev-secret"
    turnstile_site_key: str = "dev-site-key"
    turnstile_disabled: bool = True

    session_secret: str = "dev-session-secret-change-me"
    session_ttl_seconds: int = 3600

    ip_hash_salt: str = "dev-ip-salt"

    rate_limit_per_ip: str = "10/5minutes"
    daily_llm_call_limit: int = 500

    llm_providers: str = "gemini/gemini-2.5-flash,openrouter/anthropic/claude-haiku-4.5"

    retriever_top_n: int = 5
    wiki_poll_seconds: int = 60

    # API keys are read directly by LiteLLM from env, but we surface them so
    # pydantic-settings doesn't complain about extra envs and so tests can
    # set them programmatically.
    gemini_api_key: str | None = Field(default=None)
    openrouter_api_key: str | None = Field(default=None)

    # Z.AI (Zhipu) — exposes an OpenAI-compatible endpoint, so we route it
    # through LiteLLM's openai-compatible path with a custom api_base.
    zai_api_key: str | None = Field(default=None)
    zai_base_url: str = "https://api.z.ai/api/paas/v4/"

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def provider_list(self) -> list[str]:
        return [p.strip() for p in self.llm_providers.split(",") if p.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Clear cached settings (useful in tests)."""
    get_settings.cache_clear()
