"""Real-PostgreSQL delivery admission tests at the outbox public entrypoints."""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select, text

from app import models
from app.config import get_settings
from app.db import get_engine, get_session_factory
from app.services import outbox
from app.services.account_deletion_hold import activate_deletion_hold
from app.services.account_external_operations import inspect_registered_operation_drain


async def _request_deletion(client, user) -> uuid.UUID:
    response = await client.post(
        "/me/data-requests", json={"kind": "deletion"}, headers=user.headers,
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


async def _activate(user_id: uuid.UUID, request_id: uuid.UUID) -> None:
    async with get_session_factory()() as session:
        await activate_deletion_hold(session, user_id=user_id, request_id=request_id)
        await session.commit()


async def _seed_outbox(recipients: list[uuid.UUID], *, event=None) -> uuid.UUID:
    row = models.DeliveryOutbox(
        kind="poll",
        recipient_ids=recipients,
        payload=event or {"type": "poll", "poll_id": str(uuid.uuid4()), "snapshot": 1},
    )
    async with get_session_factory()() as session:
        session.add(row)
        await session.flush()
        row_id = row.id
        await session.commit()
    return row_id


async def _attempts(*, user_id: uuid.UUID | None = None):
    async with get_session_factory()() as session:
        stmt = select(models.AccountDeliveryAttempt)
        if user_id is not None:
            stmt = stmt.where(models.AccountDeliveryAttempt.user_id == user_id)
        return list((await session.scalars(stmt.order_by(
            models.AccountDeliveryAttempt.user_id,
            models.AccountDeliveryAttempt.slot,
        ))).all())


async def _pending_row(row_id: uuid.UUID):
    async with get_session_factory()() as session:
        return await session.get(models.DeliveryOutbox, row_id)


async def _make_due(row_id: uuid.UUID) -> None:
    async with get_session_factory()() as session:
        await session.execute(text(
            "UPDATE delivery_outbox SET next_attempt_at = now() WHERE id = :id"
        ), {"id": row_id})
        await session.commit()


@pytest.fixture
def delivery_admission(monkeypatch):
    monkeypatch.setattr(
        get_settings(), "account_deletion_delivery_admission_enabled", True,
    )


@pytest.mark.asyncio
async def test_held_recipient_is_suppressed_while_other_recipients_receive(
    client, make_user, monkeypatch, delivery_admission,
):
    held = await make_user("Held delivery target")
    available = await make_user("Available delivery target")
    held_id, available_id = uuid.UUID(held.id), uuid.UUID(available.id)
    request_id = await _request_deletion(client, held)
    await _activate(held_id, request_id)
    row_id = await _seed_outbox([held_id, available_id])
    delivered = []

    async def publish(recipients, event, sender_id):
        assert len(recipients) == 1, "enabled delivery should admit one recipient effect at a time"
        delivered.append((recipients[0], event))
        return []

    monkeypatch.setitem(outbox._dispatchers, "poll", publish)
    stats = await outbox.dispatch_pending(limit=10)

    assert [recipient for recipient, _ in delivered] == [available_id]
    assert await _pending_row(row_id) is None
    assert await _attempts() == []
    assert getattr(stats, "suppressed", None) == 1


@pytest.mark.asyncio
async def test_enabled_live_entrypoint_uses_committed_outbox_event(
    client, make_user, monkeypatch, delivery_admission,
):
    target = await make_user("Live delivery target")
    target_id = uuid.UUID(target.id)
    persisted_event = {"type": "poll", "poll_id": str(uuid.uuid4()), "snapshot": "stored"}
    supplied_event = {"type": "poll", "poll_id": "untrusted-live-copy", "snapshot": "memory"}
    row_id = await _seed_outbox([target_id], event=persisted_event)
    seen = []

    async def publish(recipients, event, sender_id):
        seen.append((list(recipients), event))
        return []

    monkeypatch.setitem(outbox._dispatchers, "poll", publish)
    async with get_session_factory()() as session:
        await outbox.dispatch_now(
            session, row_id, kind="poll", recipient_ids=[target_id],
            event=supplied_event, sender_id=None,
        )

    assert seen == [([target_id], persisted_event)]
    assert await _pending_row(row_id) is None
    assert await _attempts() == []


@pytest.mark.asyncio
async def test_hold_during_publish_waits_for_registered_effect_without_sql_checkout(
    client, make_user, monkeypatch, delivery_admission,
):
    target = await make_user("In-flight delivery target")
    target_id = uuid.UUID(target.id)
    request_id = await _request_deletion(client, target)
    row_id = await _seed_outbox([target_id])
    entered, release = asyncio.Event(), asyncio.Event()
    observed_pool_counts = []

    async def blocked_publish(recipients, event, sender_id):
        assert recipients == [target_id]
        observed_pool_counts.append(get_engine().pool.checkedout())
        entered.set()
        await release.wait()
        return []

    monkeypatch.setitem(outbox._dispatchers, "poll", blocked_publish)
    task = asyncio.create_task(outbox.dispatch_pending(limit=1))
    await asyncio.wait_for(entered.wait(), 5)
    assert observed_pool_counts == [0]

    await _activate(target_id, request_id)
    waiting = await inspect_registered_operation_drain(
        user_id=target_id, request_id=request_id,
    )
    assert waiting.running_count == 1 and not waiting.registered_operations_drained

    release.set()
    await asyncio.wait_for(task, 5)
    drained = await inspect_registered_operation_drain(
        user_id=target_id, request_id=request_id,
    )
    assert drained.registered_operations_drained
    assert await _pending_row(row_id) is None
    assert await _attempts(user_id=target_id) == []


@pytest.mark.asyncio
async def test_concurrent_sweepers_do_not_publish_same_claim_twice(
    client, make_user, monkeypatch, delivery_admission,
):
    target = await make_user("Concurrent delivery target")
    target_id = uuid.UUID(target.id)
    row_id = await _seed_outbox([target_id])
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def blocked_publish(recipients, event, sender_id):
        calls.extend(recipients)
        entered.set()
        await release.wait()
        return []

    monkeypatch.setitem(outbox._dispatchers, "poll", blocked_publish)
    first = asyncio.create_task(outbox.dispatch_pending(limit=1))
    await asyncio.wait_for(entered.wait(), 5)
    second = await asyncio.wait_for(outbox.dispatch_pending(limit=1), 5)
    assert calls == [target_id]
    assert second.claimed == 0
    release.set()
    await asyncio.wait_for(first, 5)
    assert calls == [target_id]
    assert await _pending_row(row_id) is None


@pytest.mark.asyncio
async def test_live_and_sweeper_paths_share_one_recipient_claim(
    client, make_user, monkeypatch, delivery_admission,
):
    target = await make_user("Live and sweeper target")
    target_id = uuid.UUID(target.id)
    event = {"type": "poll", "poll_id": str(uuid.uuid4()), "snapshot": 1}
    row_id = await _seed_outbox([target_id], event=event)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def blocked_publish(recipients, event, sender_id):
        calls.extend(recipients)
        entered.set()
        await release.wait()
        return []

    monkeypatch.setitem(outbox._dispatchers, "poll", blocked_publish)

    async def live_dispatch():
        async with get_session_factory()() as session:
            await outbox.dispatch_now(
                session, row_id, kind="poll", recipient_ids=[target_id],
                event=event, sender_id=None,
            )

    live = asyncio.create_task(live_dispatch())
    await asyncio.wait_for(entered.wait(), 5)
    worker_stats = await asyncio.wait_for(outbox.dispatch_pending(limit=1), 5)
    assert worker_stats.claimed == 0
    assert calls == [target_id]
    release.set()
    await asyncio.wait_for(live, 5)
    assert calls == [target_id]
    assert await _pending_row(row_id) is None


@pytest.mark.asyncio
async def test_expired_claim_advances_past_running_recipient_without_replay(
    client, make_user, monkeypatch, delivery_admission,
):
    target = await make_user("Expired delivery claim target")
    target_id = uuid.UUID(target.id)
    row_id = await _seed_outbox([target_id])
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def blocked_publish(recipients, event, sender_id):
        calls.extend(recipients)
        entered.set()
        await release.wait()
        return []

    monkeypatch.setitem(outbox._dispatchers, "poll", blocked_publish)
    old_worker = asyncio.create_task(outbox.dispatch_pending(limit=1))
    await asyncio.wait_for(entered.wait(), 5)

    # Model lease expiry while the original provider call is still unresolved.
    async with get_session_factory()() as session:
        await session.execute(text(
            "UPDATE delivery_outbox SET next_attempt_at = now() - interval '1 second' "
            "WHERE id = :id"
        ), {"id": row_id})
        await session.commit()
    replacement_stats = await asyncio.wait_for(outbox.dispatch_pending(limit=1), 5)
    assert replacement_stats.claimed == 1, "expired claim should be reclaimed and advanced"
    assert calls == [target_id], "a committed running attempt must never be invoked twice"

    release.set()
    await asyncio.wait_for(old_worker, 5)
    assert calls == [target_id]
    assert await _pending_row(row_id) is None
    assert await _attempts(user_id=target_id) == []


@pytest.mark.asyncio
async def test_coalescing_source_row_does_not_erase_uncertain_delivery_evidence(
    client, make_user, monkeypatch, delivery_admission,
):
    target = await make_user("Coalesced delivery target")
    target_id = uuid.UUID(target.id)
    request_id = await _request_deletion(client, target)
    old_row = await _seed_outbox([target_id])
    entered, release = asyncio.Event(), asyncio.Event()

    async def ambiguous_publish(recipients, event, sender_id):
        entered.set()
        await release.wait()
        raise TimeoutError("simulated ambiguous Redis result")

    monkeypatch.setitem(outbox._dispatchers, "poll", ambiguous_publish)
    task = asyncio.create_task(outbox.dispatch_pending(limit=1))
    await asyncio.wait_for(entered.wait(), 5)

    # Poll coalescing may delete the claimed source row while its copied event is
    # in flight. The independent recipient attempt must survive that deletion.
    async with get_session_factory()() as session:
        await session.execute(
            text("DELETE FROM delivery_outbox WHERE id = :id"), {"id": old_row},
        )
        await session.commit()
    release.set()
    await asyncio.wait_for(task, 5)

    attempts = await _attempts(user_id=target_id)
    assert len(attempts) == 1 and attempts[0].state == "uncertain"
    await _activate(target_id, request_id)
    drain = await inspect_registered_operation_drain(
        user_id=target_id, request_id=request_id,
    )
    assert drain.uncertain_count == 1 and not drain.registered_operations_drained


@pytest.mark.asyncio
async def test_cancellation_resistant_publish_is_bounded_and_remains_a_drain_blocker(
    client, make_user, monkeypatch, delivery_admission,
):
    target = await make_user("Cancellation-resistant delivery target")
    target_id = uuid.UUID(target.id)
    request_id = await _request_deletion(client, target)
    row_id = await _seed_outbox([target_id])
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(get_settings(), "outbox_dispatch_timeout_s", 0.1)

    async def resistant_publish(recipients, event, sender_id):
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
        return []

    monkeypatch.setitem(outbox._dispatchers, "poll", resistant_publish)
    task = asyncio.create_task(outbox.dispatch_pending(limit=1))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.wait_for(cancelled.wait(), 5)
        await asyncio.wait_for(task, 5)
        attempts = await _attempts(user_id=target_id)
        assert len(attempts) == 1
        assert attempts[0].state in {"running", "uncertain"}
        await _activate(target_id, request_id)
        drain = await inspect_registered_operation_drain(
            user_id=target_id, request_id=request_id,
        )
        assert not drain.registered_operations_drained
        assert await _pending_row(row_id) is None
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_full_recipient_slots_defer_only_that_recipient_then_reuse_slot(
    client, make_user, monkeypatch, delivery_admission,
):
    full = await make_user("Delivery slot full")
    available = await make_user("Delivery slot available")
    full_id, available_id = uuid.UUID(full.id), uuid.UUID(available.id)
    async with get_session_factory()() as session:
        session.add_all(
            models.AccountDeliveryAttempt(
                user_id=full_id,
                slot=slot,
                outbox_id=uuid.uuid4(),
                owner_token=uuid.uuid4(),
                state="uncertain",
            )
            for slot in range(32)
        )
        await session.commit()
    row_id = await _seed_outbox([full_id, available_id])
    delivered = []

    async def publish(recipients, event, sender_id):
        delivered.extend(recipients)
        return []

    monkeypatch.setitem(outbox._dispatchers, "poll", publish)
    await outbox.dispatch_pending(limit=1)
    assert delivered == [available_id]
    row = await _pending_row(row_id)
    assert row is not None and list(row.recipient_ids) == [full_id]
    assert len(await _attempts(user_id=full_id)) == 32

    async with get_session_factory()() as session:
        victim = await session.scalar(
            select(models.AccountDeliveryAttempt)
            .where(models.AccountDeliveryAttempt.user_id == full_id)
            .limit(1)
        )
        await session.delete(victim)
        await session.execute(text(
            "UPDATE delivery_outbox SET next_attempt_at = now() - interval '1 second' "
            "WHERE id = :id"
        ), {"id": row_id})
        await session.commit()
    await outbox.dispatch_pending(limit=1)
    assert delivered == [available_id, full_id]
    assert await _pending_row(row_id) is None
    assert len(await _attempts(user_id=full_id)) == 31


@pytest.mark.asyncio
async def test_delivery_batches_large_roster_without_the_strict_message_scope_cap(
    client, make_user, monkeypatch, delivery_admission,
):
    first = await make_user("Large delivery roster first")
    first_id = uuid.UUID(first.id)
    other_ids = [uuid.uuid4() for _ in range(259)]
    async with get_session_factory()() as session:
        session.add_all(
            models.User(
                id=user_id,
                firebase_uid=f"delivery-roster-{user_id}",
                email=f"delivery-roster-{user_id}@example.edu",
                display_name="Large delivery roster member",
                account_type="greek",
            )
            for user_id in other_ids
        )
        await session.commit()
    recipients = [first_id, *other_ids]
    request_id = await _request_deletion(client, first)
    row_id = await _seed_outbox(recipients)
    delivered = []
    attempted = []

    async def publish(singleton, event, sender_id):
        assert len(singleton) == 1
        attempted.extend(singleton)
        if singleton[0] == first_id:
            return [str(first_id)]
        delivered.extend(singleton)
        return []

    monkeypatch.setitem(outbox._dispatchers, "poll", publish)
    first_stats = await outbox.dispatch_pending(limit=1)
    assert first_stats.claimed == 1
    assert len(attempted) == 8
    assert len(delivered) == 7  # the first recipient is deliberately uncertain
    row = await _pending_row(row_id)
    assert row is not None and len(row.recipient_ids) == 252

    for _ in range(32):
        await _make_due(row_id)
        await outbox.dispatch_pending(limit=1)
    assert len(attempted) == 260
    assert len(delivered) == 259
    assert set(delivered) == set(recipients) - {first_id}
    assert await _pending_row(row_id) is None
    unresolved = await _attempts(user_id=first_id)
    assert len(unresolved) == 1 and unresolved[0].state == "uncertain"
    await _activate(first_id, request_id)
    drain = await inspect_registered_operation_drain(
        user_id=first_id, request_id=request_id,
    )
    assert drain.uncertain_count == 1 and not drain.registered_operations_drained


@pytest.mark.asyncio
async def test_confirmed_recipient_survives_sibling_timeout_as_separate_outcome(
    client, make_user, monkeypatch, delivery_admission,
):
    confirmed = await make_user("Confirmed sibling delivery")
    ambiguous = await make_user("Timed-out sibling delivery")
    confirmed_id, ambiguous_id = uuid.UUID(confirmed.id), uuid.UUID(ambiguous.id)
    request_id = await _request_deletion(client, ambiguous)
    row_id = await _seed_outbox([confirmed_id, ambiguous_id])
    monkeypatch.setattr(get_settings(), "outbox_dispatch_timeout_s", 0.1)
    started = asyncio.Event()

    async def publish(recipients, event, sender_id):
        recipient = recipients[0]
        if recipient == confirmed_id:
            return []
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setitem(outbox._dispatchers, "poll", publish)
    stats = await outbox.dispatch_pending(limit=1)
    assert stats.successful_recipients == 1 and stats.uncertain == 1
    assert started.is_set()
    attempts = await _attempts()
    assert len(attempts) == 1
    assert attempts[0].user_id == ambiguous_id and attempts[0].state == "uncertain"
    assert await _pending_row(row_id) is None
    await _activate(ambiguous_id, request_id)
    drain = await inspect_registered_operation_drain(
        user_id=ambiguous_id, request_id=request_id,
    )
    assert drain.uncertain_count == 1 and not drain.registered_operations_drained


@pytest.mark.asyncio
async def test_unstarted_tail_stays_queued_until_concurrency_slots_open(
    client, make_user, monkeypatch, delivery_admission,
):
    targets = [await make_user(f"Bounded delivery target {i}") for i in range(10)]
    recipient_ids = [uuid.UUID(target.id) for target in targets]
    row_id = await _seed_outbox(recipient_ids)
    entered, release = asyncio.Event(), asyncio.Event()
    started = []

    async def blocked_publish(recipients, event, sender_id):
        started.extend(recipients)
        if len(started) == 8:
            entered.set()
        await release.wait()
        return []

    monkeypatch.setitem(outbox._dispatchers, "poll", blocked_publish)
    first = asyncio.create_task(outbox.dispatch_pending(limit=1))
    await asyncio.wait_for(entered.wait(), 5)
    assert len(started) == 8
    row = await _pending_row(row_id)
    assert row is not None and list(row.recipient_ids) == recipient_ids[8:]
    assert {attempt.user_id for attempt in await _attempts()} == set(recipient_ids[:8])
    release.set()
    await asyncio.wait_for(first, 5)
    row = await _pending_row(row_id)
    assert row is not None and list(row.recipient_ids) == recipient_ids[8:]
    await _make_due(row_id)
    await outbox.dispatch_pending(limit=1)
    assert await _pending_row(row_id) is None
    assert {attempt.user_id for attempt in await _attempts()} == set()


@pytest.mark.asyncio
async def test_old_worker_finishes_owned_attempt_without_mutating_new_queue_claim(
    client, make_user, monkeypatch, delivery_admission,
):
    target = await make_user("Stale queue claim recipient")
    replacement = await make_user("Replacement queue recipient")
    target_id, replacement_id = uuid.UUID(target.id), uuid.UUID(replacement.id)
    row_id = await _seed_outbox([target_id])
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_publish(recipients, event, sender_id):
        entered.set()
        await release.wait()
        return []

    monkeypatch.setitem(outbox._dispatchers, "poll", blocked_publish)
    worker = asyncio.create_task(outbox.dispatch_pending(limit=1))
    await asyncio.wait_for(entered.wait(), 5)
    replacement_token = uuid.uuid4()
    async with get_session_factory()() as session:
        await session.execute(text(
            "UPDATE delivery_outbox SET claim_token = :token, recipient_ids = :recipients "
            "WHERE id = :id"
        ), {"token": replacement_token, "recipients": [replacement_id], "id": row_id})
        await session.commit()

    release.set()
    result = await asyncio.wait_for(worker, 5)
    assert result.successful_recipients == 1
    async with get_session_factory()() as session:
        row = (await session.execute(text(
            "SELECT claim_token, recipient_ids FROM delivery_outbox WHERE id = :id"
        ), {"id": row_id})).one()
    assert row.claim_token == replacement_token
    assert list(row.recipient_ids) == [replacement_id]
    assert await _attempts(user_id=target_id) == []


@pytest.mark.asyncio
async def test_wrong_attempt_token_cannot_clear_evidence_or_report_success(
    client, make_user, monkeypatch, delivery_admission,
):
    target = await make_user("Fenced attempt completion recipient")
    target_id = uuid.UUID(target.id)
    row_id = await _seed_outbox([target_id])
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_success(recipients, event, sender_id):
        entered.set()
        await release.wait()
        return []

    monkeypatch.setitem(outbox._dispatchers, "poll", blocked_success)
    worker = asyncio.create_task(outbox.dispatch_pending(limit=1))
    await asyncio.wait_for(entered.wait(), 5)
    changed_token = uuid.uuid4()
    async with get_session_factory()() as session:
        attempt = await session.scalar(select(models.AccountDeliveryAttempt))
        assert attempt is not None
        await session.execute(text(
            "UPDATE account_delivery_attempts SET owner_token = :new_token WHERE id = :id"
        ), {"new_token": changed_token, "id": attempt.id})
        await session.commit()

    release.set()
    result = await asyncio.wait_for(worker, 5)
    assert result.delivered == 0 and result.uncertain >= 1
    attempts = await _attempts(user_id=target_id)
    assert len(attempts) == 1 and attempts[0].owner_token == changed_token
    assert attempts[0].state == "running"
    assert await _pending_row(row_id) is not None


@pytest.mark.asyncio
async def test_default_off_preserves_legacy_dispatch_without_attempt_rows(
    client, make_user, monkeypatch,
):
    target = await make_user("Flag-off delivery target")
    target_id = uuid.UUID(target.id)
    request_id = await _request_deletion(client, target)
    await _activate(target_id, request_id)
    monkeypatch.setattr(
        get_settings(), "account_deletion_delivery_admission_enabled", False,
    )
    row_id = await _seed_outbox([target_id])
    seen = []

    async def legacy_publish(recipients, event, sender_id):
        seen.extend(recipients)
        return []

    monkeypatch.setitem(outbox._dispatchers, "poll", legacy_publish)
    await outbox.dispatch_pending(limit=1)
    assert seen == [target_id]
    assert await _pending_row(row_id) is None
    assert await _attempts() == []


@pytest.mark.asyncio
async def test_enabled_message_dispatch_preserves_v1_and_v2_wire_shapes(
    client, make_user, register_device, monkeypatch, delivery_admission,
):
    from app.routers import messages as messages_router
    from tests.conftest import share_verified_campus
    from tests.e2ee_v2_helpers import leg, send as send_v2
    from tests.test_c444_e2ee_v2_messages import _world
    from tests.test_c356_outbox_message_delivery import _send

    captured = []

    async def recording_publish(user_id: str, event: dict):
        captured.append((user_id, dict(event)))

    monkeypatch.setattr(messages_router, "publish_to_user", recording_publish)

    # Legacy v1 still carries exactly its established encrypted event fields.
    v1_sender = await make_user("Delivery v1 sender")
    v1_recipient = await make_user("Delivery v1 recipient")
    await share_verified_campus(v1_sender.id, v1_recipient.id)
    device = await register_device(v1_sender, one_time_prekey_count=1)
    opened = await client.post(
        "/conversations", json={"kind": "dm", "member_user_ids": [v1_recipient.id]},
        headers=v1_sender.headers,
    )
    assert opened.status_code == 201, opened.text
    sent = await _send(
        client, opened.json()["id"], v1_sender, device["id"], b"v1-ciphertext",
    )
    assert sent.status_code == 201, sent.text
    v1_events = [event for _, event in captured]
    assert len(v1_events) == 2
    assert all(set(event) == {
        "type", "conversation_id", "message_id", "sender_device_id", "ciphertext", "created_at",
    } for event in v1_events)
    assert all(event["message_id"] == sent.json()["id"] for event in v1_events)
    assert all("sender_id" not in event for event in v1_events)

    captured.clear()
    # E2EE v2 never puts a message-level ciphertext on the delivery event.
    world = await _world(client, make_user)
    sent_v2 = await send_v2(
        client, world.convo, world.a1,
        [leg(world.a2, body=b"for-a2"), leg(world.b1, body=b"for-b1")],
        expect=201,
    )
    assert sent_v2.status_code == 201, sent_v2.text
    v2_events = [event for _, event in captured]
    assert len(v2_events) == 2
    assert all(set(event) == {
        "type", "conversation_id", "message_id", "sender_device_id", "envelope_version", "created_at",
    } for event in v2_events)
    assert all(event["message_id"] == sent_v2.json()["id"] for event in v2_events)
    assert all(event["envelope_version"] == 1 and "ciphertext" not in event for event in v2_events)

