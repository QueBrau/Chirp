"""Focused review coverage for message admission and conversation locking."""
from __future__ import annotations

import asyncio
import base64
import uuid

import pytest
from sqlalchemy import func, select, text

from app import models
from app.config import get_settings
from app.db import get_engine, get_session_factory
from app.services.account_deletion_hold import activate_deletion_hold
from tests.conftest import share_verified_campus


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED", "true")
    get_settings.cache_clear()


async def _hold(user_id: str) -> None:
    async with get_session_factory()() as session:
        request = models.AccountDataRequest(
            user_id=uuid.UUID(user_id), kind="deletion", status="processing",
            open_key=f"review-{uuid.uuid4()}",
        )
        session.add(request)
        await session.flush()
        request_id = request.id
        await session.commit()
    async with get_session_factory()() as session:
        await activate_deletion_hold(
            session, user_id=uuid.UUID(user_id), request_id=request_id,
        )
        await session.commit()


async def _counts() -> tuple[int, int]:
    async with get_session_factory()() as session:
        messages = int(await session.scalar(select(func.count()).select_from(models.Message)))
        outbox = int(await session.scalar(select(func.count()).select_from(models.DeliveryOutbox)))
        return messages, outbox


@pytest.mark.asyncio
@pytest.mark.parametrize("hold_recipient", [False, True])
async def test_legacy_block_refusal_precedes_recipient_hold(
    client, make_user, register_device, monkeypatch: pytest.MonkeyPatch, hold_recipient: bool,
) -> None:
    """A named block stays indistinguishable from the same block plus a target hold."""
    _enable(monkeypatch)
    sender, recipient = await make_user("review sender"), await make_user("review recipient")
    await share_verified_campus(sender.id, recipient.id)
    device = await register_device(sender)
    created = await client.post(
        "/conversations", headers=sender.headers,
        json={"kind": "dm", "member_user_ids": [recipient.id]},
    )
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]
    blocked = await client.post(
        "/moderation/blocks", headers=recipient.headers,
        json={"blocked_id": sender.id},
    )
    assert blocked.status_code == 201, blocked.text
    if hold_recipient:
        await _hold(recipient.id)
    response = await client.post(
        f"/conversations/{conversation_id}/messages", headers=sender.headers,
        json={
            "sender_device_id": device["id"],
            "ciphertext_b64": base64.b64encode(b"review").decode(),
            "message_type": "signal",
        },
    )
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "recipient_not_reachable"}
    assert await _counts() == (0, 0)


async def _wait_for_parent_block(task: asyncio.Task, blocker_pid: int) -> None:
    """Require pg_blocking_pids evidence rather than a coroutine sleep heuristic."""
    async with asyncio.timeout(5):
        async with get_engine().connect() as observer:
            while True:
                assert not task.done(), "request completed before waiting on the parent lock"
                pids = await observer.scalars(
                    text(
                        "SELECT pid FROM pg_stat_activity "
                        "WHERE pid <> :blocker "
                        "AND :blocker = ANY(pg_blocking_pids(pid))"
                    ),
                    {"blocker": blocker_pid},
                )
                if pids.first() is not None:
                    return
                await asyncio.sleep(0.01)


async def _lock_parent(conversation_id: str):
    locker = get_session_factory()()
    await locker.execute(
        select(models.Conversation.id)
        .where(models.Conversation.id == uuid.UUID(conversation_id))
        .with_for_update()
    )
    pid = await locker.scalar(text("SELECT pg_backend_pid()"))
    return locker, int(pid)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["leave", "reuse"])
async def test_leave_and_dm_reuse_wait_for_parent_lock(
    client, make_user, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    _enable(monkeypatch)
    actor, target = await make_user("lock actor"), await make_user("lock target")
    await share_verified_campus(actor.id, target.id)
    created = await client.post(
        "/conversations", headers=actor.headers,
        json={"kind": "dm", "member_user_ids": [target.id]},
    )
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]
    if operation == "reuse":
        left = await client.post(f"/conversations/{conversation_id}/leave", headers=target.headers)
        assert left.status_code == 200, left.text

    locker, blocker_pid = await _lock_parent(conversation_id)
    task = None
    try:
        if operation == "leave":
            task = asyncio.create_task(
                client.post(f"/conversations/{conversation_id}/leave", headers=target.headers)
            )
        else:
            task = asyncio.create_task(client.post(
                "/conversations", headers=actor.headers,
                json={"kind": "dm", "member_user_ids": [target.id]},
            ))
        await _wait_for_parent_block(task, blocker_pid)
        await locker.commit()
        response = await asyncio.wait_for(task, 5)
        assert response.status_code == (200 if operation == "leave" else 200), response.text
        async with get_session_factory()() as session:
            member = await session.get(
                models.ConversationMember,
                (uuid.UUID(conversation_id), uuid.UUID(target.id)),
            )
            assert member is not None
            if operation == "leave":
                assert member.left_at is not None
            else:
                assert member.left_at is None
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await locker.close()
