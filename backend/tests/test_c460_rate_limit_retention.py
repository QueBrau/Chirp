"""c460: real Redis buckets and bounded local fallback retention."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from types import SimpleNamespace
import uuid
from urllib.parse import urlparse

import pytest
from redis.asyncio import Redis

from app.config import Settings
from app.services import rate_limit


def _redis_url() -> str:
    return os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")


def _redis_reachable() -> bool:
    parsed = urlparse(_redis_url())
    try:
        with socket.create_connection((parsed.hostname or "127.0.0.1", parsed.port or 6379), timeout=.5):
            return True
    except OSError:
        return False


needs_redis = pytest.mark.skipif(not _redis_reachable(), reason="local Redis required for c460 shared-bucket tests")


async def _stable_bucket(window: int) -> int:
    """Start with enough time before rollover for one bounded Redis assertion."""
    while True:
        now = time.time()
        bucket = int(now // window)
        remaining = (bucket + 1) * window - now
        if remaining > 2:
            return bucket
        await asyncio.sleep(remaining + .05)


@pytest.fixture
async def redis_backend(monkeypatch):
    """Drive the production branch of allow() against a real, isolated Redis keyspace."""
    from app import config
    from app.ws import pubsub

    url = _redis_url()
    parsed = urlparse(url)
    assert parsed.hostname in {"localhost", "127.0.0.1", "::1"}, "c460 tests must use loopback Redis only"
    settings = Settings(_env_file=None, env="production", redis_url=url)
    client = Redis.from_url(url, decode_responses=True, socket_connect_timeout=1)
    await asyncio.wait_for(client.ping(), 2)
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(pubsub, "get_redis", lambda: client)
    prefix = f"c460-test:{uuid.uuid4().hex}"
    rate_limit._reset_all()
    try:
        yield client, prefix
    finally:
        keys = [key async for key in client.scan_iter(match=f"chirp:ratelimit:{prefix}:*")]
        if keys:
            await client.delete(*keys)
        await client.aclose(close_connection_pool=True)
        rate_limit._reset_all()


@needs_redis
async def test_real_redis_counter_is_atomic_and_expiry_does_not_slide(redis_backend):
    redis, prefix = redis_backend
    key = f"{prefix}:atomic"
    bucket = await _stable_bucket(60)
    calls = await asyncio.gather(*(
        rate_limit.allow(key, max_calls=7, window_seconds=60) for _ in range(24)
    ))
    assert sum(calls) == 7

    redis_key = f"chirp:ratelimit:{key}:{bucket}"
    assert await redis.get(redis_key) == "24"
    initial_ttl = await redis.pttl(redis_key)
    assert 0 < initial_ttl <= 60_000

    await asyncio.sleep(.12)
    assert not await rate_limit.allow(key, max_calls=7, window_seconds=60)
    next_ttl = await redis.pttl(redis_key)
    assert 0 < next_ttl < initial_ttl, "later requests must not extend the current fixed-window expiry"


@needs_redis
async def test_real_redis_repairs_legacy_current_bucket_without_ttl(redis_backend):
    redis, prefix = redis_backend
    key = f"{prefix}:legacy-no-ttl"
    window = 45
    bucket = await _stable_bucket(window)
    redis_key = f"chirp:ratelimit:{key}:{bucket}"
    await redis.set(redis_key, 4)
    assert await redis.pttl(redis_key) == -1

    assert await rate_limit.allow(key, max_calls=8, window_seconds=window)
    ttl = await redis.pttl(redis_key)
    assert 0 < ttl <= window * 1000, "an existing current bucket without TTL must be healed"
    await asyncio.sleep(.12)
    before = await redis.pttl(redis_key)
    assert await rate_limit.allow(key, max_calls=8, window_seconds=window)
    after = await redis.pttl(redis_key)
    assert 0 < after <= before, "healing must happen once without turning the bucket into a sliding window"


@needs_redis
async def test_timeout_after_redis_increment_does_not_leave_immortal_bucket(redis_backend, monkeypatch):
    from app.ws import pubsub

    redis, prefix = redis_backend
    key = f"{prefix}:ambiguous-timeout"
    window = 40
    bucket = await _stable_bucket(window)

    class TimeoutAfterCommit:
        async def eval(self, script, numkeys, redis_key, expiry_seconds):
            await redis.eval(script, numkeys, redis_key, expiry_seconds)
            raise TimeoutError("response lost after Redis committed")

    monkeypatch.setattr(pubsub, "get_redis", lambda: TimeoutAfterCommit())
    assert await rate_limit.allow(key, max_calls=1, window_seconds=window)
    redis_key = f"chirp:ratelimit:{key}:{bucket}"
    assert await redis.get(redis_key) == "1"
    ttl = await redis.pttl(redis_key)
    assert 0 < ttl <= window * 1000, "a lost reply after commit must not strand an immortal counter"


async def test_redis_timeout_uses_fallback_within_the_existing_short_budget(monkeypatch, caplog):
    import app.config as config
    import app.ws.pubsub as pubsub

    monkeypatch.setattr(config, "get_settings", lambda: Settings(_env_file=None, env="production"))

    class StalledRedis:
        async def eval(self, *args):
            await asyncio.Event().wait()

    monkeypatch.setattr(pubsub, "get_redis", lambda: StalledRedis())
    started = time.monotonic()
    with caplog.at_level(logging.INFO, logger="app.operational"):
        assert await asyncio.wait_for(
            rate_limit.allow("c460-timeout:subject", max_calls=1, window_seconds=60), 2
        )
    elapsed = time.monotonic() - started
    assert elapsed < 1.5, "the Redis limiter call should fall back at its configured 0.5s deadline"
    records = [json.loads(row.getMessage()) for row in caplog.records if row.name == "app.operational"]
    assert [record["event"] for record in records] == ["rate_limit_fallback"]
    assert "c460-timeout:subject" not in caplog.text


async def test_local_fallback_reaps_expired_subjects_without_resetting_live_budget(monkeypatch):
    import app.config as config

    now = [100.0]
    monkeypatch.setattr(config, "get_settings", lambda: Settings(_env_file=None, env="local"))
    monkeypatch.setattr(rate_limit, "time", SimpleNamespace(monotonic=lambda: now[0]))
    rate_limit._reset_all()

    active = "c460-active-subject"
    assert await rate_limit.allow(active, max_calls=1, window_seconds=20)
    expired = [f"c460-expired-{index}" for index in range(3000)]
    for key in expired:
        assert await rate_limit.allow(key, max_calls=1, window_seconds=5)

    before_sweep = len(rate_limit._WINDOWS)
    now[0] += 6
    assert await rate_limit.allow("c460-trigger-sweep", max_calls=1, window_seconds=5)
    after_sweep = len(rate_limit._WINDOWS)
    assert 0 < before_sweep - after_sweep <= rate_limit.LOCAL_SWEEP_BATCH, (
        "a single local lookup should reclaim only its bounded batch of expired subjects"
    )
    assert after_sweep > 1, "one bounded sweep should leave older expired entries for later visits"
    assert not await rate_limit.allow(active, max_calls=1, window_seconds=20), (
        "evicting expired subjects must not refund an active subject's remaining budget"
    )

    # Repeated checks on the active key drive the bounded sweep without adding more
    # subjects. Expired mappings should disappear while the active key remains denied.
    for _ in range(64 * 10):
        assert not await rate_limit.allow(active, max_calls=1, window_seconds=20)
    assert len(rate_limit._WINDOWS) < 10, "expired subjects must be reclaimed incrementally"


async def test_existing_budget_survives_a_later_wider_window(monkeypatch):
    import app.config as config

    now = [0.0]
    monkeypatch.setattr(config, "get_settings", lambda: Settings(_env_file=None, env="local"))
    monkeypatch.setattr(rate_limit, "time", SimpleNamespace(monotonic=lambda: now[0]))
    rate_limit._reset_all()

    key = "c460-changing-window"
    assert await rate_limit.allow(key, max_calls=1, window_seconds=1)
    now[0] = 5
    assert not await rate_limit.allow(key, max_calls=1, window_seconds=10), (
        "the oldest allowed call is still inside this key's newly widened window"
    )


async def test_local_fallback_refuses_new_subject_at_capacity_without_evicting_live_budgets(monkeypatch):
    import app.config as config

    monkeypatch.setattr(config, "get_settings", lambda: Settings(_env_file=None, env="local"))
    rate_limit._reset_all()
    capacity = rate_limit.LOCAL_MAX_WINDOWS
    assert capacity >= 512
    for index in range(capacity):
        assert await rate_limit.allow(f"c460-live-{index}", max_calls=2, window_seconds=600)

    assert not await rate_limit.allow("c460-over-capacity", max_calls=2, window_seconds=600), (
        "a full fallback registry must refuse a new subject rather than grow or evict live budgets"
    )
    assert await rate_limit.allow("c460-live-0", max_calls=2, window_seconds=600)
    assert not await rate_limit.allow("c460-live-0", max_calls=2, window_seconds=600)
    assert len(rate_limit._WINDOWS) == capacity
