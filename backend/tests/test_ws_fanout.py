"""End-to-end WS + Redis fan-out (board c21): the core real-time path, previously unverified.

POST /conversations/{id}/messages -> publish_to_user() JSON-encodes an event and PUBLISHes
it to Redis channel `user:{recipient_id}` for every active member -> the /ws gateway, which
SUBSCRIBEd to that channel for the authenticated caller, forwards the raw JSON down the
socket verbatim.

Threading/event-loop note (read before touching this file): every test below drives BOTH
the HTTP calls and the websocket through one `starlette.testclient.TestClient`, entered
once per test via `with TestClient(create_app()) as client:` (see the `ws_client` fixture).
Starlette's TestClient always runs the ASGI app on a background thread with its own asyncio
event loop (an anyio "blocking portal"); inside a `with` block that portal is created once
and reused for every call made through it. app.db's engine and app.ws.pubsub's Redis client
are process-wide singletons bound to whichever event loop first touches them — mixing the
repo's usual async httpx.AsyncClient (bound to the pytest-asyncio loop) with a websocket
opened via TestClient (bound to the portal's own loop) in the same test would hand the same
asyncpg/redis connections to two different loops and intermittently blow up with "Future
attached to a different loop" errors. Routing every call in a test through the same
TestClient keeps the engine and Redis client on a single loop for that test's duration.

Redis pub/sub is fire-and-forget: SUBSCRIBE is itself a round trip to the server, and
gateway.py calls it *after* `websocket.accept()` has already unblocked the client's
handshake — so "the websocket_connect() call returned" does NOT mean "the subscription is
live". Publishing before it lands silently drops the event (no queue, no error). Every test
that expects to receive something calls `_wait_for_subscriber()` first, which polls Redis's
own `PUBSUB NUMSUB` for that channel instead of guessing with a blind sleep.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any

import anyio
import pytest
from sqlalchemy import text
from starlette.testclient import TestClient, WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from tests.conftest import b64


def _redis_reachable() -> bool:
    """True if a Redis is actually listening at settings.redis_url.

    Board c92. These three tests need a live Redis, and the two dev machines do not
    agree about having one: Q's Mac runs Docker and CI now starts a Redis service,
    while Jose's Mac has neither a redis-server binary nor Docker. Without this the
    file fails permanently on one machine and passes on the others, which is the
    corrosive kind of red — the sort a person learns to scroll past, with the next
    real failure hiding behind it.

    Deliberately a CONNECTION check and nothing more. A skip that triggered on any
    error would swallow the exact regression this file exists to catch: if Redis is
    up and fan-out is broken, these must still fail loudly.
    """
    import socket
    from urllib.parse import urlparse

    from app.config import get_settings

    parsed = urlparse(get_settings().redis_url)
    try:
        with socket.create_connection((parsed.hostname or "localhost", parsed.port or 6379), timeout=0.5):
            return True
    except OSError:
        return False


#: Applied per-test, NOT as a module-level pytestmark. Two tests in this file
#: (test_no_credentials_closes_4401, test_unknown_uid_closes_4401) exercise the
#: gateway's auth handshake and close before Redis is ever touched — they pass with
#: no Redis running, and a module-level skip would silently stop running them on
#: Jose's machine. Skipping more than necessary is its own way of losing coverage.
needs_redis = pytest.mark.skipif(
    not _redis_reachable(),
    reason="no Redis at settings.redis_url — see board c92 (brew install redis, or use CI)",
)


@dataclass
class WsUser:
    """A bootstrapped user plus the auth headers that act as them (emulated mode)."""

    id: str
    headers: dict[str, str]


# ---------------------------------------------------------------------------
# Fixture: one TestClient per test, HTTP + WS both funneled through its portal loop
# ---------------------------------------------------------------------------


@pytest.fixture
def ws_client(migrated_db: str) -> Any:
    """Sync TestClient wired to a freshly-truncated schema.

    Use this for every call in a WS test (HTTP *and* websocket) — see module docstring for
    why mixing it with the repo's usual async `client` fixture would race two event loops.
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

    asyncio.run(_truncate())
    # That asyncio.run() loop is now closed, so setup_engine's pooled connections are dead.
    # Drop the singletons again so the TestClient's portal below lazily builds fresh ones
    # bound to *its* loop on first use.
    _reset_singletons()

    with TestClient(create_app()) as client:
        yield client
        # Dispose whatever engine/redis client this test lazily created, on the same portal
        # loop that created them — disposing cross-loop would itself raise.
        if app_db._engine is not None:
            client.portal.call(app_db._engine.dispose)
        if app_pubsub._client is not None:
            client.portal.call(app_pubsub._client.aclose)

    _reset_singletons()


# ---------------------------------------------------------------------------
# API-driven setup helpers (mirrors tests/conftest.py's async versions, sync for TestClient)
# ---------------------------------------------------------------------------


def _make_user(client: TestClient, display_name: str) -> WsUser:
    uid = f"uid-{uuid.uuid4().hex}"
    headers = {"X-Debug-Firebase-Uid": uid}
    response = client.post(
        "/auth/bootstrap",
        json={
            "email": f"{uid}@example.edu",
            "display_name": display_name,
            "account_type": "greek",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return WsUser(id=response.json()["id"], headers=headers)


def _register_device(client: TestClient, user: WsUser) -> dict[str, Any]:
    body = {
        "device_label": "pytest-ws-device",
        "registration_id": 4242,
        "identity_key_b64": b64(b"identity-" + uuid.uuid4().bytes),
        "signed_prekey": {
            "key_id": 1,
            "public_key_b64": b64(b"spk-" + uuid.uuid4().bytes),
            "signature_b64": b64(b"sig-" + uuid.uuid4().bytes),
        },
        "one_time_prekeys": [{"key_id": 1, "public_key_b64": b64(b"otk-1")}],
    }
    response = client.post("/devices", json=body, headers=user.headers)
    assert response.status_code == 201, response.text
    return response.json()


def _share_verified_campus(client: TestClient, *users: WsUser) -> None:
    """Put these users on one verified campus so they may open a chapter-less DM (c243).

    tests/conftest.py's async share_verified_campus cannot be awaited from these sync
    TestClient tests. Run it through `client.portal.call` rather than a fresh
    asyncio.run(): the portal owns the loop app.db's engine singleton is bound to, and
    the module docstring's whole warning is about handing those connections to a second
    loop.
    """

    async def _write() -> None:
        from app.db import get_session_factory

        async with get_session_factory()() as session:
            result = await session.execute(
                text(
                    "INSERT INTO campuses (name, slug) VALUES (:name, :slug) RETURNING id"
                ),
                {"name": "Test Campus", "slug": f"campus-{uuid.uuid4().hex[:12]}"},
            )
            campus_id = str(result.scalar_one())
            for user in users:
                await session.execute(
                    text(
                        "UPDATE users SET campus_id = :campus, "
                        "campus_verified_at = now() WHERE id = :id"
                    ),
                    {"campus": campus_id, "id": user.id},
                )
            await session.commit()

    client.portal.call(_write)


def _make_dm(client: TestClient, creator: WsUser, other: WsUser) -> str:
    # c243: a chapter-less DM needs a reachable recipient. Fan-out, not authorization, is
    # what this file tests, so make the pair campus peers first.
    _share_verified_campus(client, creator, other)
    response = client.post(
        "/conversations",
        json={"kind": "dm", "member_user_ids": [other.id]},
        headers=creator.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _send_message(client: TestClient, conversation_id: str, sender: WsUser, device_id: str, payload: bytes) -> dict:
    response = client.post(
        f"/conversations/{conversation_id}/messages",
        json={"sender_device_id": device_id, "ciphertext_b64": b64(payload)},
        headers=sender.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# WS timing helpers
# ---------------------------------------------------------------------------


async def _wait_for_subscriber(channel: str, timeout: float = 2.0) -> None:
    """Poll Redis `PUBSUB NUMSUB` until >=1 subscriber is registered for `channel`.

    Explicit + bounded settle in place of a blind sleep (see module docstring): fast on a
    healthy system, and fails loudly instead of flakily if the subscribe never lands.
    """
    from app.ws.pubsub import get_redis

    redis = get_redis()
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while True:
        numsub = await redis.pubsub_numsub(channel)
        count = numsub[0][1] if numsub else 0
        if count >= 1:
            return
        if loop.time() >= deadline:
            raise AssertionError(f"no subscriber registered on {channel!r} within {timeout}s")
        await asyncio.sleep(0.02)


async def _recv_or_none_async(session: WebSocketTestSession, timeout: float) -> str | None:
    """Real cancellation (anyio.move_on_after), not an abandoned reader.

    An earlier version of this helper ran `session.receive_text()` in a background thread
    and, on timeout, just walked away from it (`pool.shutdown(wait=False)`). That thread
    stayed alive, still blocked reading the *same* underlying stream — so the next test
    step's own receive() call raced an orphaned zombie reader for whichever message arrived
    next, and intermittently lost it to the zombie. `move_on_after` instead cancels the
    pending `.receive()` in place when the timeout elapses, so nothing is left listening.
    """
    with anyio.move_on_after(timeout):
        message = await session._send_rx.receive()
        session._raise_on_close(message)
        return message["text"]
    return None


def _recv_or_none(session: WebSocketTestSession, timeout: float) -> str | None:
    """Bound receive: returns None on timeout instead of blocking forever if nothing
    ever arrives — a broken fan-out must fail/timeout the assertion, not hang the suite.
    Runs via the session's own portal so the timeout is real anyio cancellation, not a
    second, uncoordinated thread (see _recv_or_none_async's docstring).
    """
    return session.portal.call(_recv_or_none_async, session, timeout)


def _receive_text_required(session: WebSocketTestSession, timeout: float = 3.0) -> str:
    text_ = _recv_or_none(session, timeout)
    if text_ is None:
        raise AssertionError(f"no websocket message received within {timeout}s")
    return text_


# ---------------------------------------------------------------------------
# 1. Happy path — the core claim of board card c21
# ---------------------------------------------------------------------------


@needs_redis
def test_happy_path_recipient_receives_message_event(ws_client: TestClient) -> None:
    client = ws_client
    alice = _make_user(client, "Alice")
    bob = _make_user(client, "Bob")
    device = _register_device(client, alice)
    conversation_id = _make_dm(client, alice, bob)

    with client.websocket_connect("/ws", headers=bob.headers) as bob_ws:
        client.portal.call(_wait_for_subscriber, f"user:{bob.id}")

        sent = _send_message(client, conversation_id, alice, device["id"], b"hello bob")

        raw = _receive_text_required(bob_ws)

    event = json.loads(raw)
    assert event["type"] == "message"
    assert event["conversation_id"] == conversation_id
    assert event["message_id"] == sent["id"]
    assert event["ciphertext"] == sent["ciphertext_b64"]


# ---------------------------------------------------------------------------
# 2. Per-user isolation — the security-relevant one
# ---------------------------------------------------------------------------


@needs_redis
def test_non_member_receives_nothing(ws_client: TestClient) -> None:
    client = ws_client
    alice = _make_user(client, "Alice")
    bob = _make_user(client, "Bob")
    charlie = _make_user(client, "Charlie (not a member)")
    device = _register_device(client, alice)
    conversation_id = _make_dm(client, alice, bob)  # charlie is deliberately not invited

    with (
        client.websocket_connect("/ws", headers=bob.headers) as bob_ws,
        client.websocket_connect("/ws", headers=charlie.headers) as charlie_ws,
    ):
        client.portal.call(_wait_for_subscriber, f"user:{bob.id}")
        client.portal.call(_wait_for_subscriber, f"user:{charlie.id}")

        sent = _send_message(client, conversation_id, alice, device["id"], b"isolation payload")

        # Bob (an actual member) must get it — proves fan-out is happening at all, so the
        # negative assertion below means "isolated", not "everything is silently broken".
        raw = _receive_text_required(bob_ws)
        event = json.loads(raw)
        assert event["message_id"] == sent["id"]

        # Charlie (not a member) must get nothing on his own per-user channel.
        leaked = _recv_or_none(charlie_ws, timeout=1.0)
        assert leaked is None, (
            f"non-member received a fan-out event ({leaked!r}) — "
            "channel-per-user isolation is broken"
        )


# ---------------------------------------------------------------------------
# 3. Auth
# ---------------------------------------------------------------------------


def test_no_credentials_closes_4401(ws_client: TestClient) -> None:
    client = ws_client
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws"):
            pass
    assert exc_info.value.code == 4401


def test_unknown_uid_closes_4401(ws_client: TestClient) -> None:
    client = ws_client
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/ws", headers={"X-Debug-Firebase-Uid": "uid-does-not-exist"}
        ):
            pass
    assert exc_info.value.code == 4401


# ---------------------------------------------------------------------------
# 4. Disconnected recipient — document the real (lossy) behaviour, don't force it
# ---------------------------------------------------------------------------


@needs_redis
def test_message_published_while_recipient_offline_is_dropped_but_http_catchup_exists(
    ws_client: TestClient,
) -> None:
    """Redis pub/sub has NO persistence: an event published while the recipient has no
    socket open is simply lost, not queued. This test pins that as the documented, actual
    behaviour rather than something to work around. It also checks whether
    GET /conversations/{id}/messages lets a reconnecting client recover the dropped
    message — that HTTP catch-up path is what would make the WS gap acceptable.
    """
    client = ws_client
    alice = _make_user(client, "Alice")
    bob = _make_user(client, "Bob")
    device = _register_device(client, alice)
    conversation_id = _make_dm(client, alice, bob)

    # Bob has a live session, then it drops (backgrounded app, network blip, ...).
    with client.websocket_connect("/ws", headers=bob.headers):
        client.portal.call(_wait_for_subscriber, f"user:{bob.id}")
    # Exiting the `with` block synchronously drove the gateway's disconnect path
    # (unsubscribe + pubsub.aclose()), so Bob genuinely has no live channel now.

    # While Bob has no socket at all, Alice sends. Fire-and-forget: this publish has no
    # subscriber to reach and is simply dropped.
    missed = _send_message(client, conversation_id, alice, device["id"], b"missed while offline")

    # Bob reconnects.
    with client.websocket_connect("/ws", headers=bob.headers) as bob_ws:
        client.portal.call(_wait_for_subscriber, f"user:{bob.id}")

        missed_event = _recv_or_none(bob_ws, timeout=1.0)
        assert missed_event is None, (
            f"expected the offline-published event to be dropped, but the new socket "
            f"received {missed_event!r} — Redis pub/sub persistence behaviour changed"
        )

        # Prove the new connection's subscription itself is genuinely live (isolates the
        # assertion above from "the socket is just broken").
        live = _send_message(client, conversation_id, alice, device["id"], b"sent after reconnect")
        raw = _receive_text_required(bob_ws)
        event = json.loads(raw)
        assert event["message_id"] == live["id"]

    # Does the HTTP history endpoint let Bob recover what the socket dropped?
    history = client.get(f"/conversations/{conversation_id}/messages", headers=bob.headers)
    assert history.status_code == 200, history.text
    history_ids = {m["id"] for m in history.json()}
    assert missed["id"] in history_ids, (
        "GET /conversations/{id}/messages does not return the message dropped by the "
        "socket while Bob was offline — there is no catch-up path for the WS gap"
    )
