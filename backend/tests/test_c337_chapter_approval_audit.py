"""c337: PATCH /chapters/{id}/moderation-approval writes a moderation_actions row.

c325 built the platform-admin setter for chapters.moderation_approved and documented
"NO AUDIT ROW": moderation_actions.action and .target_type were CHECK-constrained
(0011, widened by 0017, target_type recreated by 0022) to sets that admitted neither a
chapter-approval action nor a 'chapter' target. Migration 0032 widens both; this file
proves the route now uses that room, the way 0017's own history says such a route must
be tested: through the write that actually hits the constraint, not only through the
403/404 cases that would pass with the audit insert deleted outright.

WHAT EACH TEST PINS
  * approve writes ONE row: actor = the acting platform admin (never the president who
    happens to hold the flag from make_chapter_with), action = approve_chapter,
    target_type = chapter, target_id = the chapter.
  * revoke writes revoke_chapter — a distinct action, not approve_chapter with a flag
    hidden somewhere. The audit log is the one place that must not blur what happened
    (0017's principle), and moderation_actions has no details column to hide it in.
  * idempotent re-approval STILL logs: an admin confirming a standing approval is an
    action someone took, and "who last confirmed this org" is an answerable question
    only if that row exists.
  * a caller-supplied reason is stored verbatim; with none given the row carries a
    fixed, honest placeholder rather than an empty string.
  * a refused call (403) writes nothing — the audit log records actions, not attempts.

test_c325_moderation_approval.py is left untouched and must keep passing: the request
shape is backward-compatible (reason is optional).
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import text

from tests.conftest import ApiUser, MakeChapterWith, MakeUser, _grant_platform_admin

DEFAULT_REASON = "platform admin decision, no reason given"


async def _make_platform_admin(make_user: MakeUser) -> ApiUser:
    admin = await make_user("Platform Admin")
    await _grant_platform_admin(admin.id)
    return admin


async def _audit_rows(chapter_id: str) -> list[dict]:
    """Every moderation_actions row targeting this chapter, oldest first. Read with raw
    SQL so the assertion is about what persisted, not what the ORM would map."""
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        result = await session.execute(
            text(
                "SELECT actor_id::text, action, target_type, reason "
                "FROM moderation_actions WHERE target_id = :id ORDER BY created_at, id"
            ),
            {"id": chapter_id},
        )
        return [dict(row._mapping) for row in result.all()]


async def test_approving_writes_an_approve_chapter_row_with_the_admin_as_actor(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    setup = await make_chapter_with("member", approve_moderation=False)
    admin = await _make_platform_admin(make_user)
    assert await _audit_rows(setup.chapter_id) == [], "setup is broken: rows pre-exist"

    resp = await client.patch(
        f"/chapters/{setup.chapter_id}/moderation-approval",
        json={"approved": True},
        headers=admin.headers,
    )
    assert resp.status_code == 200, resp.text

    rows = await _audit_rows(setup.chapter_id)
    assert rows == [
        {
            "actor_id": admin.id,
            "action": "approve_chapter",
            "target_type": "chapter",
            "reason": DEFAULT_REASON,
        }
    ]
    # The actor is the ADMIN who called, not the chapter's president (who also holds
    # is_platform_admin incidentally from POST /chapters) — see test_c325's docstring.
    assert rows[0]["actor_id"] != setup.president.id


async def test_revoking_writes_a_distinct_revoke_chapter_row(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    setup = await make_chapter_with("member", approve_moderation=True)
    admin = await _make_platform_admin(make_user)

    resp = await client.patch(
        f"/chapters/{setup.chapter_id}/moderation-approval",
        json={"approved": False, "reason": "org dissolved per campus registrar"},
        headers=admin.headers,
    )
    assert resp.status_code == 200, resp.text

    rows = await _audit_rows(setup.chapter_id)
    assert [(r["action"], r["reason"]) for r in rows] == [
        ("revoke_chapter", "org dissolved per campus registrar")
    ]
    assert rows[0]["actor_id"] == admin.id
    assert rows[0]["target_type"] == "chapter"


async def test_idempotent_reapproval_still_logs_a_second_row(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """c325 made re-approval a plain 200 with unchanged state. The audit log disagrees
    with 'nothing happened': an admin confirmed the approval, and that is recorded."""
    setup = await make_chapter_with("member", approve_moderation=False)
    admin = await _make_platform_admin(make_user)

    for _ in range(2):
        resp = await client.patch(
            f"/chapters/{setup.chapter_id}/moderation-approval",
            json={"approved": True},
            headers=admin.headers,
        )
        assert resp.status_code == 200, resp.text

    rows = await _audit_rows(setup.chapter_id)
    assert [r["action"] for r in rows] == ["approve_chapter", "approve_chapter"]


async def test_approve_then_revoke_reads_back_as_a_history(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    setup = await make_chapter_with("member", approve_moderation=False)
    admin = await _make_platform_admin(make_user)

    for approved in (True, False):
        resp = await client.patch(
            f"/chapters/{setup.chapter_id}/moderation-approval",
            json={"approved": approved},
            headers=admin.headers,
        )
        assert resp.status_code == 200, resp.text

    rows = await _audit_rows(setup.chapter_id)
    assert [r["action"] for r in rows] == ["approve_chapter", "revoke_chapter"]


async def test_a_refused_call_writes_no_audit_row(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member", approve_moderation=False)

    resp = await client.patch(
        f"/chapters/{setup.chapter_id}/moderation-approval",
        json={"approved": True},
        headers=setup.member.headers,
    )
    assert resp.status_code == 403, resp.text
    assert await _audit_rows(setup.chapter_id) == []


async def test_a_blank_reason_is_rejected_not_stored(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """Optional means absent, not empty: an empty string would put a blank reason on an
    audit row, which is the thing the placeholder exists to prevent."""
    setup = await make_chapter_with("member", approve_moderation=False)
    admin = await _make_platform_admin(make_user)

    resp = await client.patch(
        f"/chapters/{setup.chapter_id}/moderation-approval",
        json={"approved": True, "reason": ""},
        headers=admin.headers,
    )
    assert resp.status_code == 422, resp.text
    assert await _audit_rows(setup.chapter_id) == []
