"""c340: GET /chapters/{chapter_id}/moderation-approval/history.

The read side of c337's audit row. Every approval row in these tests is written THROUGH
PATCH /chapters/{id}/moderation-approval — never seeded by hand — so the read is proven
against the write it exists for: if the route ever stopped logging, or logged a
different shape, these tests fail here rather than passing against a hand-made table.

WHAT EACH TEST PINS
  * gate: a chapter member gets 403 with the exact platform_admin_required detail; an
    unknown chapter is 404 chapter_not_found even to an admin (not an empty list that
    reads as a real chapter with no history).
  * ordering: two approvals then a revoke, by two different admins, come back newest
    first with each actor's CURRENT display name from the users join.
  * isolation: one planted row per filter clause — another chapter's real approval
    (target_id), an approve_chapter row with target_type 'user' and THIS chapter's id
    (target_type), a suspend_user row with target_type 'chapter' and this id (action)
    — so deleting any single clause of the route's WHERE turns this red, plus a
    same-actor unrelated user row excluded by all of them.
  * cap: 101 flips written, 100 read, and it is the OLDEST that is missing — asserted by
    reason, not by count, so a cap that kept the wrong end would fail.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy import text

from tests.conftest import ApiUser, MakeChapterWith, MakeUser, _grant_platform_admin

HISTORY = "/chapters/{}/moderation-approval/history"
FLIP = "/chapters/{}/moderation-approval"


async def _make_platform_admin(make_user: MakeUser, name: str) -> ApiUser:
    admin = await make_user(name)
    await _grant_platform_admin(admin.id)
    return admin


async def _flip(client: AsyncClient, chapter_id: str, admin: ApiUser, approved: bool, reason: str) -> None:
    resp = await client.patch(
        FLIP.format(chapter_id), json={"approved": approved, "reason": reason}, headers=admin.headers
    )
    assert resp.status_code == 200, resp.text


async def _insert_row(actor_id: str, action: str, target_type: str, target_id: str) -> None:
    """A moderation row that is NOT this chapter's approval history. Raw SQL on purpose:
    no route writes these shapes against an arbitrary target, and the point is to plant
    rows that are LEGAL under the per-column CHECKs (0032) yet must be excluded — each by
    exactly one clause of the route's filter, so deleting that clause turns this red."""
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        await session.execute(
            text(
                "INSERT INTO moderation_actions (actor_id, action, target_type, target_id, reason) "
                "VALUES (:actor, :action, :tt, :target, 'not chapter history')"
            ),
            {"actor": actor_id, "action": action, "tt": target_type, "target": target_id},
        )
        await session.commit()


async def test_a_chapter_member_is_refused_platform_admin_required(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member", approve_moderation=True)
    resp = await client.get(HISTORY.format(setup.chapter_id), headers=setup.member.headers)
    assert resp.status_code == 403, resp.text
    assert resp.json() == {"detail": "platform_admin_required"}


async def test_unknown_chapter_is_404_chapter_not_found(
    client: AsyncClient, make_user: MakeUser
) -> None:
    admin = await _make_platform_admin(make_user, "Platform Admin")
    resp = await client.get(HISTORY.format(uuid.uuid4()), headers=admin.headers)
    assert resp.status_code == 404, resp.text
    assert resp.json() == {"detail": "chapter_not_found"}


async def test_history_is_newest_first_with_each_actors_display_name(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    setup = await make_chapter_with("member", approve_moderation=False)
    ada = await _make_platform_admin(make_user, "Ada Admin")
    bo = await _make_platform_admin(make_user, "Bo Admin")

    await _flip(client, setup.chapter_id, ada, True, "first approval")
    await _flip(client, setup.chapter_id, bo, True, "re-confirmed")
    await _flip(client, setup.chapter_id, ada, False, "revoked pending review")

    resp = await client.get(HISTORY.format(setup.chapter_id), headers=ada.headers)
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert [(r["action"], r["reason"], r["actor_display_name"], r["actor_id"]) for r in rows] == [
        ("revoke_chapter", "revoked pending review", "Ada Admin", ada.id),
        ("approve_chapter", "re-confirmed", "Bo Admin", bo.id),
        ("approve_chapter", "first approval", "Ada Admin", ada.id),
    ]
    stamps = [r["created_at"] for r in rows]
    assert stamps == sorted(stamps, reverse=True)


async def test_history_is_isolated_to_this_chapters_chapter_rows(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    mine = await make_chapter_with("member", approve_moderation=False)
    other = await make_chapter_with("member", approve_moderation=False)
    admin = await _make_platform_admin(make_user, "Platform Admin")

    await _flip(client, mine.chapter_id, admin, True, "mine")
    # Excluded by target_id alone: a real approval of a DIFFERENT chapter.
    await _flip(client, other.chapter_id, admin, True, "other chapter")
    # Excluded by target_type alone: an approve_chapter action whose target_type is
    # 'user' but whose target_id IS this chapter's id. Legal under 0032's per-column
    # CHECKs; only the target_type clause keeps it out.
    await _insert_row(admin.id, "approve_chapter", "user", mine.chapter_id)
    # Excluded by the action clause alone: a non-approval action correctly targeted
    # at this chapter. Only the action filter keeps it out of an APPROVAL history.
    await _insert_row(admin.id, "suspend_user", "chapter", mine.chapter_id)
    # Excluded by everything: same actor, unrelated user target.
    await _insert_row(admin.id, "suspend_user", "user", mine.member.id)

    resp = await client.get(HISTORY.format(mine.chapter_id), headers=admin.headers)
    assert resp.status_code == 200, resp.text
    assert [(r["action"], r["reason"]) for r in resp.json()] == [("approve_chapter", "mine")]


async def test_cap_keeps_the_newest_hundred_and_drops_the_oldest(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    setup = await make_chapter_with("member", approve_moderation=False)
    admin = await _make_platform_admin(make_user, "Platform Admin")

    for i in range(101):
        await _flip(client, setup.chapter_id, admin, i % 2 == 0, f"flip-{i:03d}")

    resp = await client.get(HISTORY.format(setup.chapter_id), headers=admin.headers)
    assert resp.status_code == 200, resp.text
    reasons = [r["reason"] for r in resp.json()]
    assert len(reasons) == 100
    # Which end got cut matters more than how many: the OLDEST write is the one missing.
    assert reasons[0] == "flip-100", "newest row must lead"
    assert reasons[-1] == "flip-001", "the cap must keep the newest 100"
    assert "flip-000" not in reasons, "the oldest row must be the one dropped"
