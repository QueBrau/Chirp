"""WS auth moved off the query string onto the Sec-WebSocket-Protocol subprotocol
(security-pass item 7, ~Aug 22).

The handshake used to authenticate via `?token=<id-token>` in the URL, because
RN's WebSocket constructor cannot set arbitrary headers. Cloud Run logs
`httpRequest.requestUrl` itself, at the platform layer — outside any redaction
the app could install (the filter that existed covered uvicorn's OWN access log
only, never Cloud Run's). The query string is gone entirely rather than
deprecated: zero real traffic ever depended on it (nothing called
`chirpSocket.connect()` client-side until board c63), so there was no live
client to preserve compatibility for.

Same TestClient/portal-loop discipline as test_ws_fanout.py and
test_ws_suspension.py — everything in a test goes through one sync TestClient
so app.db's engine and app.ws.pubsub's Redis client stay on a single event
loop. Not marked needs_redis: every case here closes (or fails to authenticate)
before pubsub.subscribe is ever reached, same as the existing 4401 tests.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import text
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


@pytest.fixture
def ws_client(migrated_db: str) -> Any:
    """Duplicated from test_ws_fanout.py rather than imported — pytest fixtures
    from a sibling test module aren't a supported import target."""
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
        import asyncio

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


@dataclass
class WsUser:
    id: str
    headers: dict[str, str]
    uid: str


def _make_user(client: TestClient, display_name: str) -> WsUser:
    uid = f"uid-{uuid.uuid4().hex}"
    headers = {"X-Debug-Firebase-Uid": uid}
    response = client.post(
        "/auth/bootstrap",
        json={"email": f"{uid}@example.edu", "display_name": display_name, "account_type": "greek"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return WsUser(id=response.json()["id"], headers=headers, uid=uid)


def _grant_platform_admin(client: TestClient, user_id: str) -> None:
    async def _grant() -> None:
        import app.db as app_db

        async with app_db.get_session_factory()() as session:
            await session.execute(
                text("UPDATE users SET is_platform_admin = true WHERE id = :id"),
                {"id": user_id},
            )
            await session.commit()

    client.portal.call(_grant)


def test_the_old_query_string_path_no_longer_authenticates_anyone(
    ws_client: TestClient,
) -> None:
    """The regression this migration exists to prevent: a `?token=` handshake
    used to be the PRIMARY path in firebase mode and a working fallback in
    emulated mode. Proving it is gone rather than assuming the code review
    caught every reader of `websocket.query_params` — connect with a real,
    valid debug uid on the query string (the exact shape every request line
    used to carry into Cloud Run's own logging) and confirm it now 401s exactly
    like an unknown or absent credential would.
    """
    user = _make_user(ws_client, "Old Path Caller")
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with ws_client.websocket_connect(f"/ws?token={user.uid}"):
            pass
    assert exc_info.value.code == 4401


def test_subprotocol_carrying_the_debug_uid_authenticates_in_emulated_mode(
    ws_client: TestClient,
) -> None:
    """The new primary path for a caller that cannot set headers, mirrored in
    emulated mode: offer the debug uid as the subprotocol instead of the
    X-Debug-Firebase-Uid header, and it resolves the same user."""
    user = _make_user(ws_client, "Subprotocol Auth")
    with ws_client.websocket_connect("/ws", subprotocols=[user.uid]) as ws:
        assert ws is not None


def test_an_unknown_subprotocol_value_still_closes_4401(ws_client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with ws_client.websocket_connect("/ws", subprotocols=["uid-does-not-exist"]):
            pass
    assert exc_info.value.code == 4401


def test_no_subprotocol_and_no_header_still_closes_4401(ws_client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with ws_client.websocket_connect("/ws"):
            pass
    assert exc_info.value.code == 4401


def test_the_header_path_still_works_unaffected(ws_client: TestClient) -> None:
    """Every other WS test in this repo authenticates via the header, not a
    subprotocol — this is the regression guard for all of them at once."""
    user = _make_user(ws_client, "Header Auth")
    with ws_client.websocket_connect("/ws", headers=user.headers) as ws:
        assert ws is not None


def test_header_takes_priority_over_a_conflicting_subprotocol_in_emulated_mode(
    ws_client: TestClient,
) -> None:
    """_resolve_uid's emulated branch is `header or protocol_token` — the header
    wins if both are present. Proven, not assumed: offer a real user's uid as
    the header and a nonexistent one as the subprotocol; if the subprotocol won,
    this would 401."""
    user = _make_user(ws_client, "Header Priority")
    with ws_client.websocket_connect(
        "/ws", headers=user.headers, subprotocols=["uid-does-not-exist"]
    ) as ws:
        assert ws is not None


def test_a_suspended_account_still_closes_4403_via_subprotocol_auth(
    ws_client: TestClient,
) -> None:
    """c126's suspension check runs after uid resolution regardless of which
    path resolved it — proving that against the NEW path, not just the old one."""
    admin = _make_user(ws_client, "Platform Admin")
    _grant_platform_admin(ws_client, admin.id)
    target = _make_user(ws_client, "Rule Breaker")

    suspend = ws_client.post(
        f"/moderation/users/{target.id}/suspend",
        json={"reason": "harassment"},
        headers=admin.headers,
    )
    assert suspend.status_code == 200, suspend.text

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with ws_client.websocket_connect("/ws", subprotocols=[target.uid]):
            pass
    assert exc_info.value.code == 4403
