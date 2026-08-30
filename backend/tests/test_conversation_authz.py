"""Chapter-scoped conversation membership authorization tests."""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from tests.conftest import (
    MakeChapterWith,
    MakeUser,
    set_campus,
    share_verified_campus,
)


async def _join_as_member(client: AsyncClient, setup, user) -> None:
    """Join a fixture user to a chapter through the invite API."""
    invite = await client.post(
        f"/chapters/{setup.chapter_id}/invites",
        json={"role": "member"},
        headers=setup.president.headers,
    )
    assert invite.status_code == 201, invite.text
    joined = await client.post(
        "/chapters/join", json={"code": invite.json()["code"]}, headers=user.headers
    )
    assert joined.status_code == 201, joined.text


async def _set_ghost(user_id: str) -> None:
    """Mark a fixture user as an allowed historical placeholder."""
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        await session.execute(
            text("UPDATE users SET is_ghost = true WHERE id = :user_id"),
            {"user_id": user_id},
        )
        await session.commit()


async def _set_membership_status(user_id: str, chapter_id: str, status: str) -> None:
    """Set a fixture membership status for the inactive-member case."""
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        await session.execute(
            text(
                "UPDATE memberships SET status = :status "
                "WHERE user_id = :user_id AND chapter_id = :chapter_id"
            ),
            {"status": status, "user_id": user_id, "chapter_id": chapter_id},
        )
        await session.commit()


@pytest.mark.asyncio
async def test_chapter_conversation_requires_creator_and_active_members(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """A chapter conversation cannot be used to bridge inactive or other chapters."""
    setup = await make_chapter_with("historian")
    other = await make_chapter_with("historian")
    outsider = await make_user("Conversation Outsider")
    inactive = await make_user("Inactive Conversation Member")
    await _join_as_member(client, setup, inactive)
    await _set_membership_status(inactive.id, setup.chapter_id, "inactive")

    creator_not_member = await client.post(
        "/conversations",
        json={
            "chapter_id": setup.chapter_id,
            "kind": "group",
            "member_user_ids": [setup.member.id],
        },
        headers=outsider.headers,
    )
    assert creator_not_member.status_code == 403
    assert creator_not_member.json() == {"detail": "not_a_member"}

    requested_not_member = await client.post(
        "/conversations",
        json={
            "chapter_id": setup.chapter_id,
            "kind": "dm",
            "member_user_ids": [outsider.id],
        },
        headers=setup.member.headers,
    )
    assert requested_not_member.status_code == 403
    assert requested_not_member.json() == {"detail": "not_a_member"}

    requested_cross_chapter = await client.post(
        "/conversations",
        json={
            "chapter_id": setup.chapter_id,
            "kind": "dm",
            "member_user_ids": [other.member.id],
        },
        headers=setup.member.headers,
    )
    assert requested_cross_chapter.status_code == 403
    assert requested_cross_chapter.json() == {"detail": "not_a_member"}

    requested_inactive = await client.post(
        "/conversations",
        json={
            "chapter_id": setup.chapter_id,
            "kind": "dm",
            "member_user_ids": [inactive.id],
        },
        headers=setup.member.headers,
    )
    assert requested_inactive.status_code == 403
    assert requested_inactive.json() == {"detail": "not_a_member"}


@pytest.mark.asyncio
async def test_ghost_chapter_member_and_cross_chapter_dm_remain_allowed(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """Ghost placeholders allowed; chapter_id=NULL DMs stay cross-chapter WITHIN a campus.

    c243 narrowed the second half of this test, so read the change deliberately rather
    than as a relaxation. It used to assert that two people from two chapters could DM
    with chapter_id omitted — but make_chapter_with mints a NEW campus per call, so what
    it actually pinned was a cross-CAMPUS DM between two strangers who share nothing at
    all. That was the hole: with chapter_id omitted the route ran no check whatsoever, so
    the assertion passed for every pair of user ids in the database, not because these
    two were entitled to talk.

    SPEC §3 documents chapter_id as "NULL for cross-chapter DMs", and that documented
    feature is preserved and still asserted here — the two members are now put on one
    campus, which is what "different chapter, same school" actually means. The
    cross-campus case it used to permit is asserted as a refusal below, where it belongs.
    """
    setup = await make_chapter_with("historian")
    other = await make_chapter_with("historian")
    ghost = await make_user("Historical Conversation Placeholder")
    await _set_ghost(ghost.id)
    await share_verified_campus(setup.member.id, other.member.id)

    with_ghost = await client.post(
        "/conversations",
        json={
            "chapter_id": setup.chapter_id,
            "kind": "group",
            "member_user_ids": [ghost.id],
        },
        headers=setup.member.headers,
    )
    assert with_ghost.status_code == 201, with_ghost.text
    assert ghost.id in {member["user_id"] for member in with_ghost.json()["members"]}

    cross_chapter_dm = await client.post(
        "/conversations",
        json={
            "kind": "dm",
            "member_user_ids": [other.member.id],
        },
        headers=setup.member.headers,
    )
    assert cross_chapter_dm.status_code == 201, cross_chapter_dm.text
    assert cross_chapter_dm.json()["chapter_id"] is None


@pytest.mark.asyncio
async def test_chapterless_conversation_refuses_unreachable_recipients(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """Omitting chapter_id is not a way to reach anyone in the database (board c243).

    The regression this pins: the membership check lived inside `if chapter_id is not
    None`, so leaving chapter_id out skipped authorization entirely and any account could
    open a conversation naming any user id.
    """
    stranger = await make_user("Unrelated Stranger")
    caller = await make_user("Chapterless Caller")

    # Two accounts with nothing in common - no chapter, no campus. The pre-c243 route
    # answered 201 here.
    unrelated = await client.post(
        "/conversations",
        json={"kind": "dm", "member_user_ids": [stranger.id]},
        headers=caller.headers,
    )
    assert unrelated.status_code == 403, unrelated.text
    assert unrelated.json() == {"detail": "recipient_not_reachable"}

    # Same campus is the rule, so a DIFFERENT campus stays refused even though both
    # people are fully verified members of real chapters.
    here = await make_chapter_with("historian")
    away = await make_chapter_with("historian")
    await share_verified_campus(here.member.id)
    await share_verified_campus(away.member.id)
    cross_campus = await client.post(
        "/conversations",
        json={"kind": "dm", "member_user_ids": [away.member.id]},
        headers=here.member.headers,
    )
    assert cross_campus.status_code == 403, cross_campus.text
    assert cross_campus.json() == {"detail": "recipient_not_reachable"}

    # A shared campus that the CALLER has never proved with an .edu is not reachability
    # either - that is the c104/c105 bypass shape, and campus_access.py exists to refuse
    # exactly this. The recipient is verified; the caller is not.
    unverified_caller = await make_user("Unverified Caller")
    verified_peer = await make_user("Verified Peer")
    campus_id = await share_verified_campus(verified_peer.id)
    await set_campus(unverified_caller.id, campus_id, verified=False)
    unverified = await client.post(
        "/conversations",
        json={"kind": "dm", "member_user_ids": [verified_peer.id]},
        headers=unverified_caller.headers,
    )
    assert unverified.status_code == 403, unverified.text
    assert unverified.json() == {"detail": "recipient_not_reachable"}


@pytest.mark.asyncio
async def test_chapter_mates_may_dm_without_a_chapter_id(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """The rule must not break ordinary use: chapter mates can DM, unverified or not.

    Per Jose's Aug 16 ruling (core/campus_access.py) chapter membership stands on its own
    without an .edu, so this pair - who never verify a campus - must still be able to
    talk. A guard that refused here would be worse than the bug it fixes.
    """
    setup = await make_chapter_with("historian")
    mate = await make_user("Chapter Mate")
    await _join_as_member(client, setup, mate)

    created = await client.post(
        "/conversations",
        json={"kind": "dm", "member_user_ids": [mate.id]},
        headers=setup.member.headers,
    )
    assert created.status_code == 201, created.text
    assert created.json()["chapter_id"] is None


@pytest.mark.asyncio
async def test_member_user_ids_is_capped(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """One request must not be able to name an unbounded number of people (board c243)."""
    caller = await make_user("Bulk Caller")

    too_many = await client.post(
        "/conversations",
        json={"kind": "group", "member_user_ids": [str(uuid.uuid4()) for _ in range(257)]},
        headers=caller.headers,
    )
    assert too_many.status_code == 422, too_many.text

    # The cap is a length check only, and it must fire BEFORE the route does any work -
    # these ids do not exist, so anything other than 422 means the body was accepted.
    assert "member_user_ids" in too_many.text
