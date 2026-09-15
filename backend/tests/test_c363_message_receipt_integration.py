"""Real local HTTP, WebSocket, Redis and SQL proof for the c363 instrument."""
from __future__ import annotations

import asyncio
import base64
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
from loadtest.message_receipts import run_workload
from loadtest.receipt_contract import parse_manifest
from loadtest.receipt_fixture import create_fixture
from loadtest.ws_resource_probe import local_server, require_loopback


def _redis_url() -> str:
    return require_loopback(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"))


def _redis_reachable() -> bool:
    # Match the exact client endpoint, and skip only a failed connection probe.
    # An available Redis with broken auth/protocol/publish behavior must fail.
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
async def receipt_broker(client, monkeypatch):
    from app.ws import pubsub

    broker = Redis.from_url(_redis_url(), decode_responses=True, socket_connect_timeout=1)
    try:
        await asyncio.wait_for(broker.ping(), 2)
        monkeypatch.setattr(pubsub, "_client", broker)
        yield broker
    finally:
        await asyncio.wait_for(broker.aclose(close_connection_pool=True), 2)


@needs_redis
async def test_instrument_correlates_real_message_fanout_during_http_mix(receipt_broker):
    from app import models
    from app.db import get_session_factory
    from app.main import create_app

    # receipt_broker depends on the existing client fixture: this process owns a
    # migrated, empty test database and its engine belongs to this event loop.
    async with get_session_factory()() as session:
        async with session.begin():
            raw = await create_fixture(session, 2)
    manifest = parse_manifest(json.dumps(raw))
    example = load_config(Path(__file__).parents[1] / "loadtest/message-receipts-config.yaml")

    # The real container transport runs on this same loop. No fake HTTP replies,
    # event publisher or receipt matcher stands between the driver and the app.
    async with local_server(create_app()) as (base, _server):
        config = replace(
            example, base_url=base.replace("ws://", "http://", 1), ws_url=base + "/ws",
            duration_seconds=2, ramp_in_seconds=0, think_seconds=.1,
            ws=replace(example.ws, max_sockets=2, connects_per_second=10, hold_seconds=2),
            # This is a bounded correctness check, not a latency benchmark.
            abort=replace(example.abort, read_p95_ceiling_ms=10000, write_p95_ceiling_ms=10000),
        )
        result = await asyncio.wait_for(
            run_workload(config, manifest, messages=1, interval_seconds=4, settle_seconds=1),
            timeout=20,
        )

    assert result["status"] == "LOCAL_RECEIPTS_OBSERVED"
    assert result["errors"] == []
    assert result["recipient_count"] == 2
    assert result["messages"] == {
        "requested": 1, "attempted": 1, "accepted": 1, "rejected": 0, "unconfirmed": 0,
    }
    receipts = result["receipts"]
    assert receipts["expected"] == receipts["unique"] == receipts["frames"] == 2
    assert all(receipts[key] == 0 for key in ("missing", "duplicates", "invalid", "unmatched", "late"))
    assert result["request_start_to_arrival_ms"]["samples"] == 2
    assert result["http_ready_ws_overlap"]["observed"] is True
    assert result["http_ready_ws_overlap"]["mix_2xx"] > 0
    assert result["receipt_during_http"]["observed"] is True
    assert result["receipt_during_http"]["unique_receipts_while_mix_active"] == 2

    # One actual stored message backs the report's accepted count. Full receipt
    # success also requires both real WS events to match its HTTP-returned ID.
    async with get_session_factory()() as session:
        message = (await session.execute(select(models.Message))).scalar_one()
        assert str(message.conversation_id) == raw["message_workload"]["conversation_id"]
        assert str(message.sender_device_id) == raw["message_workload"]["sender_device_id"]
        private_values = [
            str(message.id), str(message.conversation_id), str(message.sender_device_id),
            base64.b64encode(message.ciphertext).decode("ascii"), raw["campus_id"], raw["chapter_id"],
            *(value for user in raw["users"] for value in user.values()),
        ]
    aggregate = json.dumps(result)
    assert all(value not in aggregate for value in private_values)
    assert "production_capacity" in result["not_proven"]
    assert "device_decryption" in result["not_proven"]
