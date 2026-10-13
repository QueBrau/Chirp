"""Deletion-hold admission for the legacy ciphertext message route."""
from __future__ import annotations

import base64
import uuid

import pytest
from sqlalchemy import func, select

from app import models
from app.config import get_settings
from app.db import get_session_factory
from app.services.account_deletion_hold import activate_deletion_hold
from tests.conftest import share_verified_campus


def _set_admission(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setenv("ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED", str(enabled).lower())
    get_settings.cache_clear()


async def _hold(user_id: str) -> None:
    async with get_session_factory()() as session:
        request = models.AccountDataRequest(
            user_id=uuid.UUID(user_id), kind="deletion", open_key="message-test"
        )
        session.add(request)
        await session.flush()
        await session.commit()
        request_id = request.id
    async with get_session_factory()() as session:
        await activate_deletion_hold(
            session, user_id=uuid.UUID(user_id), request_id=request_id
        )
        await session.commit()


@pytest.fixture(autouse=True)
def _clear_settings():
    yield
    get_settings.cache_clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("held_role", ["sender", "recipient"])
async def test_held_participant_blocks_legacy_message_before_rows(
    client,
    make_user,
    register_device,
    monkeypatch: pytest.MonkeyPatch,
    held_role: str,
) -> None:
    _set_admission(monkeypatch, True)
    sender = await make_user("legacy sender")
    recipient = await make_user("legacy recipient")
    await share_verified_campus(sender.id, recipient.id)
    sender_device = await register_device(sender)
    created = await client.post(
        "/conversations",
        headers=sender.headers,
        json={"kind": "dm", "member_user_ids": [recipient.id]},
    )
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]
    body = {
        "sender_device_id": sender_device["id"],
        "ciphertext_b64": base64.b64encode(b"legacy").decode(),
        "message_type": "signal",
    }
    await _hold(sender.id if held_role == "sender" else recipient.id)
    blocked_sender = await client.post(
        f"/conversations/{conversation_id}/messages", headers=sender.headers, json=body
    )
    assert blocked_sender.status_code == 423, blocked_sender.text
    assert await _message_count() == 0
    assert await _outbox_count() == 0


@pytest.mark.asyncio
async def test_flag_off_preserves_legacy_message_with_held_recipient(
    client, make_user, register_device, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission(monkeypatch, False)
    sender = await make_user("legacy flag off sender")
    recipient = await make_user("legacy flag off recipient")
    await share_verified_campus(sender.id, recipient.id)
    sender_device = await register_device(sender)
    created = await client.post(
        "/conversations", headers=sender.headers,
        json={"kind": "dm", "member_user_ids": [recipient.id]},
    )
    assert created.status_code == 201, created.text
    await _hold(recipient.id)
    body = {
        "sender_device_id": sender_device["id"],
        "ciphertext_b64": base64.b64encode(b"legacy-off").decode(),
        "message_type": "signal",
    }
    response = await client.post(
        f"/conversations/{created.json()['id']}/messages",
        headers=sender.headers,
        json=body,
    )
    assert response.status_code == 201, response.text
    assert await _message_count() == 1


async def _message_count() -> int:
    async with get_session_factory()() as session:
        return int(await session.scalar(select(func.count()).select_from(models.Message)))


async def _outbox_count() -> int:
    async with get_session_factory()() as session:
        return int(await session.scalar(select(func.count()).select_from(models.DeliveryOutbox)))
