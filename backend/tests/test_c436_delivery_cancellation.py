"""Cancellation and gate-order regressions for admitted outbox publication."""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text

from app import models
from app.db import get_session_factory
from app.services import account_delivery_admission as admission
from app.services import outbox
from app.services.account_deletion_hold import activate_deletion_hold
from tests.test_c436_delivery_admission import (
    _attempts,
    _pending_row,
    _request_deletion,
    _seed_outbox,
    delivery_admission,
)


@pytest.mark.asyncio
async def test_cancel_after_all_dispatch_children_finish_handles_empty_pending_set(
    client, make_user, monkeypatch, delivery_admission,
):
    target = await make_user("Cancel after child completion")
    target_id = uuid.UUID(target.id)
    row_id = await _seed_outbox([target_id])
    published = asyncio.Event()

    async def publish(recipients, event, sender_id):
        published.set()
        return []

    monkeypatch.setitem(outbox._dispatchers, "poll", publish)
    original_wait = admission.asyncio.wait
    original_sleep = asyncio.sleep
    injected = False

    async def cancel_after_done(awaitables, *, timeout=None, return_when=asyncio.ALL_COMPLETED):
        nonlocal injected
        done, pending = await original_wait(
            awaitables, timeout=timeout, return_when=return_when,
        )
        if not injected and done and not pending:
            injected = True
            asyncio.current_task().cancel()
            await original_sleep(0)
        return done, pending

    monkeypatch.setattr(admission.asyncio, "wait", cancel_after_done)
    task = asyncio.create_task(outbox.dispatch_pending(limit=1))
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)

    assert published.is_set() and injected
    assert await _attempts(user_id=target_id) == []
    assert await _pending_row(row_id) is None


@pytest.mark.asyncio
async def test_repeated_cancellation_keeps_terminal_database_completion_owned(
    client, make_user, monkeypatch, delivery_admission,
):
    target = await make_user("Repeated cancel during terminal write")
    target_id = uuid.UUID(target.id)
    row_id = await _seed_outbox([target_id])
    entered_finish, release_finish, finish_done = (
        asyncio.Event(), asyncio.Event(), asyncio.Event(),
    )
    original_finish = admission._finish_and_release

    async def publish(recipients, event, sender_id):
        return []

    async def paused_finish(*args, **kwargs):
        entered_finish.set()
        try:
            await release_finish.wait()
            return await original_finish(*args, **kwargs)
        finally:
            finish_done.set()

    monkeypatch.setitem(outbox._dispatchers, "poll", publish)
    monkeypatch.setattr(admission, "_finish_and_release", paused_finish)
    task = asyncio.create_task(outbox.dispatch_pending(limit=1))
    await asyncio.wait_for(entered_finish.wait(), 5)

    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)

    # The outer task has propagated cancellation, but its shielded SQL finalizer
    # remains strongly owned and must settle the exact attempt before this passes.
    release_finish.set()
    await asyncio.wait_for(finish_done.wait(), 5)
    for _ in range(50):
        if not await _attempts(user_id=target_id):
            break
        await asyncio.sleep(0.02)
    assert await _attempts(user_id=target_id) == []
    assert await _pending_row(row_id) is None


@pytest.mark.asyncio
async def test_cancellation_propagates_if_shielded_finalizer_fails(
    client, make_user, monkeypatch, delivery_admission,
):
    target = await make_user("Cancel while terminal write fails")
    target_id = uuid.UUID(target.id)
    row_id = await _seed_outbox([target_id])
    finish_started, release_finish = asyncio.Event(), asyncio.Event()

    async def publish(recipients, event, sender_id):
        return []

    async def failing_finish(*args, **kwargs):
        finish_started.set()
        await release_finish.wait()
        raise RuntimeError("simulated terminal transaction failure")

    monkeypatch.setitem(outbox._dispatchers, "poll", publish)
    monkeypatch.setattr(admission, "_finish_and_release", failing_finish)
    task = asyncio.create_task(outbox.dispatch_pending(limit=1))
    await asyncio.wait_for(finish_started.wait(), 5)

    task.cancel()
    await asyncio.sleep(0)
    release_finish.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)

    # Failed terminal SQL leaves the committed ownership evidence and queue claim
    # intact; swallowed cancellation would falsely return a normal result here.
    attempts = await _attempts(user_id=target_id)
    assert len(attempts) == 1 and attempts[0].state == "running"
    assert await _pending_row(row_id) is not None


@pytest.mark.asyncio
async def test_hold_gate_wait_is_visible_in_postgres_and_suppresses_after_commit(
    client, make_user, monkeypatch, delivery_admission,
):
    target = await make_user("Admission waits behind hold gate")
    target_id = uuid.UUID(target.id)
    request_id = await _request_deletion(client, target)
    row_id = await _seed_outbox([target_id])
    gate_acquired, release_activation = asyncio.Event(), asyncio.Event()
    published = []

    async def publish(recipients, event, sender_id):
        published.extend(recipients)
        return []

    monkeypatch.setitem(outbox._dispatchers, "poll", publish)

    async def activate_and_pause():
        async with get_session_factory()() as session:
            await activate_deletion_hold(
                session, user_id=target_id, request_id=request_id,
            )
            gate_acquired.set()
            await release_activation.wait()
            await session.commit()

    activation = asyncio.create_task(activate_and_pause())
    await asyncio.wait_for(gate_acquired.wait(), 5)
    worker = asyncio.create_task(outbox.dispatch_pending(limit=1))

    async def waiting_backend_pid() -> int | None:
        async with get_session_factory()() as session:
            return await session.scalar(text("""
                SELECT pid
                  FROM pg_stat_activity
                 WHERE datname = current_database()
                   AND pid <> pg_backend_pid()
                   AND wait_event_type = 'Lock'
                   AND query ILIKE '%pg_advisory_xact_lock%'
                 ORDER BY query_start DESC
                 LIMIT 1
            """))

    try:
        async with asyncio.timeout(5):
            while await waiting_backend_pid() is None:
                await asyncio.sleep(0.01)
    except TimeoutError:
        release_activation.set()
        await activation
        await worker
        pytest.fail("delivery admission never appeared waiting on the account gate")

    release_activation.set()
    await asyncio.wait_for(activation, 5)
    result = await asyncio.wait_for(worker, 5)
    assert result.suppressed == 1
    assert published == []
    assert await _attempts(user_id=target_id) == []
    assert await _pending_row(row_id) is None
