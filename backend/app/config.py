"""Application settings loaded from environment variables / .env."""
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration; defaults target the local docker-compose services."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://chirp:chirp@localhost:5432/chirp"
    redis_url: str = "redis://localhost:6379/0"
    # Deployment tier; non-"local" values enforce safer defaults at startup (SECURITY-REVIEW
    # finding 5) — see app.main.create_app.
    env: Literal["local", "staging", "production"] = "local"
    auth_mode: Literal["emulated", "firebase"] = "emulated"
    firebase_project_id: str | None = None
    stripe_secret_key: str | None = None
    stripe_publishable_key: str | None = None
    stripe_webhook_secret: str | None = None
    # Public https origin Stripe redirects back to after Connect onboarding. Stripe
    # rejects custom schemes, so a chirp:// deep link cannot be used directly.
    app_public_base_url: str | None = None
    cors_origins: list[str] = ["*"]


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide Settings instance (cached)."""
    return Settings()
