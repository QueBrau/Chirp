"""Real local reconnect and authenticated history recovery for the c363 tool."""
from __future__ import annotations

import asyncio
import base64
from collections import Counter
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
from urllib.parse import urlsplit

import pytest
from redis.asyncio import Redis
from sqlalchemy import select

from loadtest.config import load_config
from loadtest.message_recovery import run_recovery
from loadtest.receipt_contract import parse_manifest
from loadtest.receipt_fixture import create_fixture
from loadtest.ws_resource_probe import local_server, require_loopback


def _redis_url() -> str:
    return require_loopback(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"))


def _redis_reachable() -> bool:
    # The same fixed loopback host serves the probe and the real client. Only
    # connection refusal can skip; authentication/publish failures must fail.
    parsed = urlsplit(_redis_url())
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 6379), timeout=.5):
            return True
    except OSError:
        return False


needs_redis = pytest.mark.skipif(
    not _redis_reachable(),
    reason="no Redis at REDIS_URL/127.0.0.1:6379; CI requires Redis (c92/c394)",
)


@pytest.fixture
async def recovery_broker(client, monkeypatch):
    from app.ws import pubsub

    broker = Redis.from_url(_redis_url(), decode_responses=True, socket_connect_timeout=1)
    try:
        await asyncio.wait_for(broker.ping(), 2)
        monkeypatch.setattr(pubsub, "_client", broker)
        yield broker
    finally:
        await asyncio.wait_for(broker.aclose(close_connection_pool=True), 2)


class RecoveryTraffic:
    """Observe the actual ASGI exchanges without replacing or altering any IO."""

    def __init__(self, app, conversation_id):
        self.app = app
        self.path = f"/conversations/{conversation_id}/messages"
        self.active = set()
        self.ready = Counter()
        self.events = []
        self.requests = []

    async def __call__(self, scope, receive, send):
        uid = dict(scope.get("headers", [])).get(b"x-debug-firebase-uid", b"").decode()
        if scope["type"] == "websocket":
            async def observed_receive():
                event = await receive()
                if event["type"] == "websocket.disconnect":
                    self.active.discard(uid)
                return event

            async def observed_send(event):
                await send(event)
                if event["type"] == "websocket.send" and event.get("text"):
                    frame = json.loads(event["text"])
                    if frame == {"type": "ready"}:
                        self.active.add(uid)
                        self.ready[uid] += 1
                    elif frame.get("type") == "message":
                        self.events.append((uid, frame["message_id"]))

            try:
                await self.app(scope, observed_receive, observed_send)
            finally:
                self.active.discard(uid)
            return

        if scope["type"] != "http" or not scope["path"].startswith(self.path):
            await self.app(scope, receive, send)
            return
        observed = {
            "method": scope["method"], "uid": uid,
            "active": self.active.copy(), "ready": self.ready.copy(),
        }
        body = bytearray()

        async def observed_send(event):
            await send(event)
            if event["type"] == "http.response.start":
                observed["status"] = event["status"]
            elif event["type"] == "http.response.body":
                body.extend(event.get("body", b""))
                if not event.get("more_body", False):
                    observed["body"] = json.loads(body)
                    self.requests.append(observed)

        await self.app(scope, receive, observed_send)


@needs_redis
async def test_recovery_reconciles_real_offline_message_after_reconnect(recovery_broker):
    from app import models
    from app.db import get_session_factory
    from app.main import create_app

    # The client dependency owns a migrated per-process database and an engine
    # on this event loop. Redis is real; only its process-local client is scoped.
    async with get_session_factory()() as session:
        async with session.begin():
            raw = await create_fixture(session, 2)
    manifest = parse_manifest(json.dumps(raw))
    example = load_config(Path(__file__).parents[1] / "loadtest/message-receipts-config.yaml")
    traffic = RecoveryTraffic(create_app(), manifest.conversation_id)

    async with local_server(traffic) as (base, _server):
        config = replace(
            example, base_url=base.replace("ws://", "http://", 1), ws_url=base + "/ws",
            duration_seconds=16, ramp_in_seconds=0, think_seconds=.1,
            ws=replace(example.ws, max_sockets=2, connects_per_second=10, hold_seconds=16),
            # Correctness evidence only; CI scheduling is not a latency benchmark.
            abort=replace(example.abort, read_p95_ceiling_ms=10000, write_p95_ceiling_ms=10000),
        )
        result = await asyncio.wait_for(
            run_recovery(config, manifest, interval_seconds=4, settle_seconds=1), timeout=35,
        )

    assert result["status"] == "LOCAL_RECONNECT_RECOVERY_OBSERVED"
    assert result["errors"] == []
    assert result["messages"] == {
        "requested": 3, "attempted": 3, "accepted": 3, "rejected": 0, "unconfirmed": 0,
    }
    assert result["live_receipts"]["expected"] == result["live_receipts"]["unique"] == 4
    assert result["live_receipts"]["missing"] == 0
    assert result["history_recovery"]["expected"] == result["history_recovery"]["unique"] == 2
    assert result["history_recovery"]["missing"] == 0
    assert all(result["phases"][phase] is True for phase in (
        "initial_live", "recipients_offline", "reconnected_ready", "history_reconciled", "final_live",
    ))

    recipients = set(raw["message_workload"]["recipient_uids"])
    assert [request["method"] for request in traffic.requests] == ["POST", "POST", "GET", "GET", "POST"]
    posts = [request for request in traffic.requests if request["method"] == "POST"]
    assert len(posts) == 3
    assert all(request["status"] == 201 for request in posts)
    assert all(request["uid"] == raw["message_workload"]["sender_uid"] for request in posts)
    assert [request["active"] for request in posts] == [recipients, set(), recipients]
    message_ids = [request["body"]["id"] for request in posts]
    assert len(set(message_ids)) == 3
    assert traffic.ready == Counter({uid: 2 for uid in recipients})
    assert Counter(traffic.events) == Counter(
        (uid, message_id) for uid in recipients for message_id in (message_ids[0], message_ids[2])
    )
    history = [request for request in traffic.requests if request["method"] == "GET"]
    assert Counter(request["uid"] for request in history) == Counter({uid: 1 for uid in recipients})
    for request in history:
        assert request["status"] == 200
        assert request["active"] == recipients
        assert request["ready"] == Counter({uid: 2 for uid in recipients})
        recovered = [message for message in request["body"] if message["id"] == message_ids[1]]
        assert recovered == [posts[1]["body"]]

    async with get_session_factory()() as session:
        messages = (await session.execute(select(models.Message))).scalars().all()
        assert {str(message.id) for message in messages} == set(message_ids)
        assert all(str(message.conversation_id) == manifest.conversation_id for message in messages)
        assert all(str(message.sender_device_id) == manifest.sender_device_id for message in messages)
        private_values = [
            *message_ids, manifest.conversation_id, manifest.sender_device_id,
            raw["campus_id"], raw["chapter_id"],
            *(base64.b64encode(message.ciphertext).decode("ascii") for message in messages),
            *(value for user in raw["users"] for value in user.values()),
        ]
    aggregate = json.dumps(result)
    assert all(value not in aggregate for value in private_values)
    assert "production_capacity" in result["not_proven"]
    assert "device_decryption" in result["not_proven"]
