"""A suspended account cannot open a new WS connection (board c126).

The HTTP precedent is middleware/auth.py's get_current_user: `if user.suspended_at
is not None: raise 403`, checked once at the one place every router's
current-user dependency passes through. ws/gateway.py's websocket_gateway
resolved the user at connect time and never made this check — a suspended
account could still open a fresh realtime connection, matching the HTTP path in
every other respect except the one check that route exists for.

Same TestClient/portal-loop discipline as test_ws_fanout.py: everything in a
test goes through one sync TestClient so the engine and Redis client stay on a
single event loop. Not marked needs_redis — like test_no_credentials_closes_4401,
the suspension check runs and closes the socket before pubsub.subscribe is ever
reached, so this passes with no Redis running.
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
    from a sibling test module aren't a supported import target, and this file's
    only need is the same reset-singletons-then-TestClient shape."""
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


def _grant_platform_admin(client: TestClient, user_id: str) -> None:
    """Flip is_platform_admin directly in the DB, synchronously via the portal —
    mirrors test_moderation_suspension.py's async helper; suspending needs an admin
    and there is no API that grants the flag (by design, board c28)."""

    async def _grant() -> None:
        import app.db as app_db

        async with app_db.get_session_factory()() as session:
            await session.execute(
                text("UPDATE users SET is_platform_admin = true WHERE id = :id"),
                {"id": user_id},
            )
            await session.commit()

    client.portal.call(_grant)


def test_a_suspended_account_is_refused_at_connect(ws_client: TestClient) -> None:
    client = ws_client
    admin = _make_user(client, "Platform Admin")
    _grant_platform_admin(client, admin.id)
    target = _make_user(client, "Rule Breaker")

    suspend = client.post(
        f"/moderation/users/{target.id}/suspend",
        json={"reason": "harassment"},
        headers=admin.headers,
    )
    assert suspend.status_code == 200, suspend.text

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws", headers=target.headers):
            pass
    assert exc_info.value.code == 4403


def test_an_unsuspended_account_still_connects(ws_client: TestClient) -> None:
    """The negative case matters as much as the positive one — a check this blunt
    could easily refuse everyone. Same admin/target shape as the suspended test,
    but no suspend call, so the only thing distinguishing them is the field this
    check actually reads."""
    client = ws_client
    admin = _make_user(client, "Platform Admin")
    _grant_platform_admin(client, admin.id)
    target = _make_user(client, "Ordinary Member")

    with client.websocket_connect("/ws", headers=target.headers) as ws:
        assert ws is not None


def test_unsuspending_restores_the_ability_to_connect(ws_client: TestClient) -> None:
    """Suspension is not a one-way door — c76's unsuspend route clears
    suspended_at back to NULL, and this gateway reads that column fresh on every
    connection attempt, not a cached verdict from an earlier check."""
    client = ws_client
    admin = _make_user(client, "Platform Admin")
    _grant_platform_admin(client, admin.id)
    target = _make_user(client, "Reformed Member")

    suspend = client.post(
        f"/moderation/users/{target.id}/suspend",
        json={"reason": "cooldown"},
        headers=admin.headers,
    )
    assert suspend.status_code == 200, suspend.text

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws", headers=target.headers):
            pass
    assert exc_info.value.code == 4403

    unsuspend = client.post(
        f"/moderation/users/{target.id}/unsuspend",
        json={"reason": "appeal granted"},
        headers=admin.headers,
    )
    assert unsuspend.status_code == 200, unsuspend.text

    with client.websocket_connect("/ws", headers=target.headers) as ws:
        assert ws is not None
