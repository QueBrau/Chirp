"""FastAPI application factory: CORS, all domain routers, WS gateway, health check."""
import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import Settings, get_settings
from app.core.log_scrub import install_credential_log_scrub
from app.routers import (
    alumni,
    auth,
    campus_verification,
    chapters,
    events,
    feed,
    finance,
    keys,
    lineage,
    media,
    meetings,
    polls,
    messages,
    moderation,
    payments,
    yaks,
)
from app.ws import gateway

logger = logging.getLogger(__name__)

# Long enough that a cold Memorystore connection is not called dead, short enough
# that a wrong host cannot hold a Cloud Run cold start hostage.
_REDIS_PROBE_TIMEOUT_S = 3.0


async def _probe_redis(settings: Settings) -> None:
    """PING Redis at boot and say so in the logs, either way. Never fatal.

    Board c61: Redis was never provisioned in chirps-prod at all — no Memorystore
    instance, no VPC connector, and no REDIS_URL on the service, so config.py's
    redis://localhost:6379/0 default was live and pointing inside a container
    where nothing listens. That went unnoticed for days because the only symptom
    was per-connection: the WS gateway died at subscribe time (c62), which reads
    as flaky clients rather than as missing infrastructure. Nothing anywhere said
    "there is no Redis".

    This is the same principle the firebase_admin init above already follows — a
    misconfigured deploy should announce itself at startup rather than hide in
    request-time failures.

    Deliberately NOT fatal, unlike the Firebase branch. Real-time fan-out is the
    only thing that needs Redis, and today nothing in the app even opens a socket
    (c63), so refusing to boot would take down a working HTTP API over a feature
    nobody is using yet. The log line is the deliverable: it turns a silent
    absence into one greppable line in Cloud Run.
    """
    if settings.env == "local":
        # Local dev without Redis is a normal, uninteresting state; warning about
        # it every boot trains people to ignore the warning that matters.
        return

    from redis.asyncio import Redis

    # A throwaway client on the URL we were handed, deliberately not the shared
    # get_redis() singleton. Two reasons: this probe must test the configuration
    # it was given rather than whatever global state happens to be cached, and a
    # failed connection at boot must not leave the app's long-lived pool holding
    # a dead socket.
    probe = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        await asyncio.wait_for(probe.ping(), timeout=_REDIS_PROBE_TIMEOUT_S)
    except Exception as exc:
        # The URL can carry a password (Memorystore AUTH and every managed Redis
        # put it there), so log the failure TYPE, never the value.
        logger.error(
            "redis unreachable at startup (%s) — real-time fan-out is DOWN; "
            "websocket connects will close 4503. Check that Memorystore and the "
            "VPC connector exist and that REDIS_URL is set on the service.",
            type(exc).__name__,
        )
        return
    else:
        logger.info("redis reachable — real-time fan-out available")
    finally:
        with contextlib.suppress(Exception):
            await probe.aclose()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize firebase_admin at boot, not lazily per-request, so a misconfigured
    deploy fails visibly at startup instead of masking init failures as request-time 401s.
    """
    settings = get_settings()
    if settings.auth_mode == "firebase":
        import firebase_admin

        try:
            firebase_admin.get_app()
        except ValueError:
            # Pin the token audience to the configured Firebase project; without
            # this the SDK infers the ambient GCP project, which rejects tokens
            # whenever the Firebase project differs from where the code runs.
            options = (
                {"projectId": settings.firebase_project_id}
                if settings.firebase_project_id
                else None
            )
            firebase_admin.initialize_app(options=options)

    # Fire-and-forget, NOT awaited. The probe's only deliverable is a log line,
    # so gating readiness on it would add up to _REDIS_PROBE_TIMEOUT_S to every
    # Cloud Run cold start — and c61 established that production's Redis is
    # unreachable today, so that would be the full timeout on every scale-up.
    # The handle is kept on app.state so the task is not garbage collected
    # mid-flight, which is the trap with bare create_task().
    app.state.redis_probe = asyncio.create_task(_probe_redis(settings))

    yield

    app.state.redis_probe.cancel()
    with contextlib.suppress(Exception, asyncio.CancelledError):
        await app.state.redis_probe


def create_app() -> FastAPI:
    """Build the Chirp API app; run with `uvicorn app.main:create_app --factory`."""
    # c146: scrub token/access_token/id_token query params from uvicorn's access log
    # before any request is served. A tripwire, not a fallback for c143's fix — see
    # app.core.log_scrub for why a FUTURE client putting a credential back in a URL
    # is what this guards against, not the mechanism c143 already closed.
    install_credential_log_scrub()
    settings = get_settings()
    # /docs, /redoc and /openapi.json publish the entire route/schema surface with no
    # auth of their own. Fine locally; on any real deployment they hand an attacker a
    # map for free, and they were live on chirps-prod (board finding, Aug 16 audit)
    # simply because nobody had ever turned them off.
    docs_enabled = settings.env == "local"
    app = FastAPI(
        title="Chirp API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )

    # SECURITY-REVIEW finding 5: never pair a wildcard origin with credentialed CORS —
    # Starlette reflects any origin, so "*" + credentials lets any website's JS send
    # the debug-uid header (emulated mode) or a stolen cookie and impersonate a user.
    allow_credentials = "*" not in settings.cors_origins
    if settings.env != "local":
        # Refuse to start a non-local deployment with dev-only defaults still in place.
        #
        # `raise`, not `assert`. This guard exists to make exactly one failure mode
        # impossible: emulated auth or a wildcard CORS origin reaching a real
        # deployment (SECURITY-REVIEW finding 5 - any website's JS impersonating any
        # uid). An `assert` is compiled OUT entirely under `python -O` /
        # `PYTHONOPTIMIZE=1`, silently turning the one line that exists to prevent a
        # catastrophic misconfiguration into a no-op. Nothing in this Dockerfile sets
        # either today, which is exactly the kind of fact that stops being true
        # without anyone deciding it should.
        if settings.auth_mode != "firebase":
            raise RuntimeError(
                f"env={settings.env!r} requires auth_mode='firebase', not {settings.auth_mode!r}"
            )
        if "*" in settings.cors_origins:
            raise RuntimeError(f"env={settings.env!r} forbids wildcard cors_origins")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    for module in (
        auth,
        campus_verification,
        chapters,
        keys,
        messages,
        feed,
        yaks,
        moderation,
        lineage,
        finance,
        meetings,
    polls,
        media,
        alumni,
        payments,
        events,
    ):
        app.include_router(module.router)
    app.include_router(gateway.router)

    # NOT /healthz. Google's frontend intercepts that exact path and answers it
    # with its own HTML 404 before the request ever reaches Cloud Run, so the
    # route existed, appeared in /openapi.json, and was still unreachable from
    # the internet (board c65). An uptime check pointed at it would have
    # reported the API down while it was perfectly healthy.
    @app.get("/_health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
