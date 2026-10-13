"""Real message transactions coordinate with deletion without erasing shared history."""
import asyncio
import base64
import uuid

import pytest
from sqlalchemy import select, text

from app import models
from app.config import get_settings
from app.db import get_engine, get_session_factory
from app.services import outbox
from app.services.account_cleanup import cleanup_local_account
from app.services.account_deletion_hold import activate_deletion_hold
from tests.conftest import share_verified_campus
from tests.test_c436_deletion_hold import _observe_blocked


async def _retain_delivery(*_args, **_kwargs):
    # Exercise real atomic enqueue; leave delivery for its independently tested worker.
    return None


def _body(device):
    return {
        "sender_device_id": device["id"],
        "ciphertext_b64": base64.b64encode(b"shared opaque history").decode(),
        "message_type": "signal",
    }


async def _deletion_request(user_id):
    async with get_session_factory()() as session:
        row = models.AccountDataRequest(user_id=uuid.UUID(user_id), kind="deletion")
        session.add(row)
        await session.commit()
        return row.id


@pytest.mark.asyncio
async def test_message_commit_precedes_target_hold_and_later_send_is_refused(
    client, make_user, register_device, monkeypatch,
):
    alice, bob = await make_user("Writer"), await make_user("Deleting member")
    await share_verified_campus(alice.id, bob.id)
    device = await register_device(alice)
    created = await client.post("/conversations", headers=alice.headers, json={
        "kind": "dm", "member_user_ids": [bob.id],
    })
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]
    request_id = await _deletion_request(bob.id)
    monkeypatch.setenv("ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(outbox, "dispatch_now", _retain_delivery)
    original_enqueue = outbox.enqueue
    enqueued, release = asyncio.Event(), asyncio.Event()
    sender_pid = []

    async def pause_after_enqueue(session, **kwargs):
        row_id = await original_enqueue(session, **kwargs)
        sender_pid.append(await session.scalar(text("SELECT pg_backend_pid()")))
        enqueued.set()
        await release.wait()
        return row_id

    monkeypatch.setattr(outbox, "enqueue", pause_after_enqueue)
    sender = asyncio.create_task(client.post(
        f"/conversations/{conversation_id}/messages", headers=alice.headers,
        json=_body(device),
    ))
    activation = None
    checkpoint = asyncio.create_task(enqueued.wait())
    try:
        completed, _ = await asyncio.wait(
            [sender, checkpoint], timeout=10, return_when=asyncio.FIRST_COMPLETED,
        )
        if sender in completed:
            response = sender.result()
            pytest.fail(f"Send returned before enqueue checkpoint: {response.status_code} {response.text}")
        assert checkpoint in completed, "Send never reached its real enqueue checkpoint"
        async with get_session_factory()() as activator:
            activator_pid = await activator.scalar(text("SELECT pg_backend_pid()"))
            activation = asyncio.create_task(activate_deletion_hold(
                activator, user_id=uuid.UUID(bob.id), request_id=request_id,
            ))
            await _observe_blocked(activation, get_engine(), activator_pid, sender_pid[0])
            release.set()
            sent = await asyncio.wait_for(sender, 5)
            assert sent.status_code == 201, sent.text
            await asyncio.wait_for(activation, 5)
            await activator.commit()

        refused = await client.post(
            f"/conversations/{conversation_id}/messages", headers=alice.headers,
            json=_body(device),
        )
        assert refused.status_code == 423, refused.text
        history = await client.get(
            f"/conversations/{conversation_id}/messages", headers=alice.headers,
        )
        assert history.status_code == 200, history.text
        assert [row["id"] for row in history.json()] == [sent.json()["id"]]
        async with get_session_factory()() as session:
            deliveries = (await session.scalars(select(models.DeliveryOutbox))).all()
            assert len(deliveries) == 1
            assert deliveries[0].payload["message_id"] == sent.json()["id"]
    finally:
        release.set()
        tasks = [sender, checkpoint] + ([activation] if activation is not None else [])
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_group_survives_member_cleanup_and_retains_old_messages(
    client, make_user, register_device, monkeypatch,
):
    alice, bob, carol = [await make_user(name) for name in ("Alice", "Bob", "Carol")]
    await share_verified_campus(alice.id, bob.id, carol.id)
    alice_device, bob_device = await register_device(alice), await register_device(bob)
    created = await client.post("/conversations", headers=alice.headers, json={
        "kind": "group", "member_user_ids": [bob.id, carol.id],
    })
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]
    monkeypatch.setattr(outbox, "dispatch_now", _retain_delivery)
    old = await client.post(
        f"/conversations/{conversation_id}/messages", headers=bob.headers,
        json=_body(bob_device),
    )
    assert old.status_code == 201, old.text
    request_id = await _deletion_request(bob.id)
    async with get_session_factory()() as session:
        await activate_deletion_hold(session, user_id=uuid.UUID(bob.id), request_id=request_id)
        await session.commit()
    # Exercise the existing local cleanup boundary only. Provider fulfillment and
    # production execution are deliberately outside this disposable database test.
    async with get_session_factory()() as session:
        await cleanup_local_account(session, uuid.UUID(bob.id))
        await session.commit()
    monkeypatch.setenv("ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED", "true")
    get_settings.cache_clear()
    new = await client.post(
        f"/conversations/{conversation_id}/messages", headers=alice.headers,
        json=_body(alice_device),
    )
    assert new.status_code == 201, new.text
    history = await client.get(
        f"/conversations/{conversation_id}/messages", headers=carol.headers,
    )
    assert history.status_code == 200, history.text
    assert {row["id"] for row in history.json()} == {old.json()["id"], new.json()["id"]}
    async with get_session_factory()() as session:
        deliveries = (await session.scalars(select(models.DeliveryOutbox))).all()
        new_delivery = next(row for row in deliveries if row.payload["message_id"] == new.json()["id"])
        assert {str(user_id) for user_id in new_delivery.recipient_ids} == {alice.id, carol.id}
        member = await session.get(models.ConversationMember, (uuid.UUID(conversation_id), uuid.UUID(bob.id)))
        assert member is not None and member.left_at is not None
