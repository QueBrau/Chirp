"""Operational evidence must follow the actual request/provider outcome."""
import asyncio
import json
import logging
import subprocess
import sys
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings
from app.services import rate_limit
from app.core import operational_signals as signals


def records(caplog):
    return [json.loads(r.getMessage()) for r in caplog.records
            if r.name == "app.operational"]


async def test_real_pool_timeout_emits_at_503_boundary(database_url, caplog, monkeypatch):
    from app.main import create_app

    engine = create_async_engine(database_url, pool_size=1, max_overflow=0, pool_timeout=.03)
    app = create_app()

    @app.get("/capacity-fixture")
    async def capacity_fixture():
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return {"ok": True}

    try:
        async with AsyncClient(transport=ASGITransport(app), base_url="http://local") as client:
            async with engine.connect():
                response = await client.get("/capacity-fixture?token=do-not-log")
            assert response.status_code == 503
            assert response.json() == {"detail": "over_capacity"}
            assert response.headers["retry-after"] == "5"
            assert [r["event"] for r in records(caplog)] == ["sql_pool_capacity_503"]
            assert "do-not-log" not in json.dumps(records(caplog))
            assert (await client.get("/capacity-fixture")).status_code == 200
            assert len(records(caplog)) == 1
            signals._reset_for_tests()
            def broken_sink(*args, **kwargs):
                raise RuntimeError("private-log-transport")
            monkeypatch.setattr(signals.logger, "log", broken_sink)
            async with engine.connect():
                failed_sink_response = await client.get("/capacity-fixture")
            assert failed_sink_response.status_code == 503
            assert failed_sink_response.json() == {"detail": "over_capacity"}
            assert failed_sink_response.headers["retry-after"] == "5"
    finally:
        await engine.dispose()


async def test_fallback_then_success_has_structured_observations(monkeypatch, caplog):
    import app.config as config
    import app.ws.pubsub as pubsub

    monkeypatch.setattr(config, "get_settings", lambda: Settings(_env_file=None, env="production"))
    class Redis:
        down = True
        async def incr(self, key):
            if self.down:
                raise ConnectionError("redis://private-user:private-password@example")
            return 2
    redis = Redis()
    monkeypatch.setattr(pubsub, "get_redis", lambda: redis)
    with caplog.at_level(logging.INFO):
        assert await rate_limit.allow("private-scope:private-subject", max_calls=3, window_seconds=60)
        redis.down = False
        assert await rate_limit.allow("private-scope:private-subject", max_calls=3, window_seconds=60)
    assert [r["event"] for r in records(caplog)] == [
        "rate_limit_fallback", "rate_limit_redis_success_after_fallback"]
    assert "private" not in json.dumps(records(caplog))


@pytest.mark.parametrize("second_failure", [False, True])
async def test_in_flight_success_cannot_recover_a_newer_failure(monkeypatch, caplog, second_failure):
    import app.config as config
    import app.ws.pubsub as pubsub

    monkeypatch.setattr(config, "get_settings", lambda: Settings(_env_file=None, env="production"))
    started, release = asyncio.Event(), asyncio.Event()
    class Redis:
        mode = "fail" if second_failure else "hold"
        async def incr(self, key):
            mode = self.mode
            if mode == "fail":
                raise ConnectionError("private-error")
            if mode == "hold":
                started.set()
                await release.wait()
            return 2
    redis = Redis()
    monkeypatch.setattr(pubsub, "get_redis", lambda: redis)
    async def call():
        return await rate_limit.allow("scope:subject", max_calls=100, window_seconds=60)
    with caplog.at_level(logging.INFO):
        if second_failure:
            await call()
        redis.mode = "hold"
        pending = asyncio.create_task(call())
        try:
            await asyncio.wait_for(started.wait(), .2)
            redis.mode = "fail"
            await call()
        finally:
            release.set()
            await pending
        assert [r["event"] for r in records(caplog)] == ["rate_limit_fallback"]
        redis.mode = "ok"
        await call()
    assert [r["event"] for r in records(caplog)] == [
        "rate_limit_fallback", "rate_limit_redis_success_after_fallback"]


async def test_later_failure_overrides_a_completed_success_observation(monkeypatch, caplog):
    import app.config as config
    import app.ws.pubsub as pubsub

    monkeypatch.setattr(config, "get_settings", lambda: Settings(_env_file=None, env="production"))
    started, release = asyncio.Event(), asyncio.Event()
    class Redis:
        mode = "fail"
        async def incr(self, key):
            mode = self.mode
            if mode == "delayed_failure":
                started.set()
                await release.wait()
            if mode != "ok":
                raise ConnectionError("private-error")
            return 2
    redis = Redis()
    monkeypatch.setattr(pubsub, "get_redis", lambda: redis)
    async def call():
        return await rate_limit.allow("scope:subject", max_calls=100, window_seconds=60)
    clock = [0.0]
    monkeypatch.setattr(signals, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    with caplog.at_level(logging.INFO):
        await call()
        redis.mode = "delayed_failure"
        pending = asyncio.create_task(call())
        try:
            await asyncio.wait_for(started.wait(), .2)
            redis.mode = "ok"
            await call()
            assert rate_limit._fallback_epoch is None
            clock[0] += 601
        finally:
            release.set()
            await pending
        assert rate_limit._fallback_epoch is not None
        await call()
    assert [r["event"] for r in records(caplog)] == [
        "rate_limit_fallback", "rate_limit_redis_success_after_fallback",
        "rate_limit_fallback", "rate_limit_redis_success_after_fallback"]


async def test_expiry_failure_and_redis_budget_denial_are_observed_correctly(monkeypatch, caplog):
    import app.config as config
    import app.ws.pubsub as pubsub

    monkeypatch.setattr(config, "get_settings", lambda: Settings(_env_file=None, env="production"))
    class Redis:
        count = 1
        async def incr(self, key): return self.count
        async def expire(self, key, seconds): raise TimeoutError("private-expiry")
    redis = Redis()
    monkeypatch.setattr(pubsub, "get_redis", lambda: redis)
    with caplog.at_level(logging.INFO):
        assert await rate_limit.allow("scope:subject", max_calls=1, window_seconds=60)
        redis.count = 2
        assert not await rate_limit.allow("scope:subject", max_calls=1, window_seconds=60)
    assert [r["event"] for r in records(caplog)] == [
        "rate_limit_fallback", "rate_limit_redis_success_after_fallback"]


async def test_cancellation_is_not_a_health_observation(monkeypatch, caplog):
    import app.config as config
    import app.ws.pubsub as pubsub

    monkeypatch.setattr(config, "get_settings", lambda: Settings(_env_file=None, env="production"))
    class Redis:
        async def incr(self, key): raise asyncio.CancelledError()
    monkeypatch.setattr(pubsub, "get_redis", Redis)
    with pytest.raises(asyncio.CancelledError):
        await rate_limit.allow("scope:subject", max_calls=1, window_seconds=60)
    assert records(caplog) == []


def test_bounded_fixed_vocabulary_and_persistent_failure_heartbeat(monkeypatch, caplog):
    clock = [0.0]
    monkeypatch.setattr(signals, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    with caplog.at_level(logging.INFO):
        for i in range(1000):
            signals.observe("sql_pool_capacity_503")
            signals.observe("rate_limit_fallback")
            signals.observe("rate_limit_redis_success_after_fallback")
            signals.observe("private-user-" + str(i))
        assert len(records(caplog)) == 3
        clock[0] = 60
        signals.observe("sql_pool_capacity_503")
        signals.observe("rate_limit_fallback")
        assert len(records(caplog)) == 4
        clock[0] = 600
        signals.observe("rate_limit_fallback")
    assert len(records(caplog)) == 5
    assert len(signals._last_emitted) == 3
    assert "private" not in json.dumps(records(caplog))


def test_emitter_exceptions_cannot_escape_or_leak(monkeypatch, capsys):
    def broken(*args, **kwargs):
        raise RuntimeError("private-sink-credentials")
    monkeypatch.setattr(signals.logger, "log", broken)
    signals.observe("sql_pool_capacity_503")
    assert capsys.readouterr() == ("", "")


def test_concurrent_observations_still_have_one_throttle_slot(caplog):
    from concurrent.futures import ThreadPoolExecutor
    with caplog.at_level(logging.WARNING):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(signals.observe, ["sql_pool_capacity_503"] * 200))
    assert len(records(caplog)) == 1


def test_string_subclass_cannot_bypass_fixed_event_vocabulary(caplog):
    class Forged(str):
        def __hash__(self): return hash("sql_pool_capacity_503")
        def __eq__(self, other): return True
    signals.observe(Forged("private-identifier"))
    assert records(caplog) == []
    assert signals._last_emitted == {}


@pytest.mark.parametrize("failure", ["logger", "stream"])
async def test_broken_legacy_warning_preserves_fallback_and_refusal(monkeypatch, capsys, failure):
    import app.config as config
    import app.ws.pubsub as pubsub
    from app.core.logging_config import configure_app_logging

    monkeypatch.setattr(config, "get_settings", lambda: Settings(_env_file=None, env="production"))
    def unavailable():
        raise ConnectionError("private-redis-credentials")
    monkeypatch.setattr(pubsub, "get_redis", unavailable)
    if failure == "logger":
        def broken(*args, **kwargs):
            raise RuntimeError("private-legacy-log-credentials")
        monkeypatch.setattr(rate_limit.logger, "warning", broken)
    else:
        configure_app_logging()
        class BrokenStream:
            def write(self, data): raise RuntimeError("private-legacy-log-credentials")
            def flush(self): pass
        for handler in logging.getLogger("app").handlers:
            monkeypatch.setattr(handler, "stream", BrokenStream())
    assert await rate_limit.allow("scope:private-user", max_calls=1, window_seconds=60)
    assert not await rate_limit.allow("scope:private-user", max_calls=1, window_seconds=60)
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "private" not in captured.out


def test_real_stream_is_json_and_broken_stream_is_silent():
    script = '''
from app.core.logging_config import configure_app_logging
from app.core.operational_signals import observe
import logging
configure_app_logging()
observe("sql_pool_capacity_503")
class BrokenStream:
    def write(self, data): raise RuntimeError("private-stream-error")
    def flush(self): pass
for handler in logging.getLogger("app").handlers:
    handler.setStream(BrokenStream())
observe("rate_limit_fallback")
'''
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert result.stderr == ""
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(rows) == 1
    assert rows[0] == {"schema_version": 1, "signal_family": "chirp_operational",
                       "event": "sql_pool_capacity_503", "severity": "WARNING",
                       "observation_scope": "process", "sampled": True}
