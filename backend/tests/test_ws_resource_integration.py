"""Real local Redis, PostgreSQL and network WebSockets; bounded fixture traffic."""
from __future__ import annotations

import asyncio
import json
import os
import socket as native_socket
import statistics
import time

from fastapi import FastAPI, WebSocket
import pytest
from redis.asyncio import Redis
from sqlalchemy import event, text
import websockets
from websockets.exceptions import ConnectionClosed

from app.config import get_settings
from app.ws import gateway, pubsub
from loadtest.ws_resource_probe import (
    container_transport_options, local_server, process_rss_bytes,
    record_result, redis_totals, require_loopback,
)
from tests.conftest import b64, share_verified_campus


def _redis_reachable() -> bool:
    """True if a Redis is actually listening at the address the `broker` fixture uses.

    Board c92 established the pattern (see tests/test_ws_fanout.py); board c394 applies
    it here. Copied rather than imported because this file's fixture targets REDIS_URL
    (falling back to 127.0.0.1:6379), not settings.redis_url as test_ws_fanout.py does —
    the two addresses can disagree, so the reachability check must match the fixture it
    guards, not the app's own default.

    Deliberately a CONNECTION check and nothing more. A skip that triggered on any error
    would swallow the exact regression these tests exist to catch: if Redis is up and
    something is actually broken, these must still fail loudly.
    """
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"))
    try:
        with socket.create_connection((parsed.hostname or "127.0.0.1", parsed.port or 6379), timeout=0.5):
            return True
    except OSError:
        return False


#: Applied per-test, NOT as a module-level pytestmark — see test_ws_fanout.py's
#: needs_redis for why: the other tests in this file pass with no Redis running, and a
#: module-level skip would silently stop running them on a Redis-less machine.
needs_redis = pytest.mark.skipif(
    not _redis_reachable(),
    reason="no Redis at REDIS_URL/127.0.0.1:6379 — see boards c92, c394 (brew install redis, or use CI)",
)


@pytest.fixture(autouse=True)
def fixed_pool(monkeypatch):
    monkeypatch.setenv("DB_POOL_SIZE", "1")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "0")
    monkeypatch.setenv("DB_POOL_TIMEOUT", "1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def broker(monkeypatch):
    url = require_loopback(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"))
    redis = Redis.from_url(url, decode_responses=True, socket_connect_timeout=1)
    await asyncio.wait_for(redis.ping(), 2)
    monkeypatch.setattr(pubsub, "_client", redis)
    try:
        yield redis
    finally:
        await asyncio.wait_for(redis.aclose(close_connection_pool=True), 2)


async def connected(base, uid):
    socket = await websockets.connect(base + "/ws", subprotocols=[uid], open_timeout=3, close_timeout=.2, max_size=256 * 1024, proxy=None)
    try:
        assert json.loads(await asyncio.wait_for(socket.recv(), 3)) == {"type": "ready"}
        return socket
    except BaseException:
        await socket.close()
        raise


async def no_subscribers(broker):
    async with asyncio.timeout(3):
        while (await redis_totals(broker))["redis_pubsub_clients"]:
            await asyncio.sleep(.01)


def test_real_container_cli_selects_effective_frame_limit():
    assert container_transport_options() == {
        "ws": "websockets-sansio", "ws_max_size": 1024, "ws_per_message_deflate": False,
    }


@pytest.mark.parametrize("payload", ["x" * 1025, "€" * 342, ["a" * 600, "b" * 425]])
async def test_actual_transport_rejects_oversized_message(payload):
    app = FastAPI()

    @app.websocket("/echo")
    async def echo(ws: WebSocket):
        await ws.accept()
        try:
            while True: await ws.send_text(await ws.receive_text())
        except Exception:
            pass

    async with local_server(app) as (base, server):
        async with websockets.connect(base + "/echo", close_timeout=.2, proxy=None) as socket:
            transport_name = type(next(iter(server.server_state.connections))).__name__
            assert socket.protocol.extensions == []
            await socket.send("a" * 1024)
            assert await asyncio.wait_for(socket.recv(), 1) == "a" * 1024
            pong = await socket.ping()
            await asyncio.wait_for(pong, 1)
            # Whole-message UTF-8/fragmented length, not just characters/frames.
            await socket.send(payload)
            with pytest.raises(ConnectionClosed) as caught:
                await asyncio.wait_for(socket.recv(), 1)
            assert caught.value.rcvd.code == 1009
            record_result({"case": "inbound_limit", "limit_bytes": 1024, "transport": transport_name})


async def test_actual_paused_transport_close_attempt_returns(monkeypatch):
    app, close_now, returned = FastAPI(), asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(gateway, "WS_CLOSE_SECONDS", .04)

    @app.websocket("/close")
    async def close(ws: WebSocket):
        await ws.accept()
        await close_now.wait()
        await gateway._bounded_close(ws, 4503)
        returned.set()

    async with local_server(app) as (base, server):
        async with websockets.connect(base + "/close", close_timeout=.2, proxy=None) as socket:
            protocol = next(iter(server.server_state.connections))
            protocol.writable.clear()  # actual ASGI adapter flow-control wait
            start = time.monotonic()
            close_now.set()
            await asyncio.wait_for(returned.wait(), .3)
            with pytest.raises(ConnectionClosed):
                await asyncio.wait_for(socket.recv(), .3)
            record_result({"case": "paused_close", "attempt_budget_ms": 40, "return_and_disconnect_ms": (time.monotonic() - start) * 1000})


@needs_redis
async def test_churn_delivery_sql_and_memory(client, make_user, broker, monkeypatch):
    from app.db import get_engine
    from app.main import create_app

    count = int(os.environ.get("CHIRP_WS_SOCKETS", "16"))
    rounds = int(os.environ.get("CHIRP_WS_ROUNDS", "3"))
    hold = float(os.environ.get("CHIRP_WS_HOLD_SECONDS", ".12"))
    assert 1 <= count <= 32 and 1 <= rounds <= 5 and 0 < hold <= 60
    if "CHIRP_WS_HOLD_SECONDS" not in os.environ:
        monkeypatch.setattr(gateway, "WS_SUSPENSION_POLL_SECONDS", .04)
        monkeypatch.setattr(gateway, "WS_SUSPENSION_POLL_JITTER", 0)
    user = await make_user()
    engine = get_engine().sync_engine
    assert engine.pool.checkedout() == 0
    owned_connections = set()
    metrics = {"checked_out": 0, "max_checked_out": 0, "user_selects": 0}

    def checkout(connection, record, proxy):
        owned_connections.add(id(record))
        metrics["checked_out"] = len(owned_connections)
        metrics["max_checked_out"] = max(metrics["max_checked_out"], metrics["checked_out"])
    def checkin(connection, record):
        # Pool invalidation/failed checkout may check in a record for which a
        # successful checkout event was never emitted. Track actual ownership.
        owned_connections.discard(id(record))
        metrics["checked_out"] = len(owned_connections)
    def query(conn, cursor, statement, params, context, many):
        if statement.lstrip().upper().startswith("SELECT") and "users" in statement:
            metrics["user_selects"] += 1

    event.listen(engine, "checkout", checkout)
    event.listen(engine, "checkin", checkin)
    event.listen(engine, "before_cursor_execute", query)
    baseline = await redis_totals(broker)
    try:
        async with local_server(create_app()) as (base, server):
            for round_no in range(rounds):
                sockets = []
                try:
                    connect_started = time.monotonic()
                    outcomes = await asyncio.gather(*(connected(base, user.firebase_uid) for _ in range(count)), return_exceptions=True)
                    sockets = [value for value in outcomes if not isinstance(value, BaseException)]
                    if len(sockets) != count:
                        record_result({"case": "connect_failure", "connected": len(sockets), "attempted": count,
                            "elapsed_ms": (time.monotonic() - connect_started) * 1000, **metrics})
                    for value in outcomes:
                        if isinstance(value, BaseException): raise value
                    opened = await redis_totals(broker)
                    assert opened["redis_pubsub_clients"] - baseline["redis_pubsub_clients"] == count
                    start = time.perf_counter_ns()
                    await pubsub.publish_to_user(user.id, {"type": "probe", "sequence": round_no})
                    arrivals = []
                    async def receive(socket):
                        value = json.loads(await asyncio.wait_for(socket.recv(), 2))
                        arrivals.append((time.perf_counter_ns() - start) / 1e6)
                        assert value == {"type": "probe", "sequence": round_no}
                    await asyncio.gather(*(receive(socket) for socket in sockets))
                    before_sql = metrics["user_selects"]
                    await asyncio.sleep(hold if round_no == 0 else .12)
                    periodic = metrics["user_selects"] - before_sql
                    result = await client.get("/auth/me", headers=user.headers)
                    assert result.status_code == 200
                    assert metrics["max_checked_out"] == 1
                    record_result({
                        "case": "churn", "round": round_no + 1, "sockets": count,
                        "poll_seconds": gateway.WS_SUSPENSION_POLL_SECONDS,
                        "hold_seconds": hold if round_no == 0 else .12, "periodic_user_selects": periodic,
                        "delivery_count": len(arrivals), "delivery_median_ms": statistics.median(arrivals),
                        "delivery_max_ms": max(arrivals), "max_db_checked_out": metrics["max_checked_out"],
                        "process_rss_bytes": process_rss_bytes(),
                        **opened,
                    })
                finally:
                    await asyncio.gather(*(socket.close() for socket in sockets))
                await no_subscribers(broker)
                assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("ws-") and not t.done()]
                assert metrics["checked_out"] == 0
                assert engine.pool.checkedout() == 0
                record_result({"case": "churn_released", "round": round_no + 1, **(await redis_totals(broker)), "process_rss_bytes": process_rss_bytes()})
    finally:
        event.remove(engine, "checkout", checkout)
        event.remove(engine, "checkin", checkin)
        event.remove(engine, "before_cursor_execute", query)


@needs_redis
async def test_live_suspension_unsuspend_and_broker_churn(client, make_user, broker, monkeypatch):
    from app.db import get_session_factory
    from app.main import create_app
    monkeypatch.setattr(gateway, "WS_SUSPENSION_POLL_SECONDS", .04)
    monkeypatch.setattr(gateway, "WS_SUSPENSION_POLL_JITTER", 0)
    user = await make_user()
    async with local_server(create_app()) as (base, server):
        socket = await connected(base, user.firebase_uid)
        start = time.monotonic()
        async with get_session_factory()() as session:
            await session.execute(text("UPDATE users SET suspended_at=now() WHERE id=:id"), {"id": user.id})
            await session.commit()
        with pytest.raises(ConnectionClosed) as suspended:
            await asyncio.wait_for(socket.recv(), 1)
        assert suspended.value.rcvd.code == 4403
        await socket.close()
        await no_subscribers(broker)

        record_result({"case": "live_suspension", "poll_seconds": .04, "close_code": 4403, "latency_ms": (time.monotonic() - start) * 1000})
        async with get_session_factory()() as session:
            await session.execute(text("UPDATE users SET suspended_at=NULL WHERE id=:id"), {"id": user.id})
            await session.commit()
        socket = await connected(base, user.firebase_uid)
        owned = [row for row in await broker.client_list() if "P" in row["flags"]]
        assert len(owned) == 1
        # Kill exactly this fixture's dedicated subscription, not the Redis server.
        await broker.execute_command("CLIENT", "KILL", "ID", owned[0]["id"])
        with pytest.raises(ConnectionClosed) as unavailable:
            await asyncio.wait_for(socket.recv(), 3)
        assert unavailable.value.rcvd.code == 4503
        await socket.close()
        await no_subscribers(broker)

@needs_redis
async def test_actual_slow_reader_is_released_without_starving_peer(client, make_user, broker, caplog):
    from app.db import get_engine
    from app.main import create_app
    user = await make_user()
    async with local_server(create_app()) as (base, server):
        slow, fast = await connected(base, user.firebase_uid), await connected(base, user.firebase_uid)
        slow_port = slow.transport.get_extra_info("sockname")[1]
        protocol = next(p for p in server.server_state.connections if p.client[1] == slow_port)
        # Fixed LOCAL transport budgets force backpressure with <=4MiB traffic.
        # These are probe conditions, not asserted Cloud Run/kernel settings.
        protocol.transport.set_write_buffer_limits(high=4096, low=1024)
        protocol.transport.get_extra_info("socket").setsockopt(native_socket.SOL_SOCKET, native_socket.SO_SNDBUF, 4096)
        slow.transport.get_extra_info("socket").setsockopt(native_socket.SOL_SOCKET, native_socket.SO_RCVBUF, 1024)
        slow.transport.pause_reading()
        delivered, max_output, max_transport = 0, 0, 0
        started = time.monotonic()
        try:
            async with asyncio.timeout(12):
                for sequence in range(64):
                    await pubsub.publish_to_user(user.id, {"type": "probe", "sequence": sequence, "padding": "x" * (60 * 1024)})
                    value = json.loads(await asyncio.wait_for(fast.recv(), 2))
                    assert value["sequence"] == sequence
                    delivered += 1
                    totals = await redis_totals(broker)
                    max_output = max(max_output, totals["redis_pubsub_output_bytes"])
                    max_transport = max(max_transport, protocol.transport.get_write_buffer_size())
                    if totals["redis_pubsub_clients"] == 1:
                        break
                    await asyncio.sleep(.01)
                else:
                    # Publisher stops after its fixed byte budget. Give the
                    # existing5s send/age limit and1s close attempt time to act.
                    while (await redis_totals(broker))["redis_pubsub_clients"] != 1:
                        await asyncio.sleep(.02)
            # Identify the survivor with a fresh event AFTER the count dropped;
            # a count of one alone cannot distinguish the healthy peer.
            await pubsub.publish_to_user(user.id, {"type": "probe", "survivor": True})
            assert json.loads(await asyncio.wait_for(fast.recv(), 2)) == {"type": "probe", "survivor": True}
            result = await client.get("/auth/me", headers=user.headers)
            assert result.status_code == 200
            assert get_engine().pool.checkedout() == 0
            warnings = [r for r in caplog.records if hasattr(r, "ws_peak_frames")]
            assert warnings
            assert max(r.ws_peak_frames for r in warnings) <= gateway.WS_QUEUE_MAX_FRAMES
            assert max(r.ws_peak_bytes for r in warnings) <= gateway.WS_QUEUE_MAX_BYTES
            record_result({
                "case": "slow_reader", "fast_peer_delivered": delivered,
                "published_payload_bytes_upper_bound": delivered * (61 * 1024),
                "release_latency_ms": (time.monotonic() - started) * 1000,
                "peak_app_frames": max(r.ws_peak_frames for r in warnings),
                "peak_app_bytes": max(r.ws_peak_bytes for r in warnings),
                "max_redis_pubsub_output_bytes": max_output, "max_transport_write_bytes": max_transport,
                "remaining_pubsub_clients": (await redis_totals(broker))["redis_pubsub_clients"],
            })
        finally:
            slow.transport.resume_reading()
            await asyncio.gather(slow.close(), fast.close())
        await no_subscribers(broker)



@needs_redis
async def test_disconnect_resume_preserves_durable_message_catchup(client, make_user, broker):
    from app.main import create_app
    sender, recipient = await make_user(), await make_user()
    await share_verified_campus(sender.id, recipient.id)
    device = await client.post("/devices", headers=sender.headers, json={
        "registration_id": 1, "identity_key_b64": b64(b"i" * 32),
        "signed_prekey": {"key_id": 1, "public_key_b64": b64(b"p" * 32), "signature_b64": b64(b"s" * 64)},
        "one_time_prekeys": [],
    })
    assert device.status_code == 201
    created = await client.post("/conversations", headers=sender.headers, json={"kind": "dm", "member_user_ids": [recipient.id]})
    assert created.status_code == 201
    conversation_id = created.json()["id"]
    async with local_server(create_app()) as (base, server):
        first = await connected(base, recipient.firebase_uid)
        await first.close()
        await no_subscribers(broker)
        response = await client.post(f"/conversations/{conversation_id}/messages", headers=sender.headers,
            json={"sender_device_id": device.json()["id"], "ciphertext_b64": b64(b"opaque-offline-fixture")})
        assert response.status_code == 201
        missed = response.json()["id"]
        resumed = await connected(base, recipient.firebase_uid)
        try:
            subscriptions = await broker.pubsub_numsub(f"user:{recipient.id}")
            assert subscriptions[0][1] == 1  # ready really follows broker ACK
            history = await client.get(f"/conversations/{conversation_id}/messages", headers=recipient.headers)
            assert history.status_code == 200
            assert missed in {row["id"] for row in history.json()}
            response = await client.post(f"/conversations/{conversation_id}/messages", headers=sender.headers,
                json={"sender_device_id": device.json()["id"], "ciphertext_b64": b64(b"opaque-online-fixture")})
            assert response.status_code == 201
            event = json.loads(await asyncio.wait_for(resumed.recv(), 2))
            assert event["message_id"] == response.json()["id"]
        finally:
            await resumed.close()
        await no_subscribers(broker)
