"""Execute gateway ownership/deadline failures without a provider or database.

The companion local Redis/Uvicorn probe measures transport buffers and delivery;
these controlled waits make timeout, queue ownership, and teardown regressions
deterministic rather than relying on the operating system's socket buffer size.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
import anyio

from app.ws import gateway


@dataclass
class Harness:
    items: asyncio.Queue = field(default_factory=asyncio.Queue)
    send_gate: asyncio.Event = field(default_factory=asyncio.Event)
    subscribe_gate: asyncio.Event = field(default_factory=asyncio.Event)
    cleanup_gate: asyncio.Event = field(default_factory=asyncio.Event)
    close_gate: asyncio.Event = field(default_factory=asyncio.Event)
    send_started: asyncio.Event = field(default_factory=asyncio.Event)
    ready_sent: asyncio.Event = field(default_factory=asyncio.Event)
    subscribe_started: asyncio.Event = field(default_factory=asyncio.Event)
    close_started: asyncio.Event = field(default_factory=asyncio.Event)
    sent: list[str] = field(default_factory=list)
    closes: list[int] = field(default_factory=list)
    order: list[str] = field(default_factory=list)
    tasks: list[asyncio.Task] = field(default_factory=list)
    suspended: bool = False
    missing: bool = False
    poll_error: bool = False
    poll_gate: asyncio.Event = field(default_factory=asyncio.Event)
    polls: int = 0
    authentication_queries: int = 0
    cleaned: int = 0
    scope: dict = field(default_factory=lambda: {"subprotocols": ["local-ws-user"]})
    headers: dict = field(default_factory=dict)

    def __post_init__(self):
        self.subscribe_gate.set()
        self.cleanup_gate.set()
        self.close_gate.set()
        self.poll_gate.set()

    async def accept(self, **kwargs):
        self.order.append("accept")

    async def close(self, code):
        self.close_started.set()
        self.order.append("close")
        self.closes.append(code)
        await self.close_gate.wait()

    async def receive_text(self):
        await asyncio.Future()

    async def send_text(self, value):
        if json.loads(value).get("type") != "ready":
            self.send_started.set()
            try:
                await self.send_gate.wait()
            except asyncio.CancelledError:
                self.order.append("send-canceled")
                raise
        self.sent.append(value)
        if json.loads(value).get("type") == "ready":
            self.ready_sent.set()

    async def subscribe(self, channel):
        self.subscribe_started.set()
        await self.subscribe_gate.wait()

    async def listen(self):
        while True:
            yield await self.items.get()

    async def unsubscribe(self, channel):
        await self.cleanup_gate.wait()

    async def aclose(self):
        self.cleaned += 1
        await self.cleanup_gate.wait()

    def ack(self):
        self.items.put_nowait({"type": "subscribe", "channel": "user:00000000-0000-0000-0000-000000000001", "data": 1})

    def message(self, value="value"):
        self.items.put_nowait({"type": "message", "data": json.dumps({"type": "probe", "value": value}, ensure_ascii=False)})

    def run(self):
        task = asyncio.create_task(gateway.websocket_gateway(self))
        self.tasks.append(task)
        return task


@pytest.fixture
async def env(monkeypatch):
    h = Harness()

    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def execute(self, query):
            h.polls += 1
            await h.poll_gate.wait()
            if h.poll_error:
                raise RuntimeError("controlled provider-free database error")
            value = "suspended" if h.suspended else None
            return SimpleNamespace(
                scalar_one_or_none=lambda: value,
                one_or_none=lambda: None if h.missing else (value,),
            )

    async def user(session, uid):
        h.authentication_queries += 1
        return SimpleNamespace(id="00000000-0000-0000-0000-000000000001", suspended_at=None)

    monkeypatch.setattr(gateway, "get_user_by_uid", user)
    monkeypatch.setattr(gateway, "get_session_factory", lambda: Session)
    monkeypatch.setattr(gateway, "get_settings", lambda: SimpleNamespace(auth_mode="emulated"))
    monkeypatch.setattr(gateway, "get_redis", lambda: SimpleNamespace(pubsub=lambda: h))
    for name, value in {
        "WS_SUBSCRIBE_SECONDS": .04, "WS_SEND_SECONDS": .04,
        "WS_CLOSE_SECONDS": .04, "WS_CLEANUP_SECONDS": .04,
        "WS_RECONCILE_SECONDS": .04, "WS_QUEUE_MAX_AGE_SECONDS": .1,
        "WS_SUSPENSION_POLL_SECONDS": 30, "WS_SUSPENSION_POLL_JITTER": 0,
        "WS_QUEUE_MAX_FRAMES": 32, "WS_QUEUE_MAX_BYTES": 512 * 1024,
        "WS_FRAME_MAX_BYTES": 128 * 1024,
    }.items():
        monkeypatch.setattr(gateway, name, value, raising=False)
    yield h
    for gate in (h.send_gate, h.subscribe_gate, h.cleanup_gate, h.close_gate, h.poll_gate): gate.set()
    for task in h.tasks: task.cancel()
    await asyncio.wait_for(asyncio.gather(*h.tasks, return_exceptions=True), 1)
    assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("ws-") and not t.done()]


async def _ended(task):
    done, _ = await asyncio.wait({task}, timeout=.35)
    assert task in done, "gateway did not finish within its controlled local budgets"
    await task


async def test_ready_requires_actual_subscription_ack(env):
    task = env.run()
    await asyncio.wait_for(env.subscribe_started.wait(), .2)
    await asyncio.sleep(.01)
    assert env.sent == []
    env.ack()
    await asyncio.sleep(.02)
    assert [json.loads(value) for value in env.sent] == [{"type": "ready"}]
    assert not task.done()


async def test_missing_subscription_ack_times_out(env):
    task = env.run()
    await _ended(task)
    assert env.closes == [4503]
    assert env.sent == []
    assert env.cleaned == 1


async def test_hanging_subscribe_times_out_and_cleans(env):
    env.subscribe_gate.clear()
    await _ended(env.run())
    assert env.closes == [4503]
    assert env.cleaned == 1


async def test_slow_reader_has_send_deadline(env):
    env.ack()
    env.message()
    task = env.run()
    await asyncio.wait_for(env.send_started.wait(), .2)
    await _ended(task)
    assert env.closes == [4503]
    assert env.order.index("send-canceled") < env.order.index("close")


async def test_queue_count_includes_inflight_frame(env, monkeypatch):
    monkeypatch.setattr(gateway, "WS_QUEUE_MAX_FRAMES", 2, raising=False)
    monkeypatch.setattr(gateway, "WS_SEND_SECONDS", 10, raising=False)
    env.ack()
    env.message("first")
    task = env.run()
    await asyncio.wait_for(env.send_started.wait(), .2)
    env.message("second")
    env.message("third")
    await _ended(task)
    assert env.closes == [4503]
    assert env.order.index("send-canceled") < env.order.index("close")


async def test_queue_bytes_count_utf8_and_inflight(env, monkeypatch):
    # UTF-8 bytes, rather than Python characters, determine admitted memory.
    frame = json.dumps({"type": "probe", "value": "€" * 10}, ensure_ascii=False)
    assert len(frame.encode()) > len(frame)
    monkeypatch.setattr(gateway, "WS_QUEUE_MAX_BYTES", len(frame.encode()) + 1, raising=False)
    monkeypatch.setattr(gateway, "WS_SEND_SECONDS", 10, raising=False)
    env.ack()
    task = env.run()
    await asyncio.wait_for(env.ready_sent.wait(), .2)
    env.message("€" * 10)
    await asyncio.wait_for(env.send_started.wait(), .2)
    env.message("€" * 10)
    await _ended(task)
    assert env.closes == [4503]


@pytest.mark.parametrize("reason, code", [("missing", 4401), ("suspended", 4403), ("poll_error", 4503)])
async def test_reconciliation_closes_explicitly(env, monkeypatch, reason, code):
    monkeypatch.setattr(gateway, "WS_SUSPENSION_POLL_SECONDS", .01)
    setattr(env, reason, True)
    env.ack()
    await _ended(env.run())
    assert env.closes == [code]
    assert env.polls == 1


async def test_cleanup_and_close_have_independent_budgets(env):
    env.cleanup_gate.clear()
    env.close_gate.clear()
    # No ACK: setup deadline must still reach bounded close AND cleanup.
    await _ended(env.run())
    assert env.closes == [4503]
    assert env.cleaned == 1


async def test_suspension_cancels_inflight_sender_before_close(env, monkeypatch):
    monkeypatch.setattr(gateway, "WS_SUSPENSION_POLL_SECONDS", .01)
    monkeypatch.setattr(gateway, "WS_SEND_SECONDS", 10, raising=False)
    env.ack()
    env.message()
    task = env.run()
    await asyncio.wait_for(env.send_started.wait(), .2)
    env.suspended = True
    await _ended(task)
    assert env.closes == [4403]
    assert env.order.index("send-canceled") < env.order.index("close")


async def test_reconciliation_wait_is_bounded_independently_of_redis(env, monkeypatch):
    monkeypatch.setattr(gateway, "WS_SUSPENSION_POLL_SECONDS", .01)
    env.poll_gate.clear()
    env.ack()
    await _ended(env.run())
    assert env.closes == [4503]


async def test_broker_resubscribe_forces_reconnect_catchup(env):
    env.ack()
    task = env.run()
    await asyncio.wait_for(env.ready_sent.wait(), .2)
    env.ack()
    await _ended(task)
    assert env.closes == [4503]
    assert len(env.sent) == 1


async def test_oversized_frame_closes_without_partial_send(env, monkeypatch):
    monkeypatch.setattr(gateway, "WS_FRAME_MAX_BYTES", 64)
    env.ack()
    env.message("€" * 30)
    await _ended(env.run())
    assert env.closes == [4503]
    assert not env.send_started.is_set()


async def test_queue_age_bounds_an_inflight_send_even_with_long_send_budget(env, monkeypatch):
    monkeypatch.setattr(gateway, "WS_QUEUE_MAX_AGE_SECONDS", .03)
    monkeypatch.setattr(gateway, "WS_SEND_SECONDS", 10)
    env.ack()
    env.message()
    task = env.run()
    await asyncio.wait_for(env.send_started.wait(), .2)
    await _ended(task)
    assert env.closes == [4503]


async def test_parent_cancellation_releases_sender_and_pubsub(env):
    env.ack()
    env.message()
    task = env.run()
    await asyncio.wait_for(env.send_started.wait(), .2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert env.cleaned == 1
    assert "send-canceled" in env.order


async def test_diagnostics_exclude_payload_and_token(env, caplog):
    env.ack()
    env.message("private-message-body")
    await _ended(env.run())
    assert "private-message-body" not in caplog.text
    assert "local-ws-user" not in caplog.text
    warning = next(record for record in caplog.records if hasattr(record, "ws_reason"))
    assert 0 < warning.ws_peak_frames <= gateway.WS_QUEUE_MAX_FRAMES
    assert 0 < warning.ws_peak_bytes <= gateway.WS_QUEUE_MAX_BYTES


async def test_firebase_handshake_uses_shared_bounded_workers_without_loop_stall(env, monkeypatch):
    import firebase_admin
    from firebase_admin import auth as firebase_auth
    worker_threads, ticks = [], []
    finished = threading.Event()
    def verify(token):
        worker_threads.append(threading.get_ident())
        time.sleep(.08)
        finished.set()
        return {"uid": "local-ws-user"}
    monkeypatch.setattr(gateway, "get_settings", lambda: SimpleNamespace(auth_mode="firebase"))
    monkeypatch.setattr(firebase_admin, "get_app", lambda: object())
    monkeypatch.setattr(firebase_auth, "verify_id_token", verify)
    async def heartbeat():
        while not finished.is_set():
            await asyncio.sleep(.005)
            ticks.append(time.monotonic())
    ticker = asyncio.create_task(heartbeat())
    assert await gateway._resolve_uid(env) == "local-ws-user"
    await ticker
    assert len(worker_threads) == 1
    assert worker_threads[0] != threading.get_ident()
    assert len(ticks) >= 3


async def test_firebase_caller_timeout_precedes_sql_and_running_worker_completion(env, monkeypatch):
    import firebase_admin
    from firebase_admin import auth as firebase_auth
    release, finished = threading.Event(), threading.Event()
    def verify(token):
        try:
            release.wait(1)
            return {"uid": "local-ws-user"}
        finally: finished.set()
    monkeypatch.setattr(gateway, "get_settings", lambda: SimpleNamespace(auth_mode="firebase"))
    monkeypatch.setattr(firebase_admin, "get_app", lambda: object())
    monkeypatch.setattr(firebase_auth, "verify_id_token", verify)
    monkeypatch.setattr(gateway, "WS_AUTH_SECONDS", .02)
    try:
        await _ended(env.run())
        assert env.closes == [4503]
        assert env.polls == 0
        assert env.authentication_queries == 0
        assert "accept" not in env.order
        assert not finished.is_set()
    finally:
        release.set()
        async with asyncio.timeout(1):
            while not finished.is_set(): await asyncio.sleep(.005)


async def test_anyio_level_cancellation_finishes_owned_cleanup(env):
    env.ack()
    env.message()
    async with anyio.create_task_group() as group:
        group.start_soon(gateway.websocket_gateway, env)
        await asyncio.wait_for(env.send_started.wait(), .2)
        group.cancel_scope.cancel()
    assert env.cleaned == 1
    assert "send-canceled" in env.order
