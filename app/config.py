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
    # Separate salt so rotating the user-label bucket doesn't invalidate
    # IP-based rate limits.
    user_hash_salt: str = "dev-user-salt"

    rate_limit_per_ip: str = "10/5minutes"
    daily_llm_call_limit: int = 500

    # Per-IP daily ceiling (extra to the burst rate limit above). Counts
    # successful LLM calls only — a single attacker IP can't run the
    # global cost gate to zero by themselves.
    daily_calls_per_ip_limit: int = 80

    # Per-session soft limit. A client could rotate sessionId to bypass,
    # so the IP cap is the real defence; this stops an honest client
    # from looping inside the same drawer.
    messages_per_session_limit: int = 10

    # Pydantic-enforced caps on user input. Tight enough to prevent
    # prompt-stuffing abuse, loose enough for genuine career questions.
    max_user_message_chars: int = 800
    max_messages_per_request: int = 20

    # Cap on completion length. Persona forces synthesis; this is the safety
    # net so a runaway model can't dump a 10k-token essay.
    llm_max_tokens: int = 1000

    # Cap for the (non-streaming) LLM router call. The JSON itself is tiny
    # (`{"paths":[...]}`), but Gemini 2.5 Flash spends thinking tokens against
    # the same budget; 200 was being entirely consumed before the JSON was
    # emitted in prod. 1500 leaves comfortable headroom without meaningfully
    # inflating cost (router runs once per turn, temperature=0).
    router_max_tokens: int = 1500

    llm_providers: str = "openrouter/google/gemini-2.5-flash-lite,openrouter/deepseek/deepseek-v4-flash,gemini/gemini-2.5-flash"

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
