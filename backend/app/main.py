"""FastAPI application factory: CORS, all domain routers, WS gateway, health check."""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers import (
    alumni,
    auth,
    chapters,
    events,
    feed,
    finance,
    keys,
    lineage,
    meetings,
    messages,
    moderation,
    payments,
    yaks,
)
from app.ws import gateway


def create_app() -> FastAPI:
    """Build the Chirp API app; run with `uvicorn app.main:create_app --factory`."""
    settings = get_settings()
    app = FastAPI(title="Chirp API", version="0.1.0")

    # SECURITY-REVIEW finding 5: never pair a wildcard origin with credentialed CORS —
    # Starlette reflects any origin, so "*" + credentials lets any website's JS send
    # the debug-uid header (emulated mode) or a stolen cookie and impersonate a user.
    allow_credentials = "*" not in settings.cors_origins
    if settings.env != "local":
        # Refuse to start a non-local deployment with dev-only defaults still in place.
        assert settings.auth_mode == "firebase", (
            f"env={settings.env!r} requires auth_mode='firebase', not 'emulated'"
        )
        assert "*" not in settings.cors_origins, (
            f"env={settings.env!r} forbids wildcard cors_origins"
        )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    for module in (
        auth,
        chapters,
        keys,
        messages,
        feed,
        yaks,
        moderation,
        lineage,
        finance,
        meetings,
        alumni,
        payments,
        events,
    ):
        app.include_router(module.router)
    app.include_router(gateway.router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
