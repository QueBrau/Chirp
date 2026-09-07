"""Who may call POST /chapters, board card c378.

UNTIL THIS CARD the gate was platform-admin only (board card c28 / SECURITY-REVIEW
finding 1): self-serve creation let any authenticated user become a chapter's
president (full EBOARD powers), and e-board membership used to grant campus
moderation outright, which made self-appointing into a campus's report queue as
cheap as typing an org name. Board card c308 (migration 0031) cut that wire —
chapters now carry `moderation_approved`, server_default FALSE, and moderation
never opens on a chapter this route creates — so c378 replaces the platform-admin
gate with campus verification instead of removing it outright. See
routers/chapters.py's create_chapter docstring for the full argument, and
test_c378_self_serve_chapter.py for the adversarial proof that founding still
grants no moderation.

THIS FILE IS NOW ABOUT THE GATE ITSELF: who may call the route, not what calling it
grants. Before c378 this file asserted `platform_admin_required` on every refusal
and 201 only for a platform admin; every one of those assertions is now WRONG on
current main; this file replaced them rather than leaving them to rot red.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy import text

from tests.conftest import MakeCampus, MakeUser, _grant_platform_admin, set_campus


def _chapter_body() -> dict[str, str]:
    """No campus_id (board c378): the schema has none any more, the server forces
    it from the caller's own verified campus."""
    return {
        "org_name": f"Sigma Test {uuid.uuid4().hex[:6]}",
        "chapter_name": "Alpha",
    }


async def _lapse_verification(user_id: str, campus_id: str) -> None:
    """Put a user PAST core.campus_access.VERIFICATION_TTL (365 days), not merely
    never-verified — the distinction the c378 brief calls out explicitly, and one
    `is_campus_verified` itself warns is easy to collapse into "has a campus_id".
    set_campus(..., verified=False) only builds the NULL case; this builds the
    other one, a real .edu proved once and since expired.
    """
    from app.db import get_session_factory

    stale = datetime.now(timezone.utc) - timedelta(days=400)
    async with get_session_factory()() as session:
        await session.execute(
            text(
                "UPDATE users SET campus_id = :campus, campus_verified_at = :verified_at "
                "WHERE id = :id"
            ),
            {"campus": campus_id, "id": user_id, "verified_at": stale},
        )
        await session.commit()


async def test_never_verified_user_cannot_create_a_chapter(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """A fresh account with no campus at all — the ordinary case, and the one that
    must never regress to a 201."""
    user = await make_user("Regular Student")

    response = await client.post("/chapters", json=_chapter_body(), headers=user.headers)
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "campus_unverified"}


async def test_campus_without_verification_cannot_create_a_chapter(
    client: AsyncClient, make_user: MakeUser, make_campus: MakeCampus
) -> None:
    """The exact c96/c104 bypass shape: a campus_id inherited from a chapter invite,
    with no .edu ever proved. core.campus_access.is_campus_verified's own docstring
    says a bare campus_id is not enough, and this is the test that would catch a
    regression back to that shortcut for chapter creation specifically."""
    user = await make_user("Invited But Unverified")
    campus_id = await make_campus()
    await set_campus(user.id, campus_id, verified=False)

    response = await client.post("/chapters", json=_chapter_body(), headers=user.headers)
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "campus_unverified"}


async def test_lapsed_verification_cannot_create_a_chapter(
    client: AsyncClient, make_user: MakeUser, make_campus: MakeCampus
) -> None:
    """A verification older than VERIFICATION_TTL (365 days) must refuse exactly
    like never-verified — is_campus_verified's whole reason for existing over a
    bare `campus_verified_at is not None` check."""
    user = await make_user("Lapsed Alum")
    campus_id = await make_campus()
    await _lapse_verification(user.id, campus_id)

    response = await client.post("/chapters", json=_chapter_body(), headers=user.headers)
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "campus_unverified"}


async def test_platform_admin_with_no_verification_is_still_refused(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """THE REGRESSION THIS FILE MOST NEEDS TO CATCH: is_platform_admin bought no
    exemption from c28's gate, and it must buy none from c378's replacement either.
    Board card c28's gate is not merely relaxed here, it is retired outright — an
    admin who never proved a campus is refused the same as anyone else."""
    admin = await make_user("Platform Admin")
    await _grant_platform_admin(admin.id)

    response = await client.post("/chapters", json=_chapter_body(), headers=admin.headers)
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "campus_unverified"}


async def test_verified_user_creates_a_chapter_and_becomes_its_active_president(
    client: AsyncClient, make_user: MakeUser, make_campus: MakeCampus
) -> None:
    """The positive case: a CAMPUS-VERIFIED, entirely ordinary (non-admin) student
    self-serves a chapter and is inserted as its active president — unchanged from
    what c28's gate always granted an admin, now reachable without one."""
    user = await make_user("Regular Verified Student")
    campus_id = await make_campus()
    await set_campus(user.id, campus_id)

    created = await client.post("/chapters", json=_chapter_body(), headers=user.headers)
    assert created.status_code == 201, created.text
    chapter_id = created.json()["id"]
    assert created.json()["campus_id"] == campus_id, (
        "the chapter must land on the caller's OWN verified campus"
    )

    members = await client.get(f"/chapters/{chapter_id}/members", headers=user.headers)
    assert members.status_code == 200, members.text
    roster = members.json()
    assert len(roster) == 1
    assert roster[0]["user_id"] == user.id
    assert roster[0]["role"] == "president"
    assert roster[0]["status"] == "active"
    assert roster[0]["display_name"] == "Regular Verified Student"
