"""Caller-owned message and Chirp mutations obey the account deletion hold."""
from __future__ import annotations

import base64
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import event, func, select

from app import models
from app.db import get_session_factory
from app.services.account_deletion_hold import activate_deletion_hold
from tests.conftest import ApiUser, share_verified_campus


async def _activate_hold(user_id: str) -> None:
    async with get_session_factory()() as session:
        request = models.AccountDataRequest(
            user_id=uuid.UUID(user_id), kind="deletion", open_key="open"
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


@pytest.fixture
async def message_world(client, make_user, register_device):
    alice = await make_user("Alice")
    bob = await make_user("Bob")
    await share_verified_campus(alice.id, bob.id)
    alice_device = await register_device(alice)
    bob_device = await register_device(bob, registration_id=4343)
    conversation = await client.post(
        "/conversations",
        headers=alice.headers,
        json={"kind": "dm", "member_user_ids": [bob.id]},
    )
    assert conversation.status_code == 201, conversation.text
    conversation_id = conversation.json()["id"]
    sent = await client.post(
        f"/conversations/{conversation_id}/messages",
        headers=bob.headers,
        json={
            "sender_device_id": bob_device["id"],
            "ciphertext_b64": base64.b64encode(b"opaque").decode(),
            "message_type": "signal",
        },
    )
    assert sent.status_code == 201, sent.text
    return SimpleNamespace(
        alice=alice,
        bob=bob,
        alice_device=alice_device,
        conversation_id=conversation_id,
        message_id=sent.json()["id"],
    )


async def _make_chirp(client, make_campus, user: ApiUser) -> tuple[str, str]:
    campus_id = await make_campus()
    from tests.conftest import set_campus

    await set_campus(user.id, campus_id)
    created = await client.post(
        f"/campuses/{campus_id}/chirps",
        headers=user.headers,
        json={"body": "retract me"},
    )
    assert created.status_code == 201, created.text
    return campus_id, created.json()["id"]


@pytest.mark.asyncio
async def test_hold_blocks_leave_receipt_and_chirp_delete_before_mutation(
    client, make_user, make_campus, message_world, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    world = message_world
    campus_id, chirp_id = await _make_chirp(client, make_campus, world.alice)
    await _activate_hold(world.alice.id)

    leave = await client.post(
        f"/conversations/{world.conversation_id}/leave", headers=world.alice.headers
    )
    receipt = await client.post(
        f"/messages/{world.message_id}/receipts",
        headers=world.alice.headers,
        json={"device_id": world.alice_device["id"]},
    )
    delete = await client.delete(f"/chirps/{chirp_id}", headers=world.alice.headers)
    assert leave.status_code == receipt.status_code == delete.status_code == 423
    messages = await client.get(
        f"/conversations/{world.conversation_id}/messages", headers=world.alice.headers
    )
    chirps = await client.get(f"/campuses/{campus_id}/chirps", headers=world.alice.headers)
    assert messages.status_code == 200, messages.text
    assert chirps.status_code == 200, chirps.text

    async with get_session_factory()() as session:
        member = await session.get(
            models.ConversationMember,
            (uuid.UUID(world.conversation_id), uuid.UUID(world.alice.id)),
        )
        stored_receipt = await session.get(
            models.MessageReceipt,
            (uuid.UUID(world.message_id), uuid.UUID(world.alice_device["id"])),
        )
        chirp = await session.get(models.Chirp, uuid.UUID(chirp_id))
        assert member is not None and member.left_at is None
        assert stored_receipt is None
        assert chirp is not None and chirp.removed_at is None


@pytest.mark.asyncio
async def test_hold_blocks_chirp_create_before_target_insert(
    client, make_user, make_campus, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED", "true")
    from app.config import get_settings
    from tests.conftest import set_campus

    get_settings.cache_clear()
    user = await make_user("Held chirp creator")
    campus_id = await make_campus()
    await set_campus(user.id, campus_id)
    await _activate_hold(user.id)
    statements: list[str] = []

    def record(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    from app.db import get_engine

    engine = get_engine()
    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        response = await client.post(
            f"/campuses/{campus_id}/chirps",
            headers=user.headers,
            json={"body": "must not persist"},
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
    assert response.status_code == 423, response.text
    assert not any("insert into chirps" in statement for statement in statements)
    async with get_session_factory()() as session:
        count = await session.scalar(
            select(func.count()).select_from(models.Chirp).where(
                models.Chirp.body == "must not persist"
            )
        )
    assert count == 0


@pytest.mark.asyncio
async def test_enabled_guard_preserves_other_account_and_success_paths(
    client, make_user, make_campus, register_device, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    alice = await make_user("Alice")
    bob = await make_user("Bob")
    await share_verified_campus(alice.id, bob.id)
    device = await register_device(alice)
    conversation = await client.post(
        "/conversations",
        headers=alice.headers,
        json={"kind": "dm", "member_user_ids": [bob.id]},
    )
    assert conversation.status_code == 201, conversation.text
    conversation_id = conversation.json()["id"]
    await _activate_hold(alice.id)

    other_leave = await client.post(
        f"/conversations/{conversation_id}/leave", headers=bob.headers
    )
    assert other_leave.status_code == 200, other_leave.text

    # The held account's successful-path calls are proven before activation by the
    # companion test; this assertion keeps the normal response contract explicit.
    assert device["id"]


@pytest.mark.asyncio
async def test_enabled_guard_allows_normal_leave_receipt_and_author_delete(
    client, make_user, make_campus, register_device, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    alice = await make_user("Alice")
    bob = await make_user("Bob")
    await share_verified_campus(alice.id, bob.id)
    alice_device = await register_device(alice)
    bob_device = await register_device(bob, registration_id=4444)
    conversation = await client.post(
        "/conversations",
        headers=alice.headers,
        json={"kind": "dm", "member_user_ids": [bob.id]},
    )
    assert conversation.status_code == 201, conversation.text
    conversation_id = conversation.json()["id"]
    sent = await client.post(
        f"/conversations/{conversation_id}/messages",
        headers=bob.headers,
        json={
            "sender_device_id": bob_device["id"],
            "ciphertext_b64": base64.b64encode(b"normal").decode(),
            "message_type": "signal",
        },
    )
    assert sent.status_code == 201, sent.text
    receipt = await client.post(
        f"/messages/{sent.json()['id']}/receipts",
        headers=alice.headers,
        json={"device_id": alice_device["id"]},
    )
    assert receipt.status_code == 200, receipt.text
    leave = await client.post(
        f"/conversations/{conversation_id}/leave", headers=alice.headers
    )
    assert leave.status_code == 200, leave.text
    _, chirp_id = await _make_chirp(client, make_campus, bob)
    delete = await client.delete(f"/chirps/{chirp_id}", headers=bob.headers)
    assert delete.status_code == 204, delete.text


@pytest.mark.asyncio
async def test_enabled_guard_preserves_foreign_author_nonmember_and_device_checks(
    client, make_user, make_campus, register_device, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    alice = await make_user("Alice")
    bob = await make_user("Bob")
    outsider = await make_user("Outsider")
    await share_verified_campus(alice.id, bob.id)
    alice_device = await register_device(alice)
    bob_device = await register_device(bob, registration_id=4545)
    conversation = await client.post(
        "/conversations",
        headers=alice.headers,
        json={"kind": "dm", "member_user_ids": [bob.id]},
    )
    assert conversation.status_code == 201, conversation.text
    conversation_id = conversation.json()["id"]
    sent = await client.post(
        f"/conversations/{conversation_id}/messages",
        headers=bob.headers,
        json={
            "sender_device_id": bob_device["id"],
            "ciphertext_b64": base64.b64encode(b"ownership").decode(),
            "message_type": "signal",
        },
    )
    assert sent.status_code == 201, sent.text
    receipt = await client.post(
        f"/messages/{sent.json()['id']}/receipts",
        headers=alice.headers,
        json={"device_id": bob_device["id"]},
    )
    assert receipt.status_code == 403
    assert receipt.json() == {"detail": "not_your_device"}

    outsider_leave = await client.post(
        f"/conversations/{conversation_id}/leave", headers=outsider.headers
    )
    assert outsider_leave.status_code == 403
    assert outsider_leave.json() == {"detail": "not_a_member"}

    _, chirp_id = await _make_chirp(client, make_campus, bob)
    foreign_delete = await client.delete(f"/chirps/{chirp_id}", headers=alice.headers)
    assert foreign_delete.status_code == 403
    assert foreign_delete.json() == {"detail": "not_author"}
    assert alice_device["id"]


@pytest.mark.asyncio
async def test_flag_off_keeps_content_writes_free_of_hold_sql(
    client, make_user, make_campus, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED", "false")
    from app.config import get_settings
    from app.db import get_engine

    get_settings.cache_clear()
    user = await make_user("Legacy content writer")
    campus_id = await make_campus()
    from tests.conftest import set_campus

    await set_campus(user.id, campus_id)
    await _activate_hold(user.id)
    statements: list[str] = []

    def record_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    engine = get_engine()
    event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
    try:
        created = await client.post(
            f"/campuses/{campus_id}/chirps",
            headers=user.headers,
            json={"body": "legacy create"},
        )
        assert created.status_code == 201, created.text
        chirp_id = created.json()["id"]
        response = await client.delete(f"/chirps/{chirp_id}", headers=user.headers)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_sql)
    assert response.status_code == 204, response.text
    assert not any("account_deletion_hold" in statement for statement in statements)
    assert not any("account_deletion_holds" in statement for statement in statements)
    assert campus_id


@pytest.mark.asyncio
async def test_write_guard_route_inventory_covers_only_assigned_mutations() -> None:
    import inspect

    from app.routers import chirps, messages

    for endpoint in (
        messages.leave_conversation,
        messages.upsert_receipt,
        chirps.delete_chirp,
    ):
        dependency = inspect.signature(endpoint).parameters["user"].default
        assert dependency.dependency.__name__ == "get_current_user_for_write"

    from app.routers import chirps

    create_dependency = inspect.signature(chirps.create_chirp).parameters["user"].default
    assert create_dependency.dependency.__name__ == "require_campus_member_for_write"
