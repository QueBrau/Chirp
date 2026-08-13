"""GET /me/memberships: how the client learns its own chapter_id/role (no chapter_id in path)."""
from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import MakeChapterWith


async def test_me_memberships_returns_only_active_memberships_with_chapter_name(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """A member's own active membership comes back with role, chapter_id, and org_name."""
    setup = await make_chapter_with("treasurer")

    response = await client.get("/me/memberships", headers=setup.member.headers)

    assert response.status_code == 200, response.text
    memberships = response.json()
    assert len(memberships) == 1
    membership = memberships[0]
    assert membership["chapter_id"] == setup.chapter_id
    assert membership["role"] == "treasurer"
    assert membership["status"] == "active"
    assert membership["org_name"]  # joined chapter name, not just a bare chapter_id


async def test_me_memberships_excludes_non_active_status(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """A membership the president marks inactive is filtered out (status == "active" only)."""
    setup = await make_chapter_with("member")

    update = await client.patch(
        f"/chapters/{setup.chapter_id}/members",
        json={"user_id": setup.member.id, "status": "inactive"},
        headers=setup.president.headers,
    )
    assert update.status_code == 200, update.text

    response = await client.get("/me/memberships", headers=setup.member.headers)
    assert response.status_code == 200, response.text
    assert response.json() == []


async def test_me_memberships_across_multiple_chapters(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """A user who is president of one chapter and also joined another sees both, active only."""
    chapter_one = await make_chapter_with("president")
    chapter_two = await make_chapter_with("president")

    invite = await client.post(
        f"/chapters/{chapter_two.chapter_id}/invites",
        json={"role": "member"},
        headers=chapter_two.president.headers,
    )
    assert invite.status_code == 201, invite.text
    joined = await client.post(
        "/chapters/join",
        json={"code": invite.json()["code"]},
        headers=chapter_one.president.headers,
    )
    assert joined.status_code == 201, joined.text

    response = await client.get(
        "/me/memberships", headers=chapter_one.president.headers
    )
    assert response.status_code == 200, response.text
    chapter_ids = {m["chapter_id"] for m in response.json()}
    assert chapter_ids == {chapter_one.chapter_id, chapter_two.chapter_id}


async def test_me_memberships_without_auth_is_401(client: AsyncClient) -> None:
    """No X-Debug-Firebase-Uid header (emulated mode) is 401, matching GET /auth/me."""
    response = await client.get("/me/memberships")
    assert response.status_code == 401, response.text
