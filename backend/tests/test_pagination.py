"""Message pagination tie-break: compound (created_at, id) cursor (security review finding 10)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from httpx import AsyncClient
from sqlalchemy import text

from tests.conftest import ApiUser, MakeUser, RegisterDevice, b64, share_verified_campus


async def _make_dm(client: AsyncClient, creator: ApiUser, other: ApiUser) -> str:
    # c243: a chapter-less conversation now requires a reachable recipient. These tests
    # are about cursor behaviour, not authorization, so make the pair campus peers.
    await share_verified_campus(creator.id, other.id)
    created = await client.post(
        "/conversations",
        json={"kind": "dm", "member_user_ids": [other.id]},
        headers=creator.headers,
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


async def test_tied_timestamp_page_boundary_is_lossless_with_before_id(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    """5 same-timestamp messages: paging with before+before_id returns all 5, no dupes/gaps.

    Reproduces security review finding 10: a created_at-only cursor silently
    drops rows that share a timestamp at a page boundary.
    """
    creator = await make_user("Convo Creator")
    other = await make_user("Convo Other")
    device = await register_device(creator, one_time_prekey_count=1)
    conversation_id = await _make_dm(client, creator, other)

    tied_at = datetime.now(timezone.utc)
    message_ids: list[str] = []
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        for _ in range(5):
            result = await session.execute(
                text(
                    "INSERT INTO messages"
                    " (conversation_id, sender_device_id, ciphertext, message_type, created_at)"
                    " VALUES (:conversation_id, :sender_device_id, :ciphertext, 'signal', :created_at)"
                    " RETURNING id"
                ),
                {
                    "conversation_id": uuid.UUID(conversation_id),
                    "sender_device_id": uuid.UUID(device["id"]),
                    "ciphertext": b"opaque",
                    "created_at": tied_at,
                },
            )
            message_ids.append(str(result.scalar_one()))
        await session.commit()

    seen: list[str] = []
    before: str | None = None
    before_id: str | None = None
    for _ in range(10):  # generous cap so a regression can't spin the loop forever
        params: dict[str, str | int] = {"limit": 2}
        if before is not None:
            params["before"] = before
            params["before_id"] = before_id
        response = await client.get(
            f"/conversations/{conversation_id}/messages",
            params=params,
            headers=creator.headers,
        )
        assert response.status_code == 200, response.text
        page = response.json()
        if not page:
            break
        seen.extend(m["id"] for m in page)
        before = page[-1]["created_at"]
        before_id = page[-1]["id"]

    assert sorted(seen) == sorted(message_ids), (
        "compound (created_at, id) cursor must not drop or duplicate tied-timestamp rows"
    )


async def test_before_alone_still_works_backward_compatible(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    """`before` without `before_id` (legacy clients) still paginates without erroring."""
    creator = await make_user("Convo Creator")
    other = await make_user("Convo Other")
    device = await register_device(creator, one_time_prekey_count=1)
    conversation_id = await _make_dm(client, creator, other)

    for i in range(3):
        sent = await client.post(
            f"/conversations/{conversation_id}/messages",
            json={
                "sender_device_id": device["id"],
                "ciphertext_b64": b64(f"msg-{i}".encode()),
            },
            headers=creator.headers,
        )
        assert sent.status_code == 201, sent.text

    first_page = await client.get(
        f"/conversations/{conversation_id}/messages",
        params={"limit": 2},
        headers=creator.headers,
    )
    assert first_page.status_code == 200, first_page.text
    items = first_page.json()
    assert len(items) == 2

    second_page = await client.get(
        f"/conversations/{conversation_id}/messages",
        params={"before": items[-1]["created_at"]},
        headers=creator.headers,
    )
    assert second_page.status_code == 200, second_page.text
    assert len(second_page.json()) == 1


async def test_compound_cursor_matches_default_newest_first_order(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    """Sanity: with distinct timestamps, before+before_id paging still returns newest-first."""
    creator = await make_user("Convo Creator")
    other = await make_user("Convo Other")
    device = await register_device(creator, one_time_prekey_count=1)
    conversation_id = await _make_dm(client, creator, other)

    sent_ids: list[str] = []
    for i in range(4):
        sent = await client.post(
            f"/conversations/{conversation_id}/messages",
            json={
                "sender_device_id": device["id"],
                "ciphertext_b64": b64(f"msg-{i}".encode()),
            },
            headers=creator.headers,
        )
        assert sent.status_code == 201, sent.text
        sent_ids.append(sent.json()["id"])

    collected: list[str] = []
    before: str | None = None
    before_id: str | None = None
    for _ in range(10):
        params: dict[str, str | int] = {"limit": 2}
        if before is not None:
            params["before"] = before
            params["before_id"] = before_id
        response = await client.get(
            f"/conversations/{conversation_id}/messages",
            params=params,
            headers=creator.headers,
        )
        assert response.status_code == 200, response.text
        page = response.json()
        if not page:
            break
        collected.extend(m["id"] for m in page)
        before = page[-1]["created_at"]
        before_id = page[-1]["id"]

    assert collected == list(reversed(sent_ids))
