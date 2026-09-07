"""c325: a platform-admin endpoint to set chapters.moderation_approved.

c308 (migration 0031) added this column and gated the whole moderation router on it,
but deliberately left NOTHING able to set it except a psql session (see the
migration's docstring, "WHAT IS DELIBERATELY NOT HERE") — the same no-API precedent
as is_platform_admin itself (c28). c325 is the card that decides whether that stays
true before self-serve chapter creation ships; this file exercises the endpoint that
answers it: PATCH /chapters/{chapter_id}/moderation-approval, gated the same way
account suspension already is (Depends(require_platform_admin)).

THE TRAP EVERY 403 TEST HERE IS BUILT TO AVOID, same family as test_c308's own
warning: make_chapter_with's creator is handed is_platform_admin directly in the DB,
because POST /chapters is itself platform-admin-gated (c28). A "president -> 403"
test that used that creator unmodified would actually be exercising a platform admin
who happens to also be a president, and would pass for the wrong reason — or fail to
catch a bug where require_platform_admin was replaced with something role-based.
_revoke_platform_admin below undoes that incidental grant so the president case tests
exactly what it claims to.

THE BEHAVIORAL TEST (test 7) reuses test_c308_moderation_decoupled.py's own pattern
(GET /moderation/reports, asserting the exact 403 body) so the two files agree on what
"gated" and "ungated" look like for the same column, from the read side and the write
side.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy import text

from tests.conftest import (
    ApiUser,
    MakeChapterWith,
    MakeUser,
    _grant_platform_admin,
)


async def _make_platform_admin(make_user: MakeUser, name: str = "Platform Admin") -> ApiUser:
    """A user with is_platform_admin=true, not tied to any chapter's membership —
    kept distinct from the chapter's own president/member so the actor exercising this
    route is never accidentally also the resource under test."""
    admin = await make_user(name)
    await _grant_platform_admin(admin.id)
    return admin


async def _revoke_platform_admin(user_id: str) -> None:
    """Undo the platform-admin grant make_chapter_with's president incidentally holds
    (POST /chapters is itself platform-admin-gated, c28) — same raw-SQL shape as
    conftest's _grant_platform_admin, inverse direction. Without this, a "president"
    test subject is secretly also a platform admin and a 403 assertion against them
    would pass (or fail) for the wrong reason.
    """
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        await session.execute(
            text("UPDATE users SET is_platform_admin = false WHERE id = :id"),
            {"id": user_id},
        )
        await session.commit()


async def _read_moderation_approved(chapter_id: str) -> bool:
    """Read chapters.moderation_approved directly. ChapterOut does not expose this
    column (by design — see schemas/identity.py), and the falsification standard here
    is proving the column actually persisted, not trusting the response body's echo of
    what was requested."""
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        result = await session.execute(
            text("SELECT moderation_approved FROM chapters WHERE id = :id"),
            {"id": chapter_id},
        )
        return bool(result.scalar_one())


async def test_platform_admin_approves_an_unapproved_chapter(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    setup = await make_chapter_with("member", approve_moderation=False)
    assert await _read_moderation_approved(setup.chapter_id) is False, (
        "setup is broken: this test proves nothing if the chapter started approved"
    )
    admin = await _make_platform_admin(make_user)

    resp = await client.patch(
        f"/chapters/{setup.chapter_id}/moderation-approval",
        json={"approved": True},
        headers=admin.headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"id": setup.chapter_id, "moderation_approved": True}
    assert await _read_moderation_approved(setup.chapter_id) is True


async def test_platform_admin_revokes_an_approved_chapter(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    setup = await make_chapter_with("member", approve_moderation=True)
    assert await _read_moderation_approved(setup.chapter_id) is True, (
        "setup is broken: this test proves nothing if the chapter started unapproved"
    )
    admin = await _make_platform_admin(make_user)

    resp = await client.patch(
        f"/chapters/{setup.chapter_id}/moderation-approval",
        json={"approved": False},
        headers=admin.headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"id": setup.chapter_id, "moderation_approved": False}
    assert await _read_moderation_approved(setup.chapter_id) is False


async def test_reapproving_an_already_approved_chapter_is_idempotent(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    setup = await make_chapter_with("member", approve_moderation=True)
    admin = await _make_platform_admin(make_user)

    resp = await client.patch(
        f"/chapters/{setup.chapter_id}/moderation-approval",
        json={"approved": True},
        headers=admin.headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"id": setup.chapter_id, "moderation_approved": True}
    assert await _read_moderation_approved(setup.chapter_id) is True


async def test_a_president_is_refused_platform_admin_required(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member", approve_moderation=False)
    # See the module docstring: undo the incidental platform-admin grant so this
    # actually isolates the president role.
    await _revoke_platform_admin(setup.president.id)

    resp = await client.patch(
        f"/chapters/{setup.chapter_id}/moderation-approval",
        json={"approved": True},
        headers=setup.president.headers,
    )
    assert resp.status_code == 403, resp.text
    assert resp.json() == {"detail": "platform_admin_required"}
    assert await _read_moderation_approved(setup.chapter_id) is False


async def test_a_plain_member_is_refused_platform_admin_required(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member", approve_moderation=False)

    resp = await client.patch(
        f"/chapters/{setup.chapter_id}/moderation-approval",
        json={"approved": True},
        headers=setup.member.headers,
    )
    assert resp.status_code == 403, resp.text
    assert resp.json() == {"detail": "platform_admin_required"}
    assert await _read_moderation_approved(setup.chapter_id) is False


async def test_unknown_chapter_id_is_404_chapter_not_found(
    client: AsyncClient, make_user: MakeUser
) -> None:
    admin = await _make_platform_admin(make_user)

    resp = await client.patch(
        f"/chapters/{uuid.uuid4()}/moderation-approval",
        json={"approved": True},
        headers=admin.headers,
    )
    assert resp.status_code == 404, resp.text
    assert resp.json() == {"detail": "chapter_not_found"}


async def test_approval_is_what_opens_the_presidents_moderation_queue(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """The behavioral proof c325 exists for: this endpoint is not just a column flip,
    it is the API path that makes GET /moderation/reports reachable — same 403 body
    test_c308_moderation_decoupled.py asserts for an unapproved chapter's officer, and
    the same 200 it asserts for an approved one, now reached through this route's
    write instead of conftest's direct DB helper.
    """
    setup = await make_chapter_with("member", approve_moderation=False)

    before = await client.get("/moderation/reports", headers=setup.president.headers)
    assert before.status_code == 403, before.text
    assert before.json() == {"detail": "insufficient_role"}

    admin = await _make_platform_admin(make_user)
    approved = await client.patch(
        f"/chapters/{setup.chapter_id}/moderation-approval",
        json={"approved": True},
        headers=admin.headers,
    )
    assert approved.status_code == 200, approved.text

    after = await client.get("/moderation/reports", headers=setup.president.headers)
    assert after.status_code == 200, after.text
