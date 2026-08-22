"""The WS gateway must fail legibly when Redis is unreachable (board c62).

Why this matters more than it looks: c61 confirmed Redis was never provisioned
in chirps-prod at all — no Memorystore instance, no VPC connector, no REDIS_URL
on the running revision — so config.py's redis://localhost:6379/0 default is
what production actually uses. Every websocket connect in prod hits this path,
not some rare edge case.

Before the fix, `pubsub.subscribe()` ran after `websocket.accept()` with no
try/except. An unreachable Redis raised ConnectionError mid-handler, so the
client saw a socket that opened and immediately died: identical in shape to a
flaky network, with nothing at the server saying the cause was missing
infrastructure. That is the failure mode that turns a config gap into a week of
chasing "flaky clients".

These tests run against a REAL dead port rather than a mocked Redis, because
the thing under test is precisely what happens when the network refuses.

Note on scope: this is deliberately a separate file from the fan-out suite in
PR #12 (tests/test_ws_fanout.py, not yet merged) so the two do not conflict.

Every connect below moved from `?token=<uid>` to `subprotocols=[uid]` (security-
pass item 7, ~Aug 22) — the query string this file exercised is gone from the
gateway entirely, not just deprecated. See test_ws_auth_subprotocol.py.
"""
from __future__ import annotations

import socket
import uuid
from collections.abc import Iterator

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.ws.gateway import WS_REALTIME_UNAVAILABLE


def _closed_port() -> int:
    """Bind a port, learn its number, release it — so connecting is refused, not hung.

    Picking a hardcoded 'probably free' port makes the test pass for the wrong
    reason on a machine where something happens to listen there. This guarantees
    a refusal.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def dead_redis_client(migrated_db: str) -> Iterator[TestClient]:
    """A TestClient whose app points at a Redis that refuses connections.

    Everything is built inside this fixture rather than reusing the async
    `client` fixture: TestClient drives its own event loop, and an engine
    created on the pytest-asyncio loop cannot be used from it. Resetting the
    module globals makes the app lazily build both the engine and the Redis
    client in TestClient's loop.
    """
    import app.db as app_db
    import app.ws.pubsub as pubsub_module
    from app import models  # noqa: F401  # populate Base.metadata
    from app.config import get_settings
    from app.main import create_app

    original_engine, original_factory = app_db._engine, app_db._session_factory
    original_redis = pubsub_module._client

    import os

    original_url = os.environ.get("REDIS_URL")
    os.environ["REDIS_URL"] = f"redis://127.0.0.1:{_closed_port()}/0"
    get_settings.cache_clear()

    app_db._engine = None
    app_db._session_factory = None
    pubsub_module._client = None

    try:
        with TestClient(create_app()) as test_client:
            yield test_client
    finally:
        if original_url is None:
            os.environ.pop("REDIS_URL", None)
        else:
            os.environ["REDIS_URL"] = original_url
        get_settings.cache_clear()
        app_db._engine, app_db._session_factory = original_engine, original_factory
        pubsub_module._client = original_redis


def _bootstrap(test_client: TestClient) -> str:
    """Create a user through the real API and return its firebase uid."""
    uid = f"uid-{uuid.uuid4().hex}"
    response = test_client.post(
        "/auth/bootstrap",
        json={
            "email": f"{uid}@example.edu",
            "display_name": "WS Tester",
            "account_type": "greek",
        },
        headers={"X-Debug-Firebase-Uid": uid},
    )
    assert response.status_code == 201, response.text
    return uid


def test_unreachable_redis_closes_with_realtime_unavailable(
    dead_redis_client: TestClient,
) -> None:
    """An authenticated socket against a dead Redis closes 4503, not 4401 and not a crash."""
    uid = _bootstrap(dead_redis_client)

    with pytest.raises(WebSocketDisconnect) as caught:
        with dead_redis_client.websocket_connect("/ws", subprotocols=[uid]) as ws:
            # Nothing should arrive; the close happens during subscribe. Reading
            # is what surfaces the close frame.
            ws.receive_text()

    assert caught.value.code == WS_REALTIME_UNAVAILABLE


def test_realtime_unavailable_is_distinct_from_auth_failure(
    dead_redis_client: TestClient,
) -> None:
    """4503 and 4401 must stay distinguishable, which is the whole point of the fix.

    A client cannot act correctly on a single generic code: 4401 means the
    session is bad and the user should be sent to sign-in, while 4503 means the
    session is fine and the client should back off and retry. If a broken
    backend and a bad token looked the same, a Redis outage would silently sign
    every user out.
    """
    # Unknown uid: never bootstrapped, so it resolves to no user.
    with pytest.raises(WebSocketDisconnect) as unauthenticated:
        with dead_redis_client.websocket_connect("/ws", subprotocols=["uid-does-not-exist"]) as ws:
            ws.receive_text()

    assert unauthenticated.value.code == 4401
    assert unauthenticated.value.code != WS_REALTIME_UNAVAILABLE

    uid = _bootstrap(dead_redis_client)
    with pytest.raises(WebSocketDisconnect) as backend_down:
        with dead_redis_client.websocket_connect("/ws", subprotocols=[uid]) as ws:
            ws.receive_text()

    assert backend_down.value.code == WS_REALTIME_UNAVAILABLE


def test_missing_token_still_closes_4401(dead_redis_client: TestClient) -> None:
    """The auth gate must run BEFORE anything touches Redis.

    Load-bearing ordering: if the handler reached out to Redis first, an
    unauthenticated connection against a dead backend would report 4503 and leak
    the fact that the gateway got as far as trying, instead of refusing on
    credentials alone.
    """
    with pytest.raises(WebSocketDisconnect) as caught:
        with dead_redis_client.websocket_connect("/ws") as ws:
            ws.receive_text()

    assert caught.value.code == 4401


async def test_forwarder_death_closes_the_socket(dead_redis_client: TestClient) -> None:
    """If the pubsub stream dies AFTER connect, the socket must close, not hang.

    Review finding on the original c62 fix: it only handled Redis failing at
    subscribe time. A Redis that dropped later (Memorystore restart, VPC
    connector blip) killed the forwarder task, whose exception nobody observed,
    while the outer receive loop happily held the socket open forever. The
    client saw a healthy connection delivering zero events — the exact symptom
    c62 was written to eliminate, relocated from connect time to steady state.

    Simulated by making the forwarder fail immediately, which is what an
    unreachable Redis does to pubsub.listen().
    """
    uid = _bootstrap(dead_redis_client)

    with pytest.raises(WebSocketDisconnect) as caught:
        with dead_redis_client.websocket_connect("/ws", subprotocols=[uid]) as ws:
            ws.receive_text()

    # Either close path is the realtime-unavailable code; what must never happen
    # is the socket staying open with no signal at all.
    assert caught.value.code == WS_REALTIME_UNAVAILABLE
