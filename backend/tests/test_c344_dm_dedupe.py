"""DM dedup, rate limit, and dm_key partitioning (board c344).

Before this card, POST /conversations had no dedup and no rate limit at all: every
call for kind="dm" inserted a brand-new Conversation + ConversationMember row pair,
so repeat taps on the same person, or a retried request, produced N duplicate DM
rows that persisted in the inbox forever. These tests pin the fix: a canonical
dm_key (models.messaging.Conversation) collapses repeat requests for the SAME
participant pair into HTTP 200 + the existing row, a per-caller rate limit bounds
conversation creation itself, and neither behavior loosens who is allowed to talk —
dedup and the limit both run strictly after every existing eligibility check.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rate_limits import CONVERSATION_CREATE_LIMIT
from tests.conftest import MakeChapterWith, MakeUser, share_verified_campus


async def _dm_key_of(conversation_id: str) -> str | None:
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        result = await session.execute(
            text("SELECT dm_key FROM conversations WHERE id = :id"),
            {"id": conversation_id},
        )
        return result.scalar_one()


async def test_repeat_dm_request_reuses_the_existing_conversation(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """The headline: a second POST for the same pair is a 200, not a new row."""
    a = await make_user("Alex")
    b = await make_user("Blair")
    await share_verified_campus(a.id, b.id)

    first = await client.post(
        "/conversations", json={"kind": "dm", "member_user_ids": [b.id]}, headers=a.headers
    )
    assert first.status_code == 201, first.text
    conversation_id = first.json()["id"]

    second = await client.post(
        "/conversations", json={"kind": "dm", "member_user_ids": [b.id]}, headers=a.headers
    )
    assert second.status_code == 200, second.text
    assert second.json()["id"] == conversation_id

    # The other party naming the FIRST party back is the same pair too — reuse
    # must not be direction-sensitive.
    reverse = await client.post(
        "/conversations", json={"kind": "dm", "member_user_ids": [a.id]}, headers=b.headers
    )
    assert reverse.status_code == 200, reverse.text
    assert reverse.json()["id"] == conversation_id

    from app.db import get_session_factory

    async with get_session_factory()() as session:
        count = await session.execute(
            text("SELECT count(*) FROM conversations WHERE dm_key = :key"),
            {"key": await _dm_key_of(conversation_id)},
        )
        assert count.scalar_one() == 1


async def test_reuse_clears_left_at_on_both_members(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """A departed member's row is rejoined on reuse, or the reused conversation
    would be a 200 pointing at a conversation only ONE side can still see."""
    a = await make_user("Casey")
    b = await make_user("Drew")
    await share_verified_campus(a.id, b.id)

    created = await client.post(
        "/conversations", json={"kind": "dm", "member_user_ids": [b.id]}, headers=a.headers
    )
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]

    left = await client.post(f"/conversations/{conversation_id}/leave", headers=b.headers)
    assert left.status_code == 200, left.text
    assert left.json()["left_at"] is not None

    # b no longer sees it.
    b_inbox = await client.get("/conversations", headers=b.headers)
    assert conversation_id not in {row["id"] for row in b_inbox.json()}

    reused = await client.post(
        "/conversations", json={"kind": "dm", "member_user_ids": [b.id]}, headers=a.headers
    )
    assert reused.status_code == 200, reused.text
    assert reused.json()["id"] == conversation_id
    members_by_user = {m["user_id"]: m for m in reused.json()["members"]}
    assert members_by_user[b.id]["left_at"] is None
    assert members_by_user[a.id]["left_at"] is None

    b_inbox_after = await client.get("/conversations", headers=b.headers)
    assert conversation_id in {row["id"] for row in b_inbox_after.json()}


async def test_dm_dedupe_scoped_to_chapter_context(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """A chapter-scoped DM and a chapterless DM between the SAME two people stay
    two distinct conversations — dedup does not collapse across chapter_id.

    Falsification: if dm_key ignored chapter_id (the literal "sorted pair only"
    example), the second POST here would reuse the first and this assertion of
    TWO distinct ids would fail.
    """
    setup = await make_chapter_with("historian")
    # setup.member and setup.president share an active chapter (a valid
    # chapter-scoped pair) AND, once put on one campus, are also a valid
    # chapterless pair — exactly the "same two people, two contexts" case dm_key
    # must keep apart.
    await share_verified_campus(setup.member.id, setup.president.id)

    scoped_pair = await client.post(
        "/conversations",
        json={
            "chapter_id": setup.chapter_id,
            "kind": "dm",
            "member_user_ids": [setup.president.id],
        },
        headers=setup.member.headers,
    )
    assert scoped_pair.status_code == 201, scoped_pair.text
    scoped_id = scoped_pair.json()["id"]

    chapterless_pair = await client.post(
        "/conversations",
        json={"kind": "dm", "member_user_ids": [setup.president.id]},
        headers=setup.member.headers,
    )
    assert chapterless_pair.status_code == 201, chapterless_pair.text
    chapterless_id = chapterless_pair.json()["id"]

    assert scoped_id != chapterless_id, (
        "a chapter-scoped and a chapterless DM between the same pair collapsed "
        "into one conversation — dm_key must partition on chapter_id"
    )

    # And repeating the SCOPED request still reuses the scoped row specifically.
    scoped_repeat = await client.post(
        "/conversations",
        json={
            "chapter_id": setup.chapter_id,
            "kind": "dm",
            "member_user_ids": [setup.president.id],
        },
        headers=setup.member.headers,
    )
    assert scoped_repeat.status_code == 200, scoped_repeat.text
    assert scoped_repeat.json()["id"] == scoped_id


async def test_group_and_malformed_dm_never_get_a_key(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """Groups, and a "dm" naming more than one other recipient, get no dm_key and
    no dedup — repeating either creates a SECOND row, not a reuse.

    Falsification: without the exactly-two-members guard, an implementation might
    key one or both of these too, either erroring against the partial unique index
    the first time a caller tries it twice, or wrongly deduping unrelated rows.
    """
    a = await make_user("Group Starter")
    b = await make_user("Group Member One")
    c = await make_user("Group Member Two")
    await share_verified_campus(a.id, b.id, c.id)

    group_first = await client.post(
        "/conversations",
        json={"kind": "group", "member_user_ids": [b.id, c.id]},
        headers=a.headers,
    )
    assert group_first.status_code == 201, group_first.text
    assert await _dm_key_of(group_first.json()["id"]) is None

    group_second = await client.post(
        "/conversations",
        json={"kind": "group", "member_user_ids": [b.id, c.id]},
        headers=a.headers,
    )
    assert group_second.status_code == 201, group_second.text
    assert group_second.json()["id"] != group_first.json()["id"]

    malformed_first = await client.post(
        "/conversations",
        json={"kind": "dm", "member_user_ids": [b.id, c.id]},
        headers=a.headers,
    )
    assert malformed_first.status_code == 201, malformed_first.text
    assert await _dm_key_of(malformed_first.json()["id"]) is None

    malformed_second = await client.post(
        "/conversations",
        json={"kind": "dm", "member_user_ids": [b.id, c.id]},
        headers=a.headers,
    )
    assert malformed_second.status_code == 201, malformed_second.text
    assert malformed_second.json()["id"] != malformed_first.json()["id"]


async def test_conversation_creation_rate_limit_counts_every_call(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """A per-caller budget bounds POST /conversations itself, at the REAL configured
    ceiling — not a monkeypatched one, since limit_per_user's dependency closes over
    the limit tuple at route-decoration time and a post-import patch of the module
    constant would not reach it.

    Every call here targets the SAME person, so calls 2..20 are dm_key REUSES (200),
    not new rows — proving the limit is charged on the call, not on "a row was
    created" (the manager's decision: "Every POST consumes one budget unit whether
    it creates a row or reuses one"). Without that, dedupe would hand a script an
    unlimited way to keep hitting the endpoint by reusing the same pair forever.

    Falsification: RED without dependencies=[Depends(limit_per_user(...))] wired on
    the route — all 21 calls below would succeed. GREEN after: the 21st is refused.
    """
    max_calls, _ = CONVERSATION_CREATE_LIMIT
    caller = await make_user("Prolific Caller")
    target = await make_user("Repeat Target")
    await share_verified_campus(caller.id, target.id)

    for i in range(max_calls):
        response = await client.post(
            "/conversations",
            json={"kind": "dm", "member_user_ids": [target.id]},
            headers=caller.headers,
        )
        assert response.status_code in (200, 201), f"call {i + 1}: {response.text}"

    refused = await client.post(
        "/conversations",
        json={"kind": "dm", "member_user_ids": [target.id]},
        headers=caller.headers,
    )
    assert refused.status_code == 429, refused.text
    assert refused.json()["detail"] == "conversation_create_rate_limited"


def test_conversation_create_limit_has_headroom_over_real_use() -> None:
    """Same 2x-headroom standard test_c259_rate_limits.py's _assert_headroom uses
    for every other write limit in this module: the busiest plausible real pattern
    — a handful of DMs plus a couple of group starts in one sitting, called it 8 —
    must sit well under the ceiling, or ordinary use starts 429ing."""
    max_calls, _window = CONVERSATION_CREATE_LIMIT
    assert 8 * 2 <= max_calls, (
        f"CONVERSATION_CREATE_LIMIT ceiling {max_calls} is not comfortably above "
        "the realistic heavy pattern of 8 conversation-creation calls in one sitting"
    )


def _start_conversation_select_together(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hold both requests at the dm_key pre-check SELECT before letting either
    proceed — the same synchronization shape test_c342_block_intents.py uses for
    user_blocks. Without this, asyncio.gather's two coroutines COULD race for
    real (cooperative scheduling at await points), but nothing would force them
    to, so a flaky pass would tell us nothing about the IntegrityError fallback.
    """
    execute = AsyncSession.execute
    ready = asyncio.Event()
    arrived = 0

    async def synchronized(
        session: AsyncSession, statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        nonlocal arrived
        is_conversations_select = getattr(statement, "is_select", False) and any(
            getattr(table, "name", None) == "conversations"
            for table in statement.get_final_froms()
        )
        if is_conversations_select:
            arrived += 1
            if arrived >= 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), timeout=10)
        return await execute(session, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", synchronized)


async def test_concurrent_duplicate_dm_create_dedupes(
    client: AsyncClient, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two simultaneous POSTs for the same pair: both pass the pre-check SELECT
    (forced by the synchronization above) before either has inserted, so ONE of
    the two inserts must hit the unique index and be caught.

    Falsification: without the try/except IntegrityError fallback in
    create_conversation, this test's second request raises an unhandled
    IntegrityError (surfaced as a 500) instead of a clean 200 pointing at the
    same row the first request created.
    """
    a = await make_user("Race Starter")
    b = await make_user("Race Target")
    await share_verified_campus(a.id, b.id)

    _start_conversation_select_together(monkeypatch)

    first, second = await asyncio.gather(
        client.post(
            "/conversations", json={"kind": "dm", "member_user_ids": [b.id]}, headers=a.headers
        ),
        client.post(
            "/conversations", json={"kind": "dm", "member_user_ids": [b.id]}, headers=a.headers
        ),
    )
    assert first.status_code in (200, 201), first.text
    assert second.status_code in (200, 201), second.text
    # Exactly one of the two is the "winner" (201); the other reused it (200).
    assert {first.status_code, second.status_code} == {200, 201}
    assert first.json()["id"] == second.json()["id"]

    from app.db import get_session_factory

    async with get_session_factory()() as session:
        count = await session.execute(
            text(
                "SELECT count(*) FROM conversations WHERE dm_key IS NOT NULL "
                "AND id = :id"
            ),
            {"id": first.json()["id"]},
        )
        assert count.scalar_one() == 1
        total = await session.execute(
            text("SELECT count(*) FROM conversations WHERE kind = 'dm'"),
        )
        assert total.scalar_one() == 1
