"""Bounded, cursor-paginated GET /conversations with per-conversation summary fields
and the new GET /conversations/{id} (board c344).

Before this card, GET /conversations returned every active conversation the caller
belonged to, unbounded, with no summary data at all — the mobile client compensated
by calling listMessages() once per conversation on every load, an N+1 pattern that
grows with total conversation count rather than page size. These tests pin the
replacement: a (created_at, id) cursor identical in shape to list_messages', and
last_message_at/has_messages computed with ONE grouped query per page regardless of
how many conversations are on it.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from httpx import AsyncClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import ApiUser, MakeUser, RegisterDevice, b64, share_verified_campus


async def _make_dm(client: AsyncClient, creator: ApiUser, other: ApiUser) -> str:
    await share_verified_campus(creator.id, other.id)
    created = await client.post(
        "/conversations",
        json={"kind": "dm", "member_user_ids": [other.id]},
        headers=creator.headers,
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


async def _send(client: AsyncClient, conversation_id: str, sender: ApiUser, device: dict) -> None:
    response = await client.post(
        f"/conversations/{conversation_id}/messages",
        json={
            "sender_device_id": device["id"],
            "ciphertext_b64": b64(b"real ciphertext bytes"),
            "message_type": "signal",
        },
        headers=sender.headers,
    )
    assert response.status_code == 201, response.text


async def _set_conversation_created_at(conversation_id: str, when: datetime) -> None:
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        await session.execute(
            text("UPDATE conversations SET created_at = :created_at WHERE id = :id"),
            {"created_at": when, "id": uuid.UUID(conversation_id)},
        )
        await session.commit()


async def test_cursor_pagination_survives_a_constructed_tie(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """5 conversations, ALL forced to an IDENTICAL created_at, paged with limit=2
    across 3 requests. Every page boundary necessarily falls inside the tie —
    tying only some of the five (e.g. the two newest) risks the tied rows
    clustering at one end and never actually straddling a page cut, which would
    make the test pass for the wrong reason. Same shape as
    test_pagination.py's message tie test, which ties ALL five rows for the
    identical reason.

    Falsification: a before-only cursor (no before_id tie-break) drops the
    second-and-later tied rows at every page boundary — `created_at < before`
    excludes every row that shares `before`'s exact timestamp, not just the
    one already returned.
    """
    creator = await make_user("Inbox Owner")
    others = [await make_user(f"Peer {i}") for i in range(5)]
    conversation_ids = [await _make_dm(client, creator, other) for other in others]

    tied_at = datetime.now(timezone.utc)
    for conversation_id in conversation_ids:
        await _set_conversation_created_at(conversation_id, tied_at)

    seen: list[str] = []
    before: str | None = None
    before_id: str | None = None
    for _ in range(10):
        params: dict[str, Any] = {"limit": 2}
        if before is not None:
            params["before"] = before
            params["before_id"] = before_id
        response = await client.get(
            "/conversations", params=params, headers=creator.headers
        )
        assert response.status_code == 200, response.text
        page = response.json()
        if not page:
            break
        seen.extend(row["id"] for row in page)
        before = page[-1]["created_at"]
        before_id = page[-1]["id"]

    assert sorted(seen) == sorted(conversation_ids), (
        "compound (created_at, id) cursor must not drop or duplicate a tied-"
        f"timestamp conversation — got {sorted(seen)}, expected {sorted(conversation_ids)}"
    )


async def test_summary_query_count_is_bounded_not_linear(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    """SQL statement count for GET /conversations must stay FLAT as conversation
    count grows — not one presence query per conversation.

    Falsification: an N+1 implementation (one presence query per conversation, or
    a per-row listMessages-equivalent) passes a tiny smoke check but this test
    catches it: the statement count for 8 conversations must not exceed the count
    for 2.
    """
    creator = await make_user("Bounded Owner")
    device = await register_device(creator, one_time_prekey_count=1)

    async def _load_and_count(n: int) -> int:
        others = [await make_user(f"Bounded Peer {n}-{i}") for i in range(n)]
        for other in others:
            conversation_id = await _make_dm(client, creator, other)
            await _send(client, conversation_id, creator, device)

        from app.db import get_engine

        engine = get_engine().sync_engine
        observed: list[str] = []

        def record(_conn, _cursor, statement, _parameters, _context, _many):
            observed.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            response = await client.get(
                "/conversations", params={"limit": 50}, headers=creator.headers
            )
        finally:
            event.remove(engine, "before_cursor_execute", record)
        assert response.status_code == 200, response.text
        assert len(response.json()) >= n
        return len(observed)

    small_count = await _load_and_count(2)
    large_count = await _load_and_count(8)

    assert large_count == small_count, (
        f"GET /conversations issued {large_count} statements for 8 conversations "
        f"vs {small_count} for 2 — the presence query must be grouped (one call "
        "per page), not one per conversation"
    )


async def test_summary_never_exposes_ciphertext(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    """A conversation with a REAL sent message: the summary carries has_messages
    and last_message_at, and the raw response body contains no ciphertext field or
    value anywhere.

    Falsification: catches a shortcut that computes the summary by internally
    calling list_messages and re-embedding message bodies — acceptance criterion
    1's no-decryption requirement, checked at the wire shape rather than trusted.
    """
    creator = await make_user("Ciphertext Owner")
    other = await make_user("Ciphertext Peer")
    device = await register_device(creator, one_time_prekey_count=1)
    conversation_id = await _make_dm(client, creator, other)
    await _send(client, conversation_id, creator, device)

    response = await client.get("/conversations", headers=creator.headers)
    assert response.status_code == 200, response.text
    row = next(r for r in response.json() if r["id"] == conversation_id)
    assert row["has_messages"] is True
    assert row["last_message_at"] is not None

    raw_body = response.text
    assert "ciphertext" not in raw_body.lower()


async def test_empty_conversation_summary_shape(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    """A conversation with ZERO messages, alongside a populated sibling in the
    same page: the empty one gets has_messages=False/last_message_at=None, the
    sibling is unaffected.

    Falsification: a wrong join (INNER instead of the dict.get-based effective
    LEFT join here) either drops the empty conversation from the response
    entirely or raises, rather than returning it with an empty summary.
    """
    creator = await make_user("Mixed Owner")
    empty_peer = await make_user("Silent Peer")
    populated_peer = await make_user("Chatty Peer")
    device = await register_device(creator, one_time_prekey_count=1)

    empty_id = await _make_dm(client, creator, empty_peer)
    populated_id = await _make_dm(client, creator, populated_peer)
    await _send(client, populated_id, creator, device)

    response = await client.get("/conversations", headers=creator.headers)
    assert response.status_code == 200, response.text
    by_id = {row["id"]: row for row in response.json()}

    assert by_id[empty_id]["has_messages"] is False
    assert by_id[empty_id]["last_message_at"] is None
    assert by_id[populated_id]["has_messages"] is True
    assert by_id[populated_id]["last_message_at"] is not None


async def test_one_conversations_data_does_not_erase_siblings(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    """Three conversations in one page — 0 messages, 5 messages, 1 edge-length
    ciphertext row — all return independently correct summaries together.

    Falsification: an N-independent-fetches design (e.g. asyncio.gather without
    return_exceptions over one summary call per conversation) that raises for ONE
    conversation would wipe the whole page; the single grouped-query design
    cannot fail partially, and this proves it behaviorally rather than by reading
    the implementation.
    """
    creator = await make_user("Independent Owner")
    silent = await make_user("Silent")
    chatty = await make_user("Chatty")
    edge = await make_user("Edge")
    device = await register_device(creator, one_time_prekey_count=1)

    silent_id = await _make_dm(client, creator, silent)
    chatty_id = await _make_dm(client, creator, chatty)
    edge_id = await _make_dm(client, creator, edge)

    for _ in range(5):
        await _send(client, chatty_id, creator, device)

    edge_response = await client.post(
        f"/conversations/{edge_id}/messages",
        json={
            "sender_device_id": device["id"],
            "ciphertext_b64": b64(b"x"),
            "message_type": "signal",
        },
        headers=creator.headers,
    )
    assert edge_response.status_code == 201, edge_response.text

    response = await client.get("/conversations", headers=creator.headers)
    assert response.status_code == 200, response.text
    by_id = {row["id"]: row for row in response.json()}

    assert by_id[silent_id]["has_messages"] is False
    assert by_id[silent_id]["last_message_at"] is None
    assert by_id[chatty_id]["has_messages"] is True
    assert by_id[edge_id]["has_messages"] is True


async def test_get_single_conversation_beyond_first_page(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """The oldest of several conversations, provably absent from page 1 of GET
    /conversations, still resolves through GET /conversations/{id} for a member —
    and 403s for a non-member.

    Falsification: this is the direct regression proof for the mobile [id].tsx
    screen — without this route, a conversation reached after pagination lands
    (a deep link, or scrolling past page 1) has no way to resolve its title/kind
    once listConversations().find(...) can no longer see every conversation.
    """
    creator = await make_user("Deep Link Owner")
    outsider = await make_user("Outsider")
    peers = [await make_user(f"Page Peer {i}") for i in range(4)]
    conversation_ids = [await _make_dm(client, creator, peer) for peer in peers]
    oldest_id = conversation_ids[0]

    first_page = await client.get(
        "/conversations", params={"limit": 2}, headers=creator.headers
    )
    assert first_page.status_code == 200, first_page.text
    assert oldest_id not in {row["id"] for row in first_page.json()}, (
        "test setup invariant: the oldest conversation must NOT be on page 1"
    )

    direct = await client.get(f"/conversations/{oldest_id}", headers=creator.headers)
    assert direct.status_code == 200, direct.text
    assert direct.json()["id"] == oldest_id

    refused = await client.get(f"/conversations/{oldest_id}", headers=outsider.headers)
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"] == "not_a_member"
