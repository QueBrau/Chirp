"""GET /chapters/{id}/role-meta: the role taxonomy served from permissions.py (c44).

The point of the endpoint is that the app stops hand-mirroring the eboard set and
the create_invite rule, so these tests assert against permissions.Role/EBOARD
directly — if the enum grows a role, the endpoint must reflect it with no edit here.
"""
from __future__ import annotations

from httpx import AsyncClient

from app.core.permissions import EBOARD, Role
from tests.conftest import MakeChapterWith, MakeUser

CANONICAL = [role.value for role in Role]
EBOARD_VALUES = [role.value for role in Role if role in EBOARD]
NON_EBOARD_VALUES = [role.value for role in Role if role not in EBOARD]


async def test_role_meta_mirrors_permissions_for_president(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """roles/eboard come straight from permissions.py; a president may invite everything,
    common roles listed first."""
    setup = await make_chapter_with("member")

    response = await client.get(
        f"/chapters/{setup.chapter_id}/role-meta", headers=setup.president.headers
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["roles"] == CANONICAL
    assert body["eboard"] == EBOARD_VALUES
    assert body["invitable"] == NON_EBOARD_VALUES + EBOARD_VALUES


async def test_role_meta_eboard_non_president_invites_non_eboard_only(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Any e-board role short of president mints only non-eboard invites (create_invite rule)."""
    setup = await make_chapter_with("treasurer")

    response = await client.get(
        f"/chapters/{setup.chapter_id}/role-meta", headers=setup.member.headers
    )

    assert response.status_code == 200, response.text
    assert response.json()["invitable"] == NON_EBOARD_VALUES


async def test_role_meta_plain_member_gets_empty_invitable(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """A plain member still sees the taxonomy (roster grouping needs it) but can mint nothing."""
    setup = await make_chapter_with("member")

    response = await client.get(
        f"/chapters/{setup.chapter_id}/role-meta", headers=setup.member.headers
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["roles"] == CANONICAL
    assert body["invitable"] == []


async def test_role_meta_is_org_scoped(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """A signed-in non-member gets 403, same as every other org-scoped chapter route (§8.4)."""
    setup = await make_chapter_with("member")
    outsider = await make_user("Outsider Olive")

    response = await client.get(
        f"/chapters/{setup.chapter_id}/role-meta", headers=outsider.headers
    )

    assert response.status_code == 403, response.text
