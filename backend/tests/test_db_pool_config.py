"""c207 (S2 of the Aug 26 architecture review): pool sizing is settings-driven and
the defaults respect the deployment's connection arithmetic.

The constants below are the LIVE topology the defaults are sized against. If a
default changes in app.config, or the topology changes, this test failing is the
mechanism that forces the arithmetic to be re-done consciously rather than
drifting.

c248 refreshed them, because they had gone stale in two directions at once and
the guard was passing FOR THE WRONG REASON. max_connections was still
db-f1-micro's default 25 after c225 moved the instance to db-custom-1-3840, and
chirp-ws had become a SECOND service on this same database without ever
appearing in the arithmetic. Nothing was failing and nothing is failing now -
real demand is 28 against 100, so there are 72 connections of headroom. The
defect was that a guard reading as authoritative was modelling a deployment
that no longer exists, and would have blocked a legitimate scale-up.
"""

import app.db as db
from app.config import Settings, get_settings

# --- chirp-api: these Settings defaults, at its own maxScale ---
# 4 -> 8 on Sep 3 (board c287, stage-1 scale-up, Jose's go): raised alongside
# --cpu=2 and --min-instances=1. The conscious arithmetic this test exists to
# force: 8 x (3+2) = 40 api + 2 x (1+1) = 4 ws + 3 reserved + 1 proxy = 48 of
# max_connections=100 - 52 spare. Raising maxScale past 19 without touching pool
# sizes is what would finally break this bound; the assertion below is the fence.
CLOUD_RUN_MAX_INSTANCES = 8

# --- chirp-ws: a SECOND service on the same database ---
# It does NOT use the code defaults - its deploy command passes
# DB_POOL_SIZE=1;DB_MAX_OVERFLOW=1 as Cloud Run env, and --max-instances=2.
# Those three numbers live in the deploy command in INFRA-PRIVATE.html, which is
# gitignored, so this test cannot read them and they are mirrored here by hand:
# changing that command means changing these, and there is no mechanism that
# will remind you. That is a known weakness of this guard, not an oversight.
WS_MAX_INSTANCES = 2
WS_POOL_SIZE = 1
WS_MAX_OVERFLOW = 1

# --- the database both services share ---
# 100, not db-f1-micro's default 25: c225 moved the instance to db-custom-1-3840.
# Read off live prod through the Cloud SQL proxy (manager, Aug 30, c248).
DB_MAX_CONNECTIONS = 100
SUPERUSER_RESERVED = 3
PROXY_MIGRATION_HEADROOM = 1


def test_defaults_respect_the_connection_arithmetic() -> None:
    settings = Settings(_env_file=None)
    api_per_instance = settings.db_pool_size + settings.db_max_overflow
    api_demanded = CLOUD_RUN_MAX_INSTANCES * api_per_instance
    # chirp-ws is sized by env, not by these defaults, but it draws from the same
    # max_connections - leaving it out is what made the old arithmetic wrong.
    ws_demanded = WS_MAX_INSTANCES * (WS_POOL_SIZE + WS_MAX_OVERFLOW)
    demanded = api_demanded + ws_demanded
    assert (
        demanded + SUPERUSER_RESERVED + PROXY_MIGRATION_HEADROOM <= DB_MAX_CONNECTIONS
    ), (
        f"pool defaults demand {demanded} connections across both services "
        f"(chirp-api {CLOUD_RUN_MAX_INSTANCES} x {api_per_instance} = {api_demanded}, "
        f"chirp-ws {WS_MAX_INSTANCES} x {WS_POOL_SIZE + WS_MAX_OVERFLOW} = {ws_demanded}); "
        f"with {SUPERUSER_RESERVED} reserved and {PROXY_MIGRATION_HEADROOM} headroom "
        f"that exceeds max_connections={DB_MAX_CONNECTIONS} - re-do the c207 "
        f"arithmetic, and raising max-instances on EITHER service changes it"
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
    503 tells the client to back off. This goes through a REAL request against the
    real app: get_session is overridden to raise the exact exception the pool raises
    on checkout timeout, deep inside dependency resolution (get_current_user depends
    on it), so the assertion proves interception through the full middleware stack -
    not just the handler's own return value.
    """
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

    from app.db import get_session
    from app.main import create_app

    async def exhausted_pool():
        raise SQLAlchemyTimeoutError("QueuePool limit reached, connection timed out")
        yield  # pragma: no cover - never reached; keeps the generator shape

    app = create_app()
    app.dependency_overrides[get_session] = exhausted_pool
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.put(
            "/posts/00000000-0000-0000-0000-000000000000/likes",
            headers={"X-Debug-Firebase-Uid": "test-uid"},
        )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.json() == {"detail": "over_capacity"}
