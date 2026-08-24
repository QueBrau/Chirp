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
    # Soft-deleted posts/comments/yaks are hard-deleted by app.jobs.purge once this many
    # days have passed since deleted_at/removed_at. Matches the 30-day response window
    # /privacy section 14 already commits to (board c69) — do not let these drift apart.
    purge_retention_days: int = 30
    # Transactional email (board c87). "log" records the send and delivers nothing,
    # which is the correct default for local dev and the whole test suite: no key is
    # required and no test can accidentally mail a real person. Production sets
    # "resend" and supplies resend_api_key from Secret Manager.
    email_provider: Literal["log", "resend"] = "log"
    resend_api_key: str | None = None
    # Sender identity. Until c73 buys a domain this is Resend's shared onboarding
    # sender, which Resend only permits to reach our OWN account address — a
    # deliberate, temporary state, explained in full in app.services.email_service.
    email_from: str = "Chirp <onboarding@resend.dev>"
    # Where a human reply should land. Once c74's support mailbox exists this becomes
    # that address, so a student replying to a verification mail reaches a person
    # rather than a sender nobody reads.
    email_reply_to: str | None = None
    # GCS bucket for post media (board c70). None until the bucket exists and is
    # granted to the Cloud Run service account — a real, expected state before that
    # infra step lands, not a misconfiguration. app.services.storage_service fails
    # closed (503) rather than erroring obscurely, same shape as stripe_service's
    # _secret_key() for the identical "feature exists in code, infra not live yet"
    # situation.
    media_bucket_name: str | None = None
    # HMAC key for post-media capability tokens (board c140). Secret Manager only, never
    # a file on disk — same rule the GCS upload signing already follows (see
    # app.services.storage_service's module docstring on keyless signing).
    #
    # None means "this deployment has not turned signed media reads on yet", and that is
    # a REAL, EXPECTED state, not a misconfiguration: the bucket is public-read until the
    # c140 cutover flips public_access_prevention, and until then a stored url is already
    # fetchable as-is. So the serializer falls back to emitting the stored url unchanged
    # rather than failing closed — the build is additive and flips nothing on its own.
    #
    # AFTER the flip, unsetting this does NOT reopen public access; it makes every photo
    # visibly break instead, because the emitted urls would 403 against a private bucket.
    # That is the intended failure direction: loud and harmless, never silent and open.
    media_signing_secret: str | None = None


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide Settings instance (cached)."""
    return Settings()
