"""Org scoping (SPEC §8.4): cross-chapter access MUST 403; role gates on own chapter."""
from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import MakeChapterWith

SCOPED_GET_SUFFIXES = (
    "posts",
    "members",
    "ledger",
    "lineage",
    "meetings",
    "dues-cycles",
    "spend-approvals",
)


async def test_cross_chapter_access_is_403_not_a_member(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """A member of chapter A gets 403 not_a_member on every /chapters/{B}/* read."""
    chapter_a = await make_chapter_with("member")
    chapter_b = await make_chapter_with("president")

    for suffix in SCOPED_GET_SUFFIXES:
        response = await client.get(
            f"/chapters/{chapter_b.chapter_id}/{suffix}",
            headers=chapter_a.member.headers,
        )
        assert response.status_code == 403, (
            f"/chapters/{{B}}/{suffix} returned {response.status_code}: {response.text}"
        )
        assert response.json() == {"detail": "not_a_member"}, f"/chapters/{{B}}/{suffix}"


async def test_member_can_read_own_chapter(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """A plain member gets 200 on their own chapter's non-role-gated reads."""
    setup = await make_chapter_with("member")

    for suffix in ("posts", "members", "lineage", "meetings", "dues-cycles", "spend-approvals"):
        response = await client.get(
            f"/chapters/{setup.chapter_id}/{suffix}", headers=setup.member.headers
        )
        assert response.status_code == 200, (
            f"/chapters/{{A}}/{suffix} returned {response.status_code}: {response.text}"
        )


async def test_non_treasurer_member_own_ledger_is_403_insufficient_role(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """A non-treasurer member of the SAME chapter is blocked from the ledger by role."""
    setup = await make_chapter_with("member")

    response = await client.get(
        f"/chapters/{setup.chapter_id}/ledger", headers=setup.member.headers
    )
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "insufficient_role"}


async def test_treasurer_can_read_own_ledger(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Sanity: the role gate opens for a treasurer of the same chapter."""
    setup = await make_chapter_with("treasurer")

    response = await client.get(
        f"/chapters/{setup.chapter_id}/ledger", headers=setup.member.headers
    )
    assert response.status_code == 200, response.text


async def test_cross_chapter_invite_create_is_403_not_a_member(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """An e-board member of chapter A cannot mint an invite for chapter B (§8.4).

    create_invite is gated by require_role(*EBOARD), which is layered on
    get_current_membership — org scoping must reject the cross-chapter call
    before the role check ever runs.
    """
    chapter_a = await make_chapter_with("president")
    chapter_b = await make_chapter_with("president")

    response = await client.post(
        f"/chapters/{chapter_b.chapter_id}/invites",
        json={"role": "member"},
        headers=chapter_a.president.headers,
    )
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "not_a_member"}
