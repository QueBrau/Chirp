"""Chapter-scoped conversation membership authorization tests."""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from tests.conftest import MakeChapterWith, MakeUser


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
    """Ghost lineage placeholders are allowed; chapter_id=NULL DMs stay cross-chapter."""
    setup = await make_chapter_with("historian")
    other = await make_chapter_with("historian")
    ghost = await make_user("Historical Conversation Placeholder")
    await _set_ghost(ghost.id)

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
