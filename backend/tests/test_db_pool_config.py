"""Pool construction, pressure behavior and the shared deployment capacity policy."""
import importlib.util
import json
from pathlib import Path
import sys

import app.db as db
from app.config import Settings, get_settings

ROOT = Path(__file__).resolve().parents[2]


def test_defaults_respect_the_connection_arithmetic() -> None:
    # No manual mirrors of deploy flags: the generator and this guard consume
    # the same reviewed source, including rollouts, job pools and contingency.
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        spec = importlib.util.spec_from_file_location("c362_pool_policy", ROOT / "scripts/deployment_config.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    config = json.loads((ROOT / "infra/deployment.json").read_text())
    module.validate(config)
    settings = Settings(_env_file=None)
    runtime = module.defaults(ROOT)
    assert settings.db_pool_size == runtime["DB_POOL_SIZE"]
    assert settings.db_max_overflow == runtime["DB_MAX_OVERFLOW"]
    envelope = module.pool_envelope(config, runtime)
    assert envelope["steady_fits"], envelope
    assert envelope["policy_rollout_fits"], envelope
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
