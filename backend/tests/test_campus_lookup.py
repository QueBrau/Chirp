"""GET /campuses/{campus_id} (c46): resolve users.campus_id to a real name.

This route exists so the app stops hardcoding a mock campus on Profile and the Chirp
board header. It is deliberately readable by any registered caller — campus name and
slug are public-facing labels, not org-scoped data.
"""
from __future__ import annotations

import uuid

from httpx import AsyncClient

from tests.conftest import MakeCampus, MakeUser


async def test_registered_user_can_read_a_campus(
    client: AsyncClient, make_campus: MakeCampus, make_user: MakeUser
) -> None:
    """Any registered caller gets the campus row back, id echoed and name populated."""
    campus_id = await make_campus()
    user = await make_user("Campus Reader")

    response = await client.get(f"/campuses/{campus_id}", headers=user.headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == campus_id
    assert body["name"] == "Test Campus"
    assert body["slug"].startswith("campus-")


async def test_unknown_campus_is_404(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """A well-formed but nonexistent campus id 404s rather than 500ing on a None row."""
    user = await make_user("Campus Reader")

    response = await client.get(f"/campuses/{uuid.uuid4()}", headers=user.headers)

    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "campus_not_found"}


async def test_campus_requires_authentication(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    """No bearer token is still a 401 — public-facing labels, not a public route."""
    campus_id = await make_campus()

    response = await client.get(f"/campuses/{campus_id}")

    assert response.status_code == 401, response.text
