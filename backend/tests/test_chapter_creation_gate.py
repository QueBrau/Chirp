"""Chapter creation is platform-admin only (board card c28, SECURITY-REVIEW finding 1).

Self-serve POST /chapters let any authenticated user become a chapter's
president (full EBOARD powers) — the last privilege-escalation vector. There
is no API to grant is_platform_admin; it is flipped directly in the DB.
"""
from __future__ import annotations

import uuid

from httpx import AsyncClient

from tests.conftest import MakeCampus, MakeUser, _grant_platform_admin


def _chapter_body(campus_id: str) -> dict[str, str]:
    return {
        "campus_id": campus_id,
        "org_name": f"Sigma Test {uuid.uuid4().hex[:6]}",
        "chapter_name": "Alpha",
    }


async def test_non_admin_create_chapter_is_403(
    client: AsyncClient, make_user: MakeUser, make_campus: MakeCampus
) -> None:
    """A regular authenticated user cannot self-serve a chapter."""
    user = await make_user("Regular Student")
    campus_id = await make_campus()

    response = await client.post(
        "/chapters", json=_chapter_body(campus_id), headers=user.headers
    )
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "platform_admin_required"}


async def test_platform_admin_create_chapter_is_201_and_creator_is_president(
    client: AsyncClient, make_user: MakeUser, make_campus: MakeCampus
) -> None:
    """A platform admin creates a chapter and is auto-inserted as its president."""
    admin = await make_user("Platform Admin")
    await _grant_platform_admin(admin.id)
    campus_id = await make_campus()

    created = await client.post(
        "/chapters", json=_chapter_body(campus_id), headers=admin.headers
    )
    assert created.status_code == 201, created.text
    chapter_id = created.json()["id"]

    members = await client.get(
        f"/chapters/{chapter_id}/members", headers=admin.headers
    )
    assert members.status_code == 200, members.text
    roster = members.json()
    assert len(roster) == 1
    assert roster[0]["user_id"] == admin.id
    assert roster[0]["role"] == "president"
