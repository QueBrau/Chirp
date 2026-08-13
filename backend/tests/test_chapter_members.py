"""GET /chapters/{id}/members: display_name join (users.display_name) and its interaction
with org scoping (§8.4). Regression coverage for the MembershipOut.display_name field.
"""
from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import MakeChapterWith, MakeUser


async def test_list_members_returns_display_name(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Each row's display_name matches what was bootstrapped for that user, not a bare id."""
    setup = await make_chapter_with("treasurer")

    response = await client.get(
        f"/chapters/{setup.chapter_id}/members", headers=setup.member.headers
    )

    assert response.status_code == 200, response.text
    by_user = {row["user_id"]: row["display_name"] for row in response.json()}
    assert by_user[setup.president.id] == "Chapter President"
    assert by_user[setup.member.id] == "Chapter Treasurer"


async def test_list_members_no_drop_or_duplicate_across_several_members(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """A chapter with several members returns exactly one row per membership.

    An inner join written wrong could silently drop a member (e.g. a bad join
    condition) or fan out and repeat one (e.g. joining onto a table with more
    than one matching row) — verify the row count and the user_id -> display_name
    mapping are both exactly right.
    """
    setup = await make_chapter_with("president")
    expected = {setup.president.id: "Chapter President"}

    for role, name in (
        ("treasurer", "Treasurer Tammy"),
        ("secretary", "Secretary Sam"),
        ("member", "Member Mo"),
        ("pledge", "Pledge Pat"),
    ):
        invite = await client.post(
            f"/chapters/{setup.chapter_id}/invites",
            json={"role": role},
            headers=setup.president.headers,
        )
        assert invite.status_code == 201, invite.text
        member = await make_user(name)
        joined = await client.post(
            "/chapters/join",
            json={"code": invite.json()["code"]},
            headers=member.headers,
        )
        assert joined.status_code == 201, joined.text
        expected[member.id] = name

    response = await client.get(
        f"/chapters/{setup.chapter_id}/members", headers=setup.president.headers
    )

    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == len(expected), "row count must equal membership count exactly"
    user_ids = [row["user_id"] for row in rows]
    assert len(user_ids) == len(set(user_ids)), "no user_id should be repeated by a fan-out join"
    actual = {row["user_id"]: row["display_name"] for row in rows}
    assert actual == expected, "each name must be matched to the right user_id"


async def test_list_members_is_org_scoped_and_never_leaks_other_chapter(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The join doesn't disturb §8.4: non-members still get 403, and a chapter's roster
    never contains another chapter's members."""
    chapter_a = await make_chapter_with("member")
    chapter_b = await make_chapter_with("member")

    response = await client.get(
        f"/chapters/{chapter_b.chapter_id}/members", headers=chapter_a.member.headers
    )
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "not_a_member"}

    response = await client.get(
        f"/chapters/{chapter_a.chapter_id}/members", headers=chapter_a.member.headers
    )
    assert response.status_code == 200, response.text
    user_ids = {row["user_id"] for row in response.json()}
    assert user_ids == {chapter_a.president.id, chapter_a.member.id}
    assert chapter_b.president.id not in user_ids
    assert chapter_b.member.id not in user_ids


async def test_me_memberships_chapter_name_still_populated(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """GET /me/memberships still joins chapter_name in — the MembershipOut.display_name
    addition (joined from users, not chapters) must not disturb this unrelated join."""
    setup = await make_chapter_with("secretary")

    response = await client.get("/me/memberships", headers=setup.member.headers)

    assert response.status_code == 200, response.text
    memberships = response.json()
    assert len(memberships) == 1
    assert memberships[0]["chapter_id"] == setup.chapter_id
    assert memberships[0]["chapter_name"] == "Alpha"
    assert memberships[0]["org_name"]
    # This route doesn't join users — display_name stays unset, confirming the new
    # field is additive and doesn't require every route to populate it.
    assert memberships[0]["display_name"] is None
