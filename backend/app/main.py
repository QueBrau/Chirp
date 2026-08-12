"""FastAPI application factory: CORS, all domain routers, WS gateway, health check."""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers import (
    alumni,
    auth,
    chapters,
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

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
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
    ):
        app.include_router(module.router)
    app.include_router(gateway.router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
