"""GET /auth/me: the client's way of learning its own campus_id after bootstrap."""
from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import MakeCampus, MakeUser


async def test_get_me_returns_caller_user_out(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """GET /auth/me returns the authenticated caller's own UserOut."""
    user = await make_user("Me Tester")

    response = await client.get("/auth/me", headers=user.headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == user.id
    assert body["email"] == user.email


async def test_get_me_reflects_campus_id(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    """A user bootstrapped with a campus_id sees it echoed back from /auth/me."""
    campus_id = await make_campus()
    bootstrap = await client.post(
        "/auth/bootstrap",
        json={
            "email": "campus-user@example.edu",
            "display_name": "Campus User",
            "account_type": "non_greek",
            "campus_id": campus_id,
        },
        headers={"X-Debug-Firebase-Uid": "uid-campus-me"},
    )
    assert bootstrap.status_code == 201, bootstrap.text

    response = await client.get(
        "/auth/me", headers={"X-Debug-Firebase-Uid": "uid-campus-me"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["campus_id"] == campus_id


async def test_get_me_without_auth_is_401(client: AsyncClient) -> None:
    """No X-Debug-Firebase-Uid header (emulated mode) is 401."""
    response = await client.get("/auth/me")
    assert response.status_code == 401, response.text
