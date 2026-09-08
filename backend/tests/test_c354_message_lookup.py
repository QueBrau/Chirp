"""Authoritative lookup for live hints: bounded input, current visibility, no watermark."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select, text, tuple_, update

from app import models
from app.db import get_session, get_session_factory
from app.middleware.auth import get_current_user
from app.routers import messages
from tests.conftest import share_verified_campus


@pytest.fixture
async def thread(client, make_user, register_device):
    reader = await make_user("Lookup reader")
    sender = await make_user("Lookup sender")
    outsider = await make_user("Lookup outsider")
    await share_verified_campus(reader.id, sender.id, outsider.id)
    device = await register_device(sender, one_time_prekey_count=1)
    response = await client.post("/conversations", headers=reader.headers,
                                 json={"kind": "dm", "member_user_ids": [sender.id]})
    assert response.status_code == 201, response.text
    return SimpleNamespace(reader=reader, sender=sender, outsider=outsider,
                           device_id=uuid.UUID(device["id"]), id=response.json()["id"])


async def insert(thread, count=1, *, conversation_id=None, created_at=None):
    rows = []
    async with get_session_factory()() as session:
        for i in range(count):
            row = models.Message(conversation_id=uuid.UUID(conversation_id or thread.id),
                                 sender_device_id=thread.device_id,
                                 ciphertext=b"opaque-" + str(i).encode(),
                                 message_type="sender_key_distribution")
            if created_at is not None:
                row.created_at = created_at
            session.add(row)
            rows.append(row)
        await session.commit()
    return rows


async def lookup(client, thread, ids, *, headers=None):
    return await client.get(f"/conversations/{thread.id}/messages/by-id",
                            params={"ids": ",".join(str(i) for i in ids)},
                            headers=thread.reader.headers if headers is None else headers)


@pytest.mark.parametrize("query", [
    None, "", "not-a-uuid", " ",
    str(uuid.UUID(int=1)) + ",", "," + str(uuid.UUID(int=1)),
    str(uuid.UUID(int=1)) + ",," + str(uuid.UUID(int=2)),
    "0" * 32, "{" + str(uuid.UUID(int=1)) + "}",
    "x" * 1850, ",".join([str(uuid.UUID(int=1))] * 51),
    ",".join(["a"] * 51),
])
async def test_invalid_ids_stop_before_identity_or_database(query):
    app = FastAPI()
    app.include_router(messages.router)
    entered = []
    async def forbidden_identity():
        entered.append("identity")
        raise AssertionError("invalid lookup reached identity")
    async def forbidden_session():
        entered.append("database")
        raise AssertionError("invalid lookup reached database")
        yield
    app.dependency_overrides[get_current_user] = forbidden_identity
    app.dependency_overrides[get_session] = forbidden_session
    params = {} if query is None else {"ids": query}
    async with AsyncClient(transport=ASGITransport(app), base_url="http://local") as client:
        response = await client.get(f"/conversations/{uuid.UUID(int=1)}/messages/by-id", params=params)
    assert response.status_code == 422, response.text
    assert response.json() == {"detail": "invalid_message_ids"}
    assert entered == []


async def test_repeated_ids_query_parameter_rejected_before_dependencies():
    app = FastAPI()
    app.include_router(messages.router)
    entered = []
    async def forbidden_identity():
        entered.append(True)
        raise AssertionError("repeated parameter reached identity")
    app.dependency_overrides[get_current_user] = forbidden_identity
    async with AsyncClient(transport=ASGITransport(app), base_url="http://local") as client:
        response = await client.get(f"/conversations/{uuid.UUID(int=1)}/messages/by-id",
                                    params=[("ids", str(uuid.UUID(int=2))), ("ids", str(uuid.UUID(int=3)))])
    assert response.status_code == 422, response.text
    assert entered == []


async def test_exact_50_uppercase_duplicates_and_canonical_order(client, thread):
    stamp = datetime(2026, 9, 1, 12, 0, 0, 100, tzinfo=timezone.utc)
    rows = await insert(thread, 50, created_at=stamp)
    # Same-millisecond but distinct microsecond values must remain correctly ordered.
    async with get_session_factory()() as session:
        await session.execute(update(models.Message).where(models.Message.id == rows[0].id)
                              .values(created_at=stamp + timedelta(microseconds=1)))
        await session.commit()
    requested = [str(row.id).upper() for row in rows]
    assert len(",".join(requested)) == 1849
    response = await lookup(client, thread, requested)
    assert response.status_code == 200, response.text
    expected = [str(rows[0].id)] + sorted((str(r.id) for r in rows[1:]), reverse=True)
    assert [row["id"] for row in response.json()] == expected
    assert all(row["message_type"] == "sender_key_distribution" for row in response.json())
    history = await client.get(f"/conversations/{thread.id}/messages", headers=thread.reader.headers,
                               params={"limit": 50})
    assert response.json() == history.json()
    duplicate = await lookup(client, thread, [rows[0].id] * 50)
    assert duplicate.status_code == 200
    assert duplicate.json() == [response.json()[0]]


async def test_hidden_missing_and_other_conversation_ids_are_indistinguishable(client, thread):
    visible = (await insert(thread))[0]
    other = await client.post("/conversations", headers=thread.reader.headers,
                             json={"kind": "group", "title": "Other", "member_user_ids": [thread.sender.id]})
    assert other.status_code == 201, other.text
    foreign = (await insert(thread, conversation_id=other.json()["id"]))[0]
    unknown = uuid.uuid4()
    first = await lookup(client, thread, [visible.id, foreign.id, unknown])
    assert first.status_code == 200
    assert [row["id"] for row in first.json()] == [str(visible.id)]
    blocked = await client.post("/moderation/blocks", headers=thread.reader.headers,
                                json={"blocked_id": thread.sender.id})
    assert blocked.status_code == 201, blocked.text
    for requested in ([visible.id], [foreign.id], [unknown], [visible.id, foreign.id, unknown]):
        hidden = await lookup(client, thread, requested)
        assert hidden.status_code == 200
        assert hidden.json() == []
    unblocked = await client.delete("/moderation/blocks", headers=thread.reader.headers,
                                    params={"blocked_id": thread.sender.id})
    assert unblocked.status_code == 204
    restored = await lookup(client, thread, [visible.id])
    assert restored.json() == first.json()


async def test_reverse_and_anonymous_blocks_do_not_hide_named_history(client, thread):
    row = (await insert(thread))[0]
    # A sender's block is the opposite direction; the reader's anonymous block
    # cannot identify that anonymous author by changing this named surface.
    async with get_session_factory()() as session:
        session.add_all([
            models.UserBlock(blocker_id=uuid.UUID(thread.sender.id), blocked_id=uuid.UUID(thread.reader.id), source="named"),
            models.UserBlock(blocker_id=uuid.UUID(thread.reader.id), blocked_id=uuid.UUID(thread.sender.id), source="by_chirp"),
        ])
        await session.commit()
    response = await lookup(client, thread, [row.id])
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()] == [str(row.id)]
    history = await client.get(f"/conversations/{thread.id}/messages", headers=thread.reader.headers)
    assert history.json() == response.json()


@pytest.mark.parametrize("state", ["missing_auth", "unregistered", "outsider", "left", "suspended"])
async def test_current_authorization_still_applies(client, thread, state):
    row = (await insert(thread))[0]
    headers = thread.reader.headers
    expected = 403
    if state == "missing_auth":
        headers, expected = {}, 401
    elif state == "unregistered":
        headers, expected = {"X-Debug-Firebase-Uid": "unregistered-lookup-fixture"}, 401
    elif state == "outsider":
        headers = thread.outsider.headers
    elif state == "left":
        left = await client.post(f"/conversations/{thread.id}/leave", headers=headers)
        assert left.status_code == 200
    else:
        async with get_session_factory()() as session:
            await session.execute(update(models.User).where(models.User.id == uuid.UUID(thread.reader.id))
                                  .values(suspended_at=datetime.now(timezone.utc)))
            await session.commit()
    response = await lookup(client, thread, [row.id], headers=headers)
    assert response.status_code == expected, response.text
    assert "ciphertext_b64" not in response.text


async def test_late_commit_behind_newest_tuple_is_resolved_by_id(client, thread):
    factory = get_session_factory()
    async with factory() as older:
        # Establish the transaction's now() before the newer writer begins.
        early_timestamp = await older.scalar(text("SELECT now()"))
        async with factory() as newer:
            fast = models.Message(conversation_id=uuid.UUID(thread.id), sender_device_id=thread.device_id,
                                  ciphertext=b"commits-first", message_type="signal")
            newer.add(fast)
            await newer.flush()
            await newer.refresh(fast)
            await newer.commit()
        initial = await lookup(client, thread, [fast.id])
        assert initial.status_code == 200
        slow = models.Message(conversation_id=uuid.UUID(thread.id), sender_device_id=thread.device_id,
                              ciphertext=b"commits-later", message_type="signal")
        older.add(slow)
        await older.flush()
        await older.refresh(slow)
        assert slow.created_at == early_timestamp < fast.created_at
        await older.commit()
    async with factory() as session:
        # Demonstrate the rejected forward-watermark design would miss the row.
        missed = await session.scalars(select(models.Message.id).where(
            models.Message.conversation_id == uuid.UUID(thread.id),
            tuple_(models.Message.created_at, models.Message.id) > (fast.created_at, fast.id)))
        assert list(missed) == []
    response = await lookup(client, thread, [slow.id, fast.id])
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()] == [str(fast.id), str(slow.id)]
