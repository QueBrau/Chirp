"""A real loopback Redis effect with an ambiguous caller result stays blocking."""
from __future__ import annotations

import asyncio
import json
import uuid
from urllib.parse import urlparse

from redis.asyncio import Redis

from app.config import get_settings
from app.db import get_engine
from app.services import outbox
from app.services.account_external_operations import inspect_registered_operation_drain
from tests.test_c436_delivery_admission import (
    _activate, _attempts, _pending_row, _request_deletion, _seed_outbox,
)


async def test_actual_publish_followed_by_lost_result_is_not_replayed_or_drained(
    client, make_user, monkeypatch,
):
    settings = get_settings()
    monkeypatch.setattr(settings, "account_deletion_delivery_admission_enabled", True)
    monkeypatch.setattr(settings, "outbox_dispatch_timeout_s", 5)
    assert urlparse(settings.redis_url).hostname in {"localhost", "127.0.0.1", "::1"}
    target = await make_user("Real Redis deletion target")
    target_id = uuid.UUID(target.id)
    request_id = await _request_deletion(client, target)
    event = {"type": "poll", "poll_id": str(uuid.uuid4()), "snapshot": "actual-redis"}
    row_id = await _seed_outbox([target_id], event=event)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    channel = f"user:{target_id}"
    subscription = redis.pubsub()
    calls = []
    try:
        await asyncio.wait_for(redis.ping(), 5)
        await subscription.subscribe(channel)
        async with asyncio.timeout(5):
            while True:
                acknowledgement = await subscription.get_message(timeout=.1)
                if acknowledgement and acknowledgement["type"] == "subscribe":
                    break

        async def publish_then_lose_result(recipients, payload, sender_id):
            assert recipients == [target_id]
            assert get_engine().pool.checkedout() == 0
            calls.append(recipients[0])
            assert await redis.publish(channel, json.dumps(payload)) >= 1
            # The dispatcher cannot distinguish a transport failure before the
            # write from one after broker acceptance. Never replay it blindly.
            raise RuntimeError("controlled post-effect result loss")

        monkeypatch.setitem(outbox._dispatchers, "poll", publish_then_lose_result)
        stats = await outbox.dispatch_pending(limit=1)
        assert stats.uncertain == 1 and stats.delivered == 0
        async with asyncio.timeout(5):
            while True:
                message = await subscription.get_message(ignore_subscribe_messages=True, timeout=.1)
                if message is not None:
                    assert json.loads(message["data"]) == event
                    break
        assert await _pending_row(row_id) is None
        assert [attempt.state for attempt in await _attempts(user_id=target_id)] == ["uncertain"]
        await outbox.dispatch_pending(limit=1)
        assert calls == [target_id]
        await _activate(target_id, request_id)
        drain = await inspect_registered_operation_drain(user_id=target_id, request_id=request_id)
        assert drain.uncertain_count == 1 and not drain.registered_operations_drained
        assert await subscription.get_message(ignore_subscribe_messages=True, timeout=.1) is None
    finally:
        await subscription.aclose()
        await redis.aclose(close_connection_pool=True)
