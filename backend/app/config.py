"""Application settings loaded from environment variables / .env."""
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration; defaults target the local docker-compose services."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://chirp:chirp@localhost:5432/chirp"
    redis_url: str = "redis://localhost:6379/0"
    # Deployment sizing and connection capacity policy live in
    # infra/deployment.json; scripts/deployment-config derives steady, rollout
    # and job envelopes from it. Missing live pool env is explicitly reported
    # as inferred from these checkout defaults, not proven for an older image.
    # Cloud Run can briefly exceed configured maxima. Neither the model nor
    # a historical database capacity is a hard connection/availability guarantee.
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
    # Which router families this process mounts (board c375): "all" is today's
    # behavior (every domain router plus the /ws gateway), "api" drops the /ws
    # gateway, "ws" drops every domain router except app.routers.deployment (kept
    # so DEPLOY-VERIFICATION.md's authenticated /_deployment check still has
    # something to hit). An out-of-enum value fails Settings() construction the
    # same way env/auth_mode do; see app.main.create_app for the mounting
    # dispatch and DEPLOY.md's "Service roles" section for the operational
    # picture, including why this PR does not flip any live service's value.
    service_role: Literal["all", "api", "ws"] = "all"
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
    # Grace window before ALREADY-USELESS e2ee key material (consumed one-time prekeys,
    # superseded signed prekeys, any prekey row belonging to a long-revoked device) is
    # hard-deleted by app.jobs.purge's key-retirement phase. Deliberately separate from
    # purge_retention_days: that setting governs user-CONTENT soft-delete grace, this one
    # governs crypto-hygiene cleanup of material nothing can use any more — the two have
    # different reasons to change and should not be forced to move together (board c347).
    e2ee_key_retirement_grace_days: int = 7
    # Transactional email (board c87). "log" records the send and delivers nothing,
    # which is the correct default for local dev and the whole test suite: no key is
    # required and no test can accidentally mail a real person. Production sets
    # "resend" or "ses" and supplies that provider's credentials from Secret Manager.
    #
    # "ses" is the launch target (c240: Jose chose SES over Resend Pro, cheaper by orders
    # of magnitude at our volume, and Resend's free tier caps at 100/DAY not 3,000/month).
    # The switch is a config change alone — env flip plus secret mount — which is the
    # whole reason app.services.email_service put the provider behind this Literal.
    email_provider: Literal["log", "resend", "ses"] = "log"
    resend_api_key: str | None = None
    # SES credentials (c284). All three are None until Jose's AWS account exists (his half
    # of c240: domain DNS plus the sandbox-exit request), and email_service fails CLOSED
    # with 503 email_not_configured rather than half-sending — same shape as the Resend
    # key and stripe_service's _secret_key, so an unconfigured integration reads as the
    # deployment state it is rather than as a user error.
    #
    # The region is not guessable from anything else and a wrong one produces a signature
    # that fails against the right host, so it is explicit rather than defaulted to
    # whatever us-east-1 happens to be.
    aws_region: str | None = None
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
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
    # The intentional bearer-revocation window (board c350): how long a still-valid
    # capability token can keep redirecting to a photo after the viewer's entitlement
    # to it is revoked (removed from the chapter, suspended, campus verification
    # lapsed, or the post deleted), in the worst case. storage_service derives the
    # token/signed-url TTL from this as 2x the window (see that module's WHY THE FIX
    # IS QUANTIZED EXPIRY section) — unchanged at 6h/12h by manager ruling (c350 R1),
    # only the FREEZE at import time is what changes: this now reads live from
    # Settings on every mint/verify, so an operator can retune the window without a
    # code change. "Without a code change" does NOT mean "without a deploy" — Settings
    # are not hot-reloaded, so a new value here only takes effect on the next
    # Cloud Run revision (see DEPLOY.md's "Media revocation window" section).
    media_revocation_window_hours: int = 6
    # How long a POSITIVE media-entitlement decision may be reused in-process before
    # re-checking the database (board c350 R5). Bounds the cost of the per-request
    # entitlement re-check GET /media/{token} now performs — a feed render can fan
    # out to 20+ image requests per viewer — while adding this many seconds to the
    # worst-case revocation floor on top of the window above. Denials are never
    # memoized (see app.services.media_entitlement), so a re-grant after a mistaken
    # revocation is immediate; only an actual revocation waits out this TTL.
    media_entitlement_memo_seconds: int = 60
    # Durable delivery outbox sweeper (board c356) -- see app/services/outbox.py.
    # Disabling this only stops the background retry loop; the live/fast dispatch
    # path on send_message is unaffected.
    outbox_sweeper_enabled: bool = True
    outbox_sweep_interval_s: float = 5.0
    outbox_sweep_batch_limit: int = 10
    # One recipient loop's total deadline, mirroring POLL_BROADCAST_TIMEOUT_SECONDS's
    # order of magnitude (polls.py) -- not shared with it, since polls stay untouched
    # by this card.
    outbox_dispatch_timeout_s: float = 2.0
    outbox_max_attempts: int = 8
    outbox_backoff_base_s: float = 1.0
    outbox_backoff_cap_s: float = 60.0
    # How long a sweep's claim UPDATE holds a row before another sweep may reclaim it
    # (next_attempt_at is pushed this far into the future at claim time, then
    # overwritten with the real backoff/delivered outcome once the publish attempt
    # finishes) -- the lease, not a retry backoff. Must comfortably exceed
    # outbox_dispatch_timeout_s so a normal in-flight attempt is never reclaimed.
    outbox_lease_s: float = 30.0


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide Settings instance (cached)."""
    return Settings()
