"""Application configuration via pydantic-settings.

All secrets and tunables come from the environment / .env. Note: ANTHROPIC_API_KEY
is deliberately NOT a setting here — Claude Code must use the Max subscription, and
the API key is stripped from the claude subprocess env (see agent/provider.py, spec §8).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- monobank ---
    mono_token: str = ""
    mono_webhook_secret: str = "change-me"

    # --- Telegram ---
    telegram_bot_token: str = ""
    telegram_allowed_user_id: int = 0

    # --- infra ---
    database_url: str = "sqlite+aiosqlite:///./komunalka.db"
    redis_url: str = "redis://localhost:6379/0"

    # --- LLM ---
    llm_provider: str = "claude_code"
    claude_bin: str = "claude"
    claude_timeout_seconds: int = 60

    # --- matching ---
    # NoDecode: pydantic-settings would otherwise JSON-decode this complex field from
    # the dotenv string; we want the "4900,4814" CSV form parsed by the validator below.
    utility_mccs: Annotated[set[int], NoDecode] = Field(
        default_factory=lambda: {4900, 4814}
    )

    # --- misc ---
    tz: str = "Europe/Kyiv"
    public_base_url: str = "https://example.com"

    @field_validator("utility_mccs", mode="before")
    @classmethod
    def _parse_mccs(cls, value: object) -> object:
        """Accept "4900,4814" (env string) as well as a real iterable of ints."""
        if isinstance(value, str):
            return {int(part) for part in value.split(",") if part.strip()}
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
