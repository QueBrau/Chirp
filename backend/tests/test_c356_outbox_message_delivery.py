"""send_message's delivery intent must survive a crash and retry without duplicating
(board card c356). See DELIVERY-OUTBOX.md for the STORED/DELIVERED/READ contract.

Polls are NOT touched by this card -- test_poll_delivery_paths_are_unmodified_by_this_change
below is a smoke guard proving app/routers/polls.py has zero references to the new
outbox module and its pinned _broadcast/_prepare_broadcast shape is untouched.
"""
from __future__ import annotations

import inspect
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.db import get_session, get_session_factory
from app.services import outbox
from tests.conftest import MakeUser, RegisterDevice, b64, share_verified_campus


async def _open_group(client: AsyncClient, creator: Any, others: list[Any], title: str) -> str:
    response = await client.post(
        "/conversations",
        json={"kind": "group", "title": title, "member_user_ids": [o.id for o in others]},
        headers=creator.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _block(client: AsyncClient, blocker: Any, blocked: Any) -> None:
    response = await client.post(
        "/moderation/blocks", json={"blocked_id": blocked.id}, headers=blocker.headers
    )
    assert response.status_code == 201, response.text


async def _send(
    client: AsyncClient, conversation_id: str, sender: Any, device_id: str, plaintext: bytes
) -> Any:
    return await client.post(
        f"/conversations/{conversation_id}/messages",
        json={
            "sender_device_id": device_id,
            "ciphertext_b64": b64(plaintext),
            "message_type": "signal",
        },
        headers=sender.headers,
    )


async def _outbox_row_for(message_id: str) -> models.DeliveryOutbox | None:
    async with get_session_factory()() as session:
        result = await session.execute(
            select(models.DeliveryOutbox).where(
                models.DeliveryOutbox.payload["message_id"].astext == message_id
            )
        )
        return result.scalars().first()


async def test_outbox_row_and_message_row_share_one_transaction(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice,
) -> None:
    """Moving outbox.enqueue to AFTER session.commit would leave the message row
    behind while the outbox row does not, the instant commit is forced to fail right
    after the flush/refresh that populates message.id (acceptance criterion 2).
    """
    creator = await make_user("Creator")
    other = await make_user("Other")
    await share_verified_campus(creator.id, other.id)
    device = await register_device(creator, one_time_prekey_count=1)
    opened = await client.post(
        "/conversations", json={"kind": "dm", "member_user_ids": [other.id]},
        headers=creator.headers,
    )
    assert opened.status_code == 201, opened.text
    conversation_id = opened.json()["id"]

    app = client._transport.app

    async def crashing_session():
        async with get_session_factory()() as session:
            async def failing_commit() -> None:
                raise RuntimeError("c356 simulated crash before commit")

            session.commit = failing_commit  # type: ignore[method-assign]
            yield session

    app.dependency_overrides[get_session] = crashing_session
    try:
        with pytest.raises(RuntimeError, match="c356 simulated crash before commit"):
            await _send(client, conversation_id, creator, device["id"], b"never persisted")
    finally:
        app.dependency_overrides.pop(get_session, None)

    async with get_session_factory()() as session:
        message_count = await session.scalar(
            select(models.Message)
            .where(models.Message.conversation_id == uuid.UUID(conversation_id))
        )
        assert message_count is None, "the message must not have survived the failed commit"
        outbox_count = await session.execute(
            text(
                "SELECT COUNT(*) FROM delivery_outbox WHERE payload->>'conversation_id' = :cid"
            ),
            {"cid": conversation_id},
        )
        assert outbox_count.scalar_one() == 0, (
            "an outbox row surviving a rolled-back message row would mean the two "
            "writes are not sharing one transaction"
        )


async def test_crash_between_commit_and_publish_recovers_with_ciphertext_and_excludes_blockers(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creator = await make_user("Creator")
    blocker = await make_user("Blocker")
    bystander = await make_user("Bystander")
    await share_verified_campus(creator.id, blocker.id, bystander.id)
    device = await register_device(creator, one_time_prekey_count=1)
    conversation_id = await _open_group(client, creator, [blocker, bystander], "Group")
    await _block(client, blocker, creator)

    async def crashed_dispatch(*args: Any, **kwargs: Any) -> None:
        return None  # simulate the process dying right after the domain commit

    monkeypatch.setattr(outbox, "dispatch_now", crashed_dispatch)

    plaintext = b"opaque-group-ciphertext"
    sent = await _send(client, conversation_id, creator, device["id"], plaintext)
    assert sent.status_code == 201, sent.text
    message_id = sent.json()["id"]

    row = await _outbox_row_for(message_id)
    assert row is not None, "the row must exist even though live dispatch never ran"
    assert row.delivered_at is None
    assert row.dead_at is None
    # The group's active members minus the blocker (blockers_of excludes the sender
    # itself from candidacy, so the sender's own id stays a recipient -- multi-device
    # WS sync -- exactly as it does on today's live path).
    assert set(str(rid) for rid in row.recipient_ids) == {creator.id, bystander.id}
    assert "ciphertext" not in row.payload, "the outbox payload must never carry ciphertext"

    published: list[tuple[str, dict]] = []

    async def recording_publish(user_id: str, event: dict) -> None:
        published.append((user_id, event))

    import app.routers.messages as messages_router

    monkeypatch.setattr(messages_router, "publish_to_user", recording_publish)

    stats = await outbox.dispatch_pending(limit=10)
    assert stats.delivered == 1
    assert stats.retried == 0
    assert stats.dead == 0

    assert len(published) == 2, "exactly one publish call per surviving recipient"
    recipients_published = {user_id for user_id, _ in published}
    assert recipients_published == {creator.id, bystander.id}
    for _, event in published:
        assert event["message_id"] == message_id
        assert event["ciphertext"] == b64(plaintext), (
            "ciphertext must be hydrated fresh from the messages table, matching what "
            "was originally sent"
        )

    assert await _outbox_row_for(message_id) is None, "a fully delivered row is deleted"


async def test_redis_down_then_up_retries_without_duplicating(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creator = await make_user("Creator")
    other = await make_user("Other")
    await share_verified_campus(creator.id, other.id)
    device = await register_device(creator, one_time_prekey_count=1)
    opened = await client.post(
        "/conversations", json={"kind": "dm", "member_user_ids": [other.id]},
        headers=creator.headers,
    )
    conversation_id = opened.json()["id"]

    down = {"value": True}
    successful_publishes: list[str] = []

    async def flaky_publish(user_id: str, event: dict) -> None:
        if down["value"]:
            raise ConnectionError("redis down (c356 test)")
        successful_publishes.append(user_id)

    import app.routers.messages as messages_router

    monkeypatch.setattr(messages_router, "publish_to_user", flaky_publish)

    sent = await _send(client, conversation_id, creator, device["id"], b"retry-me")
    assert sent.status_code == 201, sent.text
    message_id = sent.json()["id"]

    row = await _outbox_row_for(message_id)
    assert row is not None
    assert row.attempts == 1
    assert row.dead_at is None
    assert row.next_attempt_at > datetime.now(timezone.utc), (
        "a naive immediate-retry loop would hot-loop the sweeper -- this must be pushed "
        "into the future"
    )
    recipients_after_failure = set(str(rid) for rid in row.recipient_ids)
    assert recipients_after_failure == {creator.id, other.id}

    # Sweeping again immediately must find nothing: the lease/backoff genuinely blocks
    # an eager re-poll (falsification target for "bump next_attempt_at").
    stats = await outbox.dispatch_pending(limit=10)
    assert stats.claimed == 0
    assert successful_publishes == []

    down["value"] = False
    async with get_session_factory()() as session:
        await session.execute(
            text("UPDATE delivery_outbox SET next_attempt_at = now() - interval '1 second' "
                 "WHERE id = :id"),
            {"id": row.id},
        )
        await session.commit()

    stats = await outbox.dispatch_pending(limit=10)
    assert stats.delivered == 1
    assert stats.retried == 0
    assert set(successful_publishes) == {creator.id, other.id}
    assert len(successful_publishes) == 2, (
        "each recipient must be successfully published to exactly once across the "
        "whole retry sequence, not doubled"
    )
    assert await _outbox_row_for(message_id) is None


async def test_attempts_cap_sets_dead_letter_and_stops_retrying(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    from app.config import get_settings

    creator = await make_user("Creator")
    other = await make_user("Other")
    await share_verified_campus(creator.id, other.id)
    device = await register_device(creator, one_time_prekey_count=1)

    async def always_fails(user_id: str, event: dict) -> None:
        raise ConnectionError("permanently down (c356 test)")

    import app.routers.messages as messages_router

    monkeypatch.setattr(messages_router, "publish_to_user", always_fails)

    opened = await client.post(
        "/conversations", json={"kind": "dm", "member_user_ids": [other.id]},
        headers=creator.headers,
    )
    sent = await _send(client, opened.json()["id"], creator, device["id"], b"doomed")
    message_id = sent.json()["id"]

    max_attempts = get_settings().outbox_max_attempts
    async with get_session_factory()() as session:
        await session.execute(
            text(
                "UPDATE delivery_outbox SET attempts = :n, next_attempt_at = now() - "
                "interval '1 second' WHERE payload->>'message_id' = :mid"
            ),
            {"n": max_attempts - 1, "mid": message_id},
        )
        await session.commit()

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.services.outbox"):
        stats = await outbox.dispatch_pending(limit=10)
    assert stats.dead == 1
    assert stats.delivered == 0
    assert stats.retried == 0

    row = await _outbox_row_for(message_id)
    assert row is not None, "a dead row is kept, not deleted (decision 2)"
    assert row.dead_at is not None
    assert row.attempts == max_attempts

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("outbox row dead" in m and str(row.id) in m for m in warnings)
    joined = " ".join(warnings)
    assert "doomed" not in joined
    assert "ConnectionError" not in joined
    assert "permanently down" not in joined

    stats_again = await outbox.dispatch_pending(limit=10)
    assert stats_again.claimed == 0, "a dead row must never be claimed again"


def test_poll_delivery_paths_are_unmodified_by_this_change() -> None:
    """polls.py is not touched in this PR -- see DELIVERY-OUTBOX.md's 'Not covered'."""
    from app.routers import polls

    assert hasattr(polls, "_broadcast")
    assert hasattr(polls, "_prepare_broadcast")
    assert polls.POLL_BROADCAST_TIMEOUT_SECONDS == 1.0
    assert inspect.iscoroutinefunction(polls._broadcast)
    assert list(inspect.signature(polls._broadcast).parameters) == ["batch"]
    assert inspect.signature(polls._broadcast).return_annotation in (None, "None")

    source = Path(polls.__file__).read_text()
    assert "outbox" not in source, "polls.py must have zero references to the outbox module"
