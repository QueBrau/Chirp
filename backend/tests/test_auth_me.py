"""GET /auth/me: caller's user row plus active memberships, ordered by joined_at."""
from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import MakeChapterWith, MakeUser


async def test_unauthenticated_is_401(client: AsyncClient) -> None:
    """No auth headers at all raises 401 (missing_debug_uid in emulated mode)."""
    response = await client.get("/auth/me")

    assert response.status_code == 401


async def test_registered_user_with_no_membership_returns_empty_list(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """A bootstrapped user with no chapter membership gets 200 and an empty list."""
    user = await make_user()

    response = await client.get("/auth/me", headers=user.headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user"]["id"] == user.id
    assert body["user"]["email"] == user.email
    assert body["memberships"] == []


async def test_membership_appears_after_joining(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """After joining a chapter, the membership shows up in /auth/me."""
    setup = await make_chapter_with("member")

    response = await client.get("/auth/me", headers=setup.member.headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user"]["id"] == setup.member.id
    assert len(body["memberships"]) == 1
    membership = body["memberships"][0]
    assert membership["chapter_id"] == setup.chapter_id
    assert membership["user_id"] == setup.member.id
    assert membership["role"] == "member"
    assert membership["status"] == "active"


async def test_unregistered_identity_is_404_user_not_registered(client: AsyncClient) -> None:
    """A verified-but-never-bootstrapped uid gets 404 user_not_registered."""
    headers = {"X-Debug-Firebase-Uid": "uid-never-bootstrapped"}

    response = await client.get("/auth/me", headers=headers)

    assert response.status_code == 404
    assert response.json() == {"detail": "user_not_registered"}
