"""Account-deletion admission for the v2 message writer."""
from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import func, select

from app import models
from app.config import get_settings
from app.db import get_session_factory
from app.services.account_deletion_hold import activate_deletion_hold
from app.routers import messages_v2
from tests.conftest import MakeUser
from tests.e2ee_v2_helpers import leg, register_approved_pair, send
from tests.test_c444_e2ee_v2_messages import World, _world


def _set_admission(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setenv("ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED", str(enabled).lower())
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clear_settings() -> None:
    yield
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


async def _message_counts() -> dict[str, int]:
    async with get_session_factory()() as session:
        return {
            "messages": int(await session.scalar(select(func.count()).select_from(models.Message)) or 0),
            "legs": int(await session.scalar(select(func.count()).select_from(models.MessageLeg)) or 0),
            "outbox": int(await session.scalar(select(func.count()).select_from(models.DeliveryOutbox)) or 0),
        }


@pytest.mark.asyncio
async def test_actor_and_recipient_holds_block_before_message_graph_writes(
    client: Any, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission(monkeypatch, True)
    world: World = await _world(client, make_user)
    before = await _message_counts()

    await _hold(world.bob.id)
    blocked_recipient = await send(
        client, world.convo, world.a1, world.required_from_a1(), expect=423
    )
    assert blocked_recipient.json()["detail"] == "account_deletion_in_progress"
    assert await _message_counts() == before

    await _hold(world.alice.id)
    blocked_actor = await send(
        client, world.convo, world.a1, world.required_from_a1(), expect=423
    )
    assert blocked_actor.json()["detail"] == "account_deletion_in_progress"
    assert await _message_counts() == before


@pytest.mark.asyncio
async def test_unrelated_hold_allows_v2_and_flag_off_preserves_send_behavior(
    client: Any, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission(monkeypatch, True)
    world: World = await _world(client, make_user)
    unrelated = await make_user("unrelated message hold")
    await _hold(unrelated.id)
    assert (await send(client, world.convo, world.a1, world.required_from_a1())).status_code == 201

    # A fresh world gives a valid exact device set for the default-off assertion.
    _set_admission(monkeypatch, False)
    world2: World = await _world(client, make_user)
    await _hold(world2.bob.id)
    assert (await send(client, world2.convo, world2.a1, world2.required_from_a1())).status_code == 201


@pytest.mark.asyncio
async def test_blocked_recipients_have_same_refusal_with_or_without_target_hold(
    client: Any, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission(monkeypatch, True)
    world: World = await _world(client, make_user)
    blocked = await client.post(
        "/moderation/blocks", json={"blocked_id": world.alice.id}, headers=world.bob.headers
    )
    assert blocked.status_code == 201, blocked.text
    unheld = await send(client, world.convo, world.a1, world.required_from_a1())
    assert unheld.status_code == 403
    assert unheld.json() == {"detail": "recipient_not_reachable"}

    await _hold(world.bob.id)
    held = await send(client, world.convo, world.a1, world.required_from_a1())
    assert held.status_code == 403
    assert held.json() == {"detail": "recipient_not_reachable"}


@pytest.mark.asyncio
async def test_exact_replay_survives_recipient_block_and_device_revocation(
    client: Any, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission(monkeypatch, True)
    world: World = await _world(client, make_user)
    client_message_id = str(uuid.uuid4())
    legs = world.required_from_a1()
    first = await send(
        client, world.convo, world.a1, legs,
        client_message_id=client_message_id, expect=201,
    )
    counts = await _message_counts()
    blocked = await client.post(
        "/moderation/blocks", json={"blocked_id": world.alice.id}, headers=world.bob.headers
    )
    assert blocked.status_code == 201, blocked.text
    revoked = await client.delete(f"/v2/devices/{world.b1.id}", headers=world.bob.headers)
    assert revoked.status_code == 200, revoked.text
    retry = await send(
        client, world.convo, world.a1, legs,
        client_message_id=client_message_id, expect=200,
    )
    assert retry.json()["id"] == first.json()["id"]
    assert await _message_counts() == counts

    await _hold(world.alice.id)
    actor_held = await send(
        client, world.convo, world.a1, legs,
        client_message_id=client_message_id,
    )
    assert actor_held.status_code == 423


@pytest.mark.asyncio
async def test_unknown_conversation_does_not_reveal_existence_or_hold_state(
    client: Any, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission(monkeypatch, True)
    user = await make_user("unknown conversation sender")
    sender, _ = await register_approved_pair(client, user)
    response = await send(
        client, str(uuid.uuid4()), sender, [leg(sender)], user=user
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "not_a_member"}


@pytest.mark.asyncio
async def test_exact_retry_precedes_new_target_hold(
    client: Any, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission(monkeypatch, True)
    world: World = await _world(client, make_user)
    client_message_id = str(uuid.uuid4())
    legs = world.required_from_a1()
    first = await send(client, world.convo, world.a1, legs, client_message_id=client_message_id, expect=201)
    counts = await _message_counts()
    await _hold(world.bob.id)
    retry = await send(
        client, world.convo, world.a1, legs, client_message_id=client_message_id, expect=200
    )
    assert retry.json()["id"] == first.json()["id"]
    assert await _message_counts() == counts


@pytest.mark.asyncio
async def test_replay_disappearing_after_preflight_cannot_fall_through_to_insert(
    client: Any, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission(monkeypatch, True)
    world: World = await _world(client, make_user)
    client_message_id = str(uuid.uuid4())
    legs = world.required_from_a1()
    first = await send(
        client, world.convo, world.a1, legs,
        client_message_id=client_message_id, expect=201,
    )
    before = await _message_counts()
    original = messages_v2._existing_message
    removed = False

    async def disappear(session: Any, sender_device_id: uuid.UUID, message_id: uuid.UUID):
        nonlocal removed
        found = await original(session, sender_device_id, message_id)
        if found is not None and not removed:
            async with get_session_factory()() as deleter:
                await deleter.execute(
                    models.MessageLeg.__table__.delete().where(
                        models.MessageLeg.message_id == found.id
                    )
                )
                await deleter.execute(
                    models.Message.__table__.delete().where(models.Message.id == found.id)
                )
                await deleter.commit()
            removed = True
        return None

    monkeypatch.setattr(messages_v2, "_existing_message", disappear)
    retry = await send(
        client, world.convo, world.a1, legs,
        client_message_id=client_message_id,
    )
    assert retry.status_code == 503
    assert removed
    after = await _message_counts()
    assert after["messages"] == before["messages"] - 1
    assert after["legs"] == before["legs"] - 2
    assert after["outbox"] == before["outbox"]


@pytest.mark.asyncio
async def test_nonmember_does_not_learn_held_participant_state(
    client: Any, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission(monkeypatch, True)
    world: World = await _world(client, make_user)
    stranger = await make_user("message stranger")
    stranger_device, _ = await register_approved_pair(client, stranger)
    await _hold(world.bob.id)
    response = await send(
        client,
        world.convo,
        stranger_device,
        [leg(world.a1)],
        user=stranger,
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "not_a_member"}


@pytest.mark.asyncio
async def test_roster_change_after_preflight_is_rejected_before_message_insert(
    client: Any, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The request guard catches a member set that changes after short preflight."""
    _set_admission(monkeypatch, True)
    world: World = await _world(client, make_user)
    late = await make_user("late message member")
    original = __import__("app.services.messaging_write_admission", fromlist=["_active_member_ids"])
    calls = 0

    async def roster(session: Any, conversation_id: uuid.UUID) -> list[uuid.UUID]:
        nonlocal calls
        members = await original._active_member_ids_original(session, conversation_id)
        calls += 1
        if calls == 1:
            async with get_session_factory()() as writer:
                writer.add(models.ConversationMember(conversation_id=uuid.UUID(world.convo), user_id=uuid.UUID(late.id)))
                await writer.commit()
        return members

    monkeypatch.setattr(original, "_active_member_ids_original", original._active_member_ids, raising=False)
    monkeypatch.setattr(original, "_active_member_ids", roster)
    response = await send(client, world.convo, world.a1, world.required_from_a1())
    assert response.status_code == 503
    assert await _message_counts() == {"messages": 0, "legs": 0, "outbox": 0}
