"""dispatch_pending's lease must make concurrent sweepers bounded and idempotent, and
never hold a database session/connection/row lock across the publish attempt (board
card c356, manager override on the sweeper shape). See DELIVERY-OUTBOX.md.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.config import get_settings
from app.core import operational_signals as signals
from app.db import get_session_factory
from app.services import outbox
from tests.conftest import MakeUser, RegisterDevice, b64, share_verified_campus


async def _pending_row_id_for(message_id: str) -> uuid.UUID:
    async with get_session_factory()() as session:
        result = await session.execute(
            text("SELECT id FROM delivery_outbox WHERE payload->>'message_id' = :mid"),
            {"mid": message_id},
        )
        return result.scalar_one()


async def _row_lock_is_held(row_id: uuid.UUID) -> bool:
    """A THIRD connection's plain SELECT ... FOR UPDATE NOWAIT must raise if, and only
    if, another transaction genuinely holds this row's lock (vacuous-test rule: prove
    the lock is real before relying on it blocking anything).
    """
    async with get_session_factory()() as session:
        try:
            await session.execute(
                text("SELECT id FROM delivery_outbox WHERE id = :id FOR UPDATE NOWAIT"),
                {"id": row_id},
            )
        except DBAPIError:
            await session.rollback()
            return True
        else:
            await session.rollback()
            return False


async def test_bounded_workers_skip_locked_prevents_double_delivery(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creator = await make_user("Creator")
    other = await make_user("Other")
    await share_verified_campus(creator.id, other.id)
    device = await register_device(creator, one_time_prekey_count=1)

    async def crashed_dispatch(*args: Any, **kwargs: Any) -> None:
        return None  # leave the row pending, as if the process died right after commit

    monkeypatch.setattr(outbox, "dispatch_now", crashed_dispatch)

    opened = await client.post(
        "/conversations", json={"kind": "dm", "member_user_ids": [other.id]},
        headers=creator.headers,
    )
    sent = await client.post(
        f"/conversations/{opened.json()['id']}/messages",
        json={"sender_device_id": device["id"], "ciphertext_b64": b64(b"lease-test"),
              "message_type": "signal"},
        headers=creator.headers,
    )
    assert sent.status_code == 201, sent.text
    message_id = sent.json()["id"]
    row_id = await _pending_row_id_for(message_id)

    settings = get_settings()
    session_a = get_session_factory()()
    try:
        # Session A plays the sweeper's own transaction-1 claim, by hand, and does
        # NOT commit yet -- exactly the window a real sweeper holds between the
        # claim UPDATE and its commit.
        claimed = await session_a.execute(
            outbox._CLAIM_SQL, {"lease_s": settings.outbox_lease_s, "n": 1}
        )
        claimed_rows = claimed.mappings().all()
        assert len(claimed_rows) == 1
        assert claimed_rows[0]["id"] == row_id

        # Prove the lock is genuinely held before relying on it blocking anything.
        assert await _row_lock_is_held(row_id) is True

        started = time.monotonic()
        stats_b = await outbox.dispatch_pending(limit=1)
        elapsed = time.monotonic() - started
        assert stats_b.claimed == 0, "SKIP LOCKED must make B skip A's held row entirely"
        assert elapsed < settings.db_pool_timeout / 2, (
            f"B must return fast, not block on A's row lock (took {elapsed:.3f}s)"
        )

        await session_a.commit()
    finally:
        await session_a.close()

    async with get_session_factory()() as session:
        result = await session.execute(
            text("SELECT attempts, next_attempt_at FROM delivery_outbox WHERE id = :id"),
            {"id": row_id},
        )
        row_after_claim = result.mappings().one()
        assert row_after_claim["attempts"] == 1, "A's own claim must have bumped attempts exactly once"
        assert row_after_claim["next_attempt_at"] > datetime.now(timezone.utc), (
            "the claim itself is the lease -- next_attempt_at must already be pushed "
            "into the future the instant A commits, not only once a publish outcome "
            "is later recorded"
        )

    # A committed its claim (attempts bumped) but never ran a publish attempt or
    # recorded an outcome -- exactly "crash between the claim and the outcome
    # transaction". If the claim itself does not push next_attempt_at into the
    # future, this immediate second sweep re-claims the same row before anyone
    # has actually tried to deliver it (falsification target (iii)).
    stats_immediate = await outbox.dispatch_pending(limit=10)
    assert stats_immediate.claimed == 0, (
        "a row claimed but not yet outcome-recorded must not be reclaimable until "
        "its lease actually expires"
    )
    async with get_session_factory()() as session:
        result = await session.execute(
            text("SELECT attempts FROM delivery_outbox WHERE id = :id"), {"id": row_id}
        )
        assert result.scalar_one() == 1, "attempts must still be exactly 1 -- no double-claim"

    published: list[str] = []

    async def recording_publish(user_id: str, event: dict) -> None:
        published.append(user_id)

    import app.routers.messages as messages_router

    monkeypatch.setattr(messages_router, "publish_to_user", recording_publish)

    # Finish delivery: the lease A set is still in the future, so nothing is claimable
    # until it lapses -- time-travel it into the past, matching test_redis_down_then_up's
    # pattern, then let a real sweep complete the row.
    async with get_session_factory()() as session:
        await session.execute(
            text("UPDATE delivery_outbox SET next_attempt_at = now() - interval '1 second' "
                 "WHERE id = :id"),
            {"id": row_id},
        )
        await session.commit()

    stats_finish = await outbox.dispatch_pending(limit=1)
    assert stats_finish.delivered == 1
    assert len(published) == 2, "exactly one publish per recipient, exactly once total"
    assert set(published) == {creator.id, other.id}


async def test_concurrent_dispatch_pending_delivers_five_rows_exactly_once_each(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creator = await make_user("Creator")
    other = await make_user("Other")
    await share_verified_campus(creator.id, other.id)
    device = await register_device(creator, one_time_prekey_count=1)

    async def crashed_dispatch(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(outbox, "dispatch_now", crashed_dispatch)

    opened = await client.post(
        "/conversations", json={"kind": "dm", "member_user_ids": [other.id]},
        headers=creator.headers,
    )
    conversation_id = opened.json()["id"]
    message_ids = []
    for i in range(5):
        sent = await client.post(
            f"/conversations/{conversation_id}/messages",
            json={"sender_device_id": device["id"], "ciphertext_b64": b64(f"msg-{i}".encode()),
                  "message_type": "signal"},
            headers=creator.headers,
        )
        assert sent.status_code == 201, sent.text
        message_ids.append(sent.json()["id"])
    assert len(set(message_ids)) == 5

    calls: list[tuple[str, str]] = []
    lock = asyncio.Lock()

    async def recording_publish(user_id: str, event: dict) -> None:
        async with lock:
            calls.append((user_id, event["message_id"]))

    import app.routers.messages as messages_router

    monkeypatch.setattr(messages_router, "publish_to_user", recording_publish)

    results = await asyncio.gather(
        outbox.dispatch_pending(limit=10), outbox.dispatch_pending(limit=10)
    )
    total_delivered = sum(r.delivered for r in results)
    assert total_delivered == 5, "every row must be delivered exactly once across both sweeps"

    per_message = {}
    for user_id, message_id in calls:
        per_message.setdefault(message_id, set()).add(user_id)
    assert set(per_message.keys()) == set(message_ids)
    for message_id in message_ids:
        assert per_message[message_id] == {creator.id, other.id}, (
            f"message {message_id} must be published to each recipient exactly once"
        )

    async with get_session_factory()() as session:
        remaining = await session.execute(
            text("SELECT COUNT(*) FROM delivery_outbox WHERE payload->>'conversation_id' = :cid"),
            {"cid": conversation_id},
        )
        assert remaining.scalar_one() == 0, "every delivered row must be gone afterward"


def _records(caplog: pytest.LogCaptureFixture) -> list[dict]:
    return [
        json.loads(r.getMessage()) for r in caplog.records if r.name == "app.operational"
    ]


async def test_queue_age_signal_reports_pending_count_and_oldest_age(
    caplog: pytest.LogCaptureFixture,
) -> None:
    signals._reset_for_tests()
    async with get_session_factory()() as session:
        await session.execute(
            text(
                "INSERT INTO delivery_outbox (kind, recipient_ids, payload, created_at) "
                "VALUES ('message', ARRAY[:rid]::uuid[], (:payload)::jsonb, "
                "now() - interval '10 minutes')"
            ),
            {"rid": str(uuid.uuid4()), "payload": json.dumps({"message_id": str(uuid.uuid4())})},
        )
        await session.commit()

    pending, oldest_age_seconds = await outbox.queue_stats()
    assert pending == 1
    assert oldest_age_seconds >= 600

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.operational"):
        signals.report_queue_age(pending, oldest_age_seconds)

    records = _records(caplog)
    assert len(records) == 1
    record = records[0]
    assert record["event"] == "outbox_queue_age"
    assert record["pending"] == 1
    assert record["oldest_age_seconds"] >= 600
    assert set(record.keys()) == {
        "schema_version", "signal_family", "event", "severity", "observation_scope",
        "sampled", "pending", "oldest_age_seconds",
    }, "no ids, no ciphertext -- counts only"
