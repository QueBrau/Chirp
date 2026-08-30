"""Group leave contract (SPEC §6.4): left_at is set and fan-out excludes the leaver."""
from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from tests.conftest import MakeUser, RegisterDevice, b64, share_verified_campus


async def test_leave_sets_left_at_and_fanout_skips_left_member(
    client: AsyncClient,
    make_user: MakeUser,
    register_device: RegisterDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Member leaves a group of 3; a new message fans out only to the remaining members."""
    creator = await make_user("Group Creator")
    stayer = await make_user("Group Stayer")
    leaver = await make_user("Group Leaver")
    # c243: a chapter-less group now requires every named member to be reachable. The
    # subject here is left_at and fan-out, so make the three of them campus peers.
    await share_verified_campus(creator.id, stayer.id, leaver.id)
    device = await register_device(creator, one_time_prekey_count=1)

    created = await client.post(
        "/conversations",
        json={
            "kind": "group",
            "title": "Retreat Planning",
            "member_user_ids": [stayer.id, leaver.id],
        },
        headers=creator.headers,
    )
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]
    assert len(created.json()["members"]) == 3

    # --- leave ---
    left = await client.post(
        f"/conversations/{conversation_id}/leave", headers=leaver.headers
    )
    assert left.status_code == 200, left.text
    assert left.json()["left_at"] is not None

    from app.db import get_session_factory

    async with get_session_factory()() as session:
        left_at = (
            await session.execute(
                text(
                    "SELECT left_at FROM conversation_members"
                    " WHERE conversation_id = :conversation_id AND user_id = :user_id"
                ),
                {
                    "conversation_id": uuid.UUID(conversation_id),
                    "user_id": uuid.UUID(leaver.id),
                },
            )
        ).scalar_one()
    assert left_at is not None, "conversation_members.left_at must be set on leave"

    # --- fan-out after the leave ---
    published: list[tuple[str, dict[str, Any]]] = []

    async def _capture_publish(user_id: str, event: dict[str, Any]) -> None:
        published.append((user_id, event))

    import app.routers.messages as messages_router
    import app.ws.pubsub as pubsub

    monkeypatch.setattr(pubsub, "publish_to_user", _capture_publish)
    # messages.py binds the name at import time (from-import), so patch its module too.
    monkeypatch.setattr(messages_router, "publish_to_user", _capture_publish)

    sent = await client.post(
        f"/conversations/{conversation_id}/messages",
        json={
            "sender_device_id": device["id"],
            "ciphertext_b64": b64(b"opaque-sender-key-ciphertext"),
            "message_type": "signal",
        },
        headers=creator.headers,
    )
    assert sent.status_code == 201, sent.text

    fanned_out_to = {user_id for user_id, _event in published}
    assert leaver.id not in fanned_out_to, "left member must not receive fan-out"
    assert stayer.id in fanned_out_to, "remaining member must receive fan-out"
    assert fanned_out_to <= {creator.id, stayer.id}

    for _user_id, event in published:
        assert event["type"] == "message"
        assert event["conversation_id"] == conversation_id

    # --- the leaver is fully out: sending back into the group is 403 ---
    rejected = await client.post(
        f"/conversations/{conversation_id}/messages",
        json={
            "sender_device_id": device["id"],
            "ciphertext_b64": b64(b"should-never-land"),
        },
        headers=leaver.headers,
    )
    assert rejected.status_code == 403, rejected.text
    assert rejected.json() == {"detail": "not_a_member"}
