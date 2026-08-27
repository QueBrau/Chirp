"""An open WebSocket must not hold a pooled Postgres connection (board card c205).

THE BUG THIS PINS. ws/gateway.py took `session: AsyncSession = Depends(get_session)`.
FastAPI holds a yield-dependency open for the whole endpoint call, which on an HTTP
route is milliseconds and on a WEBSOCKET route is the entire user session. Every
connected user therefore pinned one connection out of a pool of 15 per instance, and
HTTP requests draw from that same pool - so idle sockets could starve the instance's
REST API at near-zero CPU, which is invisible to CPU-based autoscaling.

WHY THIS FILE EXISTS RATHER THAN AN ASSERTION IN THE EXISTING WS TESTS. Every test in
test_ws_fanout / test_ws_suspension / test_ws_auth_subprotocol passes both BEFORE and
AFTER the fix, because a leaked connection changes no observable behaviour until the
pool runs dry. The leak was invisible to the entire suite. These tests look at the pool
itself, which is the only place the difference shows up.

Both tests below fail against the pre-c205 gateway and pass after it.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import text
from starlette.testclient import TestClient


@pytest.fixture
def ws_client(migrated_db: str) -> Any:
    """Same reset-singletons-then-TestClient shape as test_ws_suspension.py.

    Duplicated rather than imported for the reason that file already records: pytest
    fixtures in a sibling test module are not a supported import target.
    """
    import app.db as app_db
    import app.ws.pubsub as app_pubsub
    from app import models  # noqa: F401  # populate Base.metadata
    from app.main import create_app

    def _reset_singletons() -> None:
        app_db._engine = None
        app_db._session_factory = None
        app_pubsub._client = None

    _reset_singletons()
    setup_engine = app_db.get_engine()

    async def _truncate() -> None:
        table_names = ", ".join(t.name for t in app_db.Base.metadata.sorted_tables)
        async with setup_engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE TABLE {table_names} CASCADE"))
        await setup_engine.dispose()

    import asyncio

    asyncio.run(_truncate())
    _reset_singletons()

    with TestClient(create_app()) as client:
        yield client
        if app_db._engine is not None:
            client.portal.call(app_db._engine.dispose)
        if app_pubsub._client is not None:
            client.portal.call(app_pubsub._client.aclose)

    _reset_singletons()


def _redis_reachable() -> bool:
    """Same probe as test_ws_fanout.py, duplicated for the same reason the fixture is."""
    import socket
    from urllib.parse import urlparse

    from app.config import get_settings

    parsed = urlparse(get_settings().redis_url)
    try:
        with socket.create_connection(
            (parsed.hostname or "localhost", parsed.port or 6379), timeout=0.5
        ):
            return True
    except OSError:
        return False


#: Both tests here open a socket that reaches pubsub.subscribe, so unlike the auth
#: tests in test_ws_fanout.py neither can run without Redis.
needs_redis = pytest.mark.skipif(
    not _redis_reachable(),
    reason="no Redis at settings.redis_url — see board c92 (brew install redis, or use CI)",
)


@dataclass
class WsUser:
    id: str
    headers: dict[str, str]


def _make_user(client: TestClient, display_name: str) -> WsUser:
    uid = f"uid-{uuid.uuid4().hex}"
    headers = {"X-Debug-Firebase-Uid": uid}
    response = client.post(
        "/auth/bootstrap",
        json={"email": f"{uid}@example.edu", "display_name": display_name, "account_type": "greek"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return WsUser(id=response.json()["id"], headers=headers)


def _checked_out(client: TestClient) -> int:
    """Connections currently checked OUT of the engine's pool.

    Read through the portal so it is sampled on the same event loop the app runs on,
    matching how these tests do every other cross-loop call.
    """
    import app.db as app_db

    def _sample() -> int:
        return app_db.get_engine().pool.checkedout()

    return client.portal.call(_sample)  # type: ignore[arg-type]


@needs_redis
def test_an_open_socket_holds_no_pooled_connection(ws_client: TestClient) -> None:
    """The whole point of c205, stated as one number.

    A connection checked out while the socket merely sits there is the bug. The
    suspension poll does take one briefly every ~30s, but this assertion runs far
    inside the first interval, so a non-zero count here means the connection is held
    for the socket's lifetime rather than for a query.
    """
    client = ws_client
    user = _make_user(client, "Socket Holder")

    baseline = _checked_out(client)

    with client.websocket_connect("/ws", headers=user.headers) as ws:
        assert ws is not None
        assert _checked_out(client) == baseline, (
            "an idle WebSocket is holding a pooled connection — this is the c205 leak; "
            "the gateway must not take a session that spans the connection"
        )


@needs_redis
def test_http_still_works_with_more_sockets_open_than_the_pool_holds(
    ws_client: TestClient,
) -> None:
    """The consequence, not just the mechanism.

    The pool is pool_size=5 + max_overflow=10 = 15. Sixteen concurrent sockets is one
    more than the pool can ever hand out, so against the pre-c205 gateway the 16th
    connect blocks on checkout until it times out, and the HTTP request below cannot
    get a connection either because sockets and requests share the pool.

    Deliberately asserts on an ORDINARY HTTP CALL rather than on pool internals: the
    thing that actually broke in production was the REST API going unanswerable while
    CPU sat idle, and that is what this reproduces.
    """
    client = ws_client
    user = _make_user(client, "Pool Exhauster")

    sockets = []
    try:
        for _ in range(16):
            socket = client.websocket_connect("/ws", headers=user.headers)
            socket.__enter__()
            sockets.append(socket)

        response = client.get("/auth/me", headers=user.headers)
        assert response.status_code == 200, (
            "HTTP is starved while sockets are open — sockets and requests share one "
            f"pool of 15 (got {response.status_code})"
        )
    finally:
        for socket in reversed(sockets):
            try:
                socket.__exit__(None, None, None)
            except Exception:
                pass
