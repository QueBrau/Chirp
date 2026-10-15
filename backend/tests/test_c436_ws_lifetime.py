"""Real PostgreSQL ownership with controlled ASGI teardown and real Redis coverage."""
from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from fastapi import WebSocketDisconnect
from redis.asyncio import Redis
from sqlalchemy import select, text

from app import models
from app.config import Settings, get_settings
from app.db import get_engine, get_session_factory
from app.services.account_deletion_hold import _lock_exclusive
from app.services.account_external_operations import inspect_registered_operation_drain
from app.ws import gateway
from tests.test_c436_external_operations import _activate, _deletion_request
from tests.test_ws_resource_bounds import Harness


class Socket(Harness):
    """Control transport completion without replacing any registry SQL."""
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.scope = {"subprotocols": [owner.firebase_uid]}
        self.disconnect = asyncio.Event()
        self.accepted = asyncio.Event()
        self.accept_gate = asyncio.Event()
        self.accept_gate.set()
        self.cleanup_started = asyncio.Event()
        self.resistant_started = asyncio.Event()
        self.resistant_release = asyncio.Event()
        self.resistant_cancelled = asyncio.Event()
        self.resist_cancellation = False
        self.accept_error = False
        self.close_error = False
        self.cleanup_error = False
        self.pool_samples = []
        self.send_gate.set()

    async def accept(self, **kwargs):
        self.pool_samples.append(get_engine().pool.checkedout())
        self.order.append("accept")
        self.accepted.set()
        await self.accept_gate.wait()
        if self.accept_error:
            raise RuntimeError("controlled accept failure")

    async def receive_text(self):
        self.resistant_started.set()
        if self.resist_cancellation:
            while not self.resistant_release.is_set():
                try:
                    await self.resistant_release.wait()
                except asyncio.CancelledError:
                    self.resistant_cancelled.set()
            raise WebSocketDisconnect(code=1000)
        await self.disconnect.wait()
        raise WebSocketDisconnect(code=1000)

    async def close(self, code):
        await super().close(code)
        if self.close_error:
            raise RuntimeError("controlled close failure")

    async def aclose(self):
        self.cleanup_started.set()
        await super().aclose()
        if self.cleanup_error:
            raise RuntimeError("controlled Redis close failure")

    def ack(self):
        self.items.put_nowait({
            "type": "subscribe", "channel": f"user:{self.owner.id}", "data": 1,
        })


async def _event(event):
    await asyncio.wait_for(event.wait(), 10)


async def _ended(task, *, cancelled=False):
    # wait() does not cancel a hung gateway and thereby accidentally repair it.
    done, _ = await asyncio.wait({task}, timeout=10)
    assert task in done, "gateway failed to finish bounded local teardown"
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task


async def _rows(owner):
    async with get_session_factory()() as session:
        return (await session.execute(
            select(models.AccountWsConnection.id, models.AccountWsConnection.state)
            .where(models.AccountWsConnection.user_id == uuid.UUID(owner.id))
        )).all()


async def _drain(owner, request_id):
    return await inspect_registered_operation_drain(
        user_id=uuid.UUID(owner.id), request_id=uuid.UUID(request_id),
    )


@pytest.fixture
async def socket_env(client, make_user, monkeypatch):
    owner = await make_user("WS lifetime owner")
    socket = Socket(owner)
    monkeypatch.setattr(get_settings(), "account_deletion_ws_admission_enabled", True)
    monkeypatch.setattr(gateway, "get_redis", lambda: SimpleNamespace(pubsub=lambda: socket))
    for name, value in {
        "WS_AUTH_SECONDS": 2, "WS_RECONCILE_SECONDS": 2,
        "WS_SUBSCRIBE_SECONDS": 2, "WS_SEND_SECONDS": 2,
        "WS_CLOSE_SECONDS": .2, "WS_CLEANUP_SECONDS": .5,
        "WS_SUSPENSION_POLL_SECONDS": 30, "WS_SUSPENSION_POLL_JITTER": 0,
    }.items():
        monkeypatch.setattr(gateway, name, value)
    yield socket, owner
    for event in (socket.send_gate, socket.subscribe_gate, socket.cleanup_gate,
                  socket.close_gate, socket.accept_gate, socket.resistant_release,
                  socket.disconnect):
        event.set()
    for task in socket.tasks:
        if not task.done():
            task.cancel()
    if socket.tasks:
        await asyncio.wait_for(asyncio.gather(*socket.tasks, return_exceptions=True), 10)
    # A cancellation-resistant controlled task is released above; give its result
    # callback a chance to run and verify no transport task escaped ownership.
    pending = [task for task in asyncio.all_tasks()
               if task.get_name().startswith("ws-") and not task.done()]
    if pending:
        await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 10)


async def test_flag_off_uses_no_new_registry_or_hold_queries(socket_env, monkeypatch):
    socket, owner = socket_env
    assert Settings(_env_file=None).account_deletion_ws_admission_enabled is False
    monkeypatch.setattr(get_settings(), "account_deletion_ws_admission_enabled", False)

    async def forbidden(*args, **kwargs):
        pytest.fail("disabled WS admission must not query its registry or hold")
    monkeypatch.setattr(gateway, "register_ws_connection", forbidden)
    monkeypatch.setattr(gateway, "finish_ws_connection", forbidden)
    monkeypatch.setattr(gateway, "inspect_deletion_hold", forbidden)
    socket.ack()
    task = socket.run()
    await _event(socket.ready_sent)
    socket.disconnect.set()
    await _ended(task)
    assert socket.cleaned == 1
    assert await _rows(owner) == []


async def test_held_account_is_rejected_before_accept_or_subscription(socket_env, client):
    socket, owner = socket_env
    request_id = await _deletion_request(client, owner)
    await _activate(owner.id, request_id)
    await _ended(socket.run())
    assert not socket.accepted.is_set()
    assert not socket.subscribe_started.is_set()
    assert socket.closes == [4503]
    assert await _rows(owner) == []


async def test_hold_waits_for_registered_socket_teardown_without_pinning_sql(
    socket_env, client, monkeypatch,
):
    socket, owner = socket_env
    request_id = await _deletion_request(client, owner)
    monkeypatch.setattr(gateway, "WS_SUSPENSION_POLL_SECONDS", .05)
    monkeypatch.setattr(gateway, "WS_CLEANUP_SECONDS", 4)
    socket.accept_gate.clear()
    socket.cleanup_gate.clear()
    socket.ack()
    task = socket.run()
    await _event(socket.accepted)
    # Registration is durable before accept can finish, without an open SQL scope.
    assert socket.pool_samples == [0]
    assert [state for _, state in await _rows(owner)] == ["running"]
    await _activate(owner.id, request_id)
    assert (await _drain(owner, request_id)).running_count == 1
    socket.accept_gate.set()
    await _event(socket.cleanup_started)
    assert not task.done()
    assert get_engine().pool.checkedout() == 0
    assert (await _drain(owner, request_id)).registered_operations_drained is False
    socket.cleanup_gate.set()
    await _ended(task)
    assert socket.closes == [4503]
    assert await _rows(owner) == []
    assert (await _drain(owner, request_id)).registered_operations_drained is True


@pytest.mark.parametrize("failure", ["redis_close", "asgi_close", "finish_write", "finish_timeout"])
async def test_ambiguous_teardown_keeps_deletion_blocked(socket_env, client, monkeypatch, failure):
    socket, owner = socket_env
    request_id = await _deletion_request(client, owner)
    monkeypatch.setattr(gateway, "WS_SUSPENSION_POLL_SECONDS", .05)
    socket.cleanup_error = failure == "redis_close"
    socket.close_error = failure == "asgi_close"
    if failure in {"finish_write", "finish_timeout"}:
        async def fail_finish(*args, **kwargs):
            if failure == "finish_timeout":
                await asyncio.Event().wait()
            raise RuntimeError("controlled completion failure")
        monkeypatch.setattr(gateway, "finish_ws_connection", fail_finish)
    socket.ack()
    task = socket.run()
    await _event(socket.ready_sent)
    await _activate(owner.id, request_id)
    await _ended(task)
    expected = "running" if failure.startswith("finish_") else "uncertain"
    assert [state for _, state in await _rows(owner)] == [expected]
    assert (await _drain(owner, request_id)).registered_operations_drained is False


@pytest.mark.parametrize("cancelled", [False, True])
async def test_lost_registration_response_preserves_committed_blocker(
    socket_env, client, monkeypatch, cancelled,
):
    socket, owner = socket_env
    request_id = await _deletion_request(client, owner)
    register = gateway.register_ws_connection

    async def lose_response(**kwargs):
        await register(**kwargs)
        # Simulate loss after a real durable commit but before the handle reaches
        # the gateway. Neither an error nor cancellation proves no row exists.
        if cancelled:
            raise asyncio.CancelledError()
        raise RuntimeError("controlled lost commit response")

    monkeypatch.setattr(gateway, "register_ws_connection", lose_response)
    await _ended(socket.run(), cancelled=cancelled)
    assert not socket.accepted.is_set()
    assert not socket.subscribe_started.is_set()
    assert [state for _, state in await _rows(owner)] == ["running"]
    await _activate(owner.id, request_id)
    assert (await _drain(owner, request_id)).registered_operations_drained is False


async def test_cancel_resistant_child_cannot_hang_or_clear_the_registry(socket_env, client, monkeypatch):
    socket, owner = socket_env
    socket.resist_cancellation = True
    monkeypatch.setattr(gateway, "WS_SUSPENSION_POLL_SECONDS", .05)
    request_id = await _deletion_request(client, owner)
    socket.ack()
    task = socket.run()
    await _event(socket.ready_sent)
    await _event(socket.resistant_started)
    await _activate(owner.id, request_id)
    await _event(socket.resistant_cancelled)
    await _ended(task)
    assert not socket.resistant_release.is_set()
    assert [state for _, state in await _rows(owner)] == ["uncertain"]
    assert (await _drain(owner, request_id)).registered_operations_drained is False
    socket.resistant_release.set()


async def test_caller_cancellation_propagates_after_confirmed_teardown(socket_env):
    socket, owner = socket_env
    socket.ack()
    task = socket.run()
    await _event(socket.ready_sent)
    task.cancel()
    await _ended(task, cancelled=True)
    assert socket.cleaned == 1
    assert socket.closes == [4503]
    assert await _rows(owner) == []


async def test_accept_failure_with_no_child_tasks_can_retire_confirmed_close(socket_env):
    socket, owner = socket_env
    socket.accept_error = True
    await _ended(socket.run())
    assert socket.closes == [4503]
    assert socket.cleaned == 0
    assert not socket.subscribe_started.is_set()
    assert await _rows(owner) == []


async def test_real_advisory_lock_wait_is_bounded_before_accept(socket_env, caplog, monkeypatch):
    socket, owner = socket_env
    monkeypatch.setattr(gateway, "WS_RECONCILE_SECONDS", 1)
    async with get_session_factory()() as held:
        await _lock_exclusive(held, uuid.UUID(owner.id))
        holder_pid = await held.scalar(text("SELECT pg_backend_pid()"))
        task = socket.run()
        async with asyncio.timeout(5):
            while True:
                async with get_session_factory()() as probe:
                    blocked = await probe.scalar(text(
                        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                        "WHERE :holder = ANY(pg_blocking_pids(pid)))"
                    ), {"holder": holder_pid})
                if blocked:
                    break
                assert not task.done(), "handshake ended before it reached the held admission gate"
                await asyncio.sleep(.01)
        await _ended(task)
        assert not socket.accepted.is_set()
        assert not socket.subscribe_started.is_set()
        assert "operation_registration_failed" in caplog.text
        await held.rollback()
    assert await _rows(owner) == []


async def test_real_redis_subscription_is_released_before_connection_row(socket_env, monkeypatch):
    socket, owner = socket_env
    url = get_settings().redis_url
    assert urlparse(url).hostname in {"localhost", "127.0.0.1", "::1"}
    redis = Redis.from_url(url, decode_responses=True)
    await asyncio.wait_for(redis.ping(), 5)
    monkeypatch.setattr(gateway, "get_redis", lambda: redis)
    channel = f"user:{owner.id}"
    task = socket.run()
    try:
        await _event(socket.ready_sent)
        assert [state for _, state in await _rows(owner)] == ["running"]
        assert get_engine().pool.checkedout() == 0
        assert await redis.pubsub_numsub(channel) == [(channel, 1)]
        socket.disconnect.set()
        await _ended(task)
        assert await _rows(owner) == []
        # Redis processes the TCP close asynchronously; wait for its own count.
        async with asyncio.timeout(5):
            while await redis.pubsub_numsub(channel) != [(channel, 0)]:
                await asyncio.sleep(.01)
        assert json.loads(socket.sent[0]) == {"type": "ready"}
    finally:
        socket.disconnect.set()
        await redis.aclose(close_connection_pool=True)
