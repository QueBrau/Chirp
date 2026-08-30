"""Application settings loaded from environment variables / .env."""
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration; defaults target the local docker-compose services."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://chirp:chirp@localhost:5432/chirp"
    redis_url: str = "redis://localhost:6379/0"
    # Connection-pool arithmetic (board c207 - S2 of the Aug 26 architecture review;
    # numbers refreshed against live prod in c248).
    # THE INVARIANT: summed over EVERY service on this database, Cloud Run
    # max-instances x (pool_size + max_overflow), plus Postgres's superuser-reserved
    # slots (3) and one proxy/migration session, must stay <= max_connections.
    # LIVE TOPOLOGY (Aug 30, c248) - TWO services share this database now:
    #   chirp-api  maxScale 4 x (3 + 2) = 20   <- these defaults
    #   chirp-ws   maxScale 2 x (1 + 1) =  4   <- passes DB_POOL_SIZE=1 and
    #                                             DB_MAX_OVERFLOW=1 as Cloud Run env,
    #                                             so it does NOT use these defaults
    #   24 demanded + 3 reserved + 1 proxy = 28, against max_connections 100.
    # That 100 is db-custom-1-3840 since the c225 tier bump, NOT db-f1-micro's default
    # 25 that this comment used to name - read off the live instance through the Cloud
    # SQL proxy. Both facts had drifted at once: the tier changed under the number, and
    # chirp-ws arrived as a second consumer that the arithmetic did not model at all.
    # maxScale=4 was read from the live service (gcloud run services describe, Aug 27);
    # it was set at first deploy and the annotation persists across --source redeploys,
    # which is why DEPLOY.md's everyday command never passes it. chirp-ws passes
    # --max-instances=2 explicitly in its own deploy command. The c153 media reconciler
    # is a separate process on this same pool config; it holds at most one connection,
    # sequentially, and fits inside the same headroom. The previous hardcoded
    # 5 + 10 = 15 per instance demanded 60: the database refused connections while every
    # instance sat at near-zero CPU, which autoscaling cannot see (S1/c205 explains why
    # CPU never moves). Raising max-instances on EITHER service, the tier, or these
    # numbers means re-doing that arithmetic - tests/test_db_pool_config.py pins it so
    # the change is conscious.
    db_pool_size: int = 3
    db_max_overflow: int = 2
    # Fail fast: SQLAlchemy's default 30s checkout wait turns pool exhaustion into
    # requests that hang half a minute before erroring. 10s still rides out a burst
    # but surfaces saturation while the client is plausibly still waiting.
    db_pool_timeout: int = 10
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
    # Soft-deleted posts/comments/chirps are hard-deleted by app.jobs.purge once this many
    # days have passed since deleted_at/removed_at. Matches the 30-day response window
    # /privacy section 14 already commits to (board c69) — do not let these drift apart.
    purge_retention_days: int = 30
    # Transactional email (board c87). "log" records the send and delivers nothing,
    # which is the correct default for local dev and the whole test suite: no key is
    # required and no test can accidentally mail a real person. Production sets
    # "resend" and supplies resend_api_key from Secret Manager.
    email_provider: Literal["log", "resend"] = "log"
    resend_api_key: str | None = None
    # Sender identity. josedev.app has been verified in Resend since Aug 24 (board c134),
    # so this default is our real sender now, not Resend's shared onboarding address —
    # the old "we own no domain" note here was true only before that date. Prod does not
    # rely on this default: EMAIL_FROM is set explicitly in the Cloud Run env and wins.
    # It matters for local dev and for anyone reading this to learn who Chirp mails as.
    # Sending is proven; a school inbox actually RECEIVING a code is not — see
    # app.services.email_service for exactly where the evidence stops.
    email_from: str = "Chirp <hello@josedev.app>"
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
