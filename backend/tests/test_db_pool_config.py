"""c207 (S2 of the Aug 26 architecture review): pool sizing is settings-driven and
the defaults respect the deployment's connection arithmetic.

The constants below are the LIVE topology the defaults were sized against
(Cloud Run maxScale 4, db-f1-micro max_connections 25, Postgres's 3
superuser-reserved slots, one proxy/migration session). If a default changes in
app.config, or the topology changes, this test failing is the mechanism that
forces the arithmetic to be re-done consciously rather than drifting.
"""

import app.db as db
from app.config import Settings, get_settings

CLOUD_RUN_MAX_INSTANCES = 4
DB_MAX_CONNECTIONS = 25
SUPERUSER_RESERVED = 3
PROXY_MIGRATION_HEADROOM = 1


def test_defaults_respect_the_connection_arithmetic() -> None:
    settings = Settings(_env_file=None)
    per_instance = settings.db_pool_size + settings.db_max_overflow
    demanded = CLOUD_RUN_MAX_INSTANCES * per_instance
    assert (
        demanded + SUPERUSER_RESERVED + PROXY_MIGRATION_HEADROOM <= DB_MAX_CONNECTIONS
    ), (
        f"pool defaults demand {demanded} connections across "
        f"{CLOUD_RUN_MAX_INSTANCES} instances; with {SUPERUSER_RESERVED} reserved "
        f"and {PROXY_MIGRATION_HEADROOM} headroom that exceeds "
        f"max_connections={DB_MAX_CONNECTIONS} - re-do the c207 arithmetic"
    )
    # Fail fast under saturation: anything at or past SQLAlchemy's 30s default just
    # converts exhaustion into hung requests.
    assert settings.db_pool_timeout < 30


def test_engine_is_built_from_the_pool_settings(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_create_engine(url: str, **kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setenv("DB_POOL_SIZE", "7")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "4")
    monkeypatch.setenv("DB_POOL_TIMEOUT", "9")
    monkeypatch.setattr(db, "create_async_engine", fake_create_engine)
    monkeypatch.setattr(db, "_engine", None)
    get_settings.cache_clear()
    try:
        db.get_engine()
    finally:
        # The cache must not leak these env-derived settings into other tests;
        # monkeypatch restores _engine and the env vars themselves at teardown.
        get_settings.cache_clear()

    assert captured["pool_size"] == 7
    assert captured["max_overflow"] == 4
    assert captured["pool_timeout"] == 9
    assert captured["pool_pre_ping"] is True


async def test_pool_exhaustion_maps_to_503_with_retry_after() -> None:
    """Checkout timeout is a capacity signal: 503 + Retry-After, never a generic 500.

    A 500 reads as a crash and invites an immediate retry into the saturated pool;
    503 tells the client to back off. The handler is registered per-class, so it runs
    in Starlette's ExceptionMiddleware before ServerErrorMiddleware ever sees it.
    """
    from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

    from app.main import create_app

    app = create_app()
    handler = app.exception_handlers[SQLAlchemyTimeoutError]
    response = await handler(None, SQLAlchemyTimeoutError())
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert b"over_capacity" in response.body
