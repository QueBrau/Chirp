"""Account suspension + moderation audit trail (board card c76): the LIVE /terms claims
"we can remove content and suspend accounts that break these rules" — these tests fail
if that capability regresses to just being words."""
from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy import text

from tests.conftest import MakeChapterWith, MakeUser, set_campus, verify_campus


async def _moderation_action_rows(
    target_type: str, target_id: str
) -> list[dict[str, object]]:
    """Direct DB read of moderation_actions for a target — the audit trail itself,
    not just the API's view of current state."""
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        result = await session.execute(
            text(
                "SELECT actor_id, action, reason FROM moderation_actions "
                "WHERE target_type = :target_type AND target_id = :target_id "
                "ORDER BY created_at"
            ),
            {"target_type": target_type, "target_id": target_id},
        )
        return [dict(row._mapping) for row in result]


async def _grant_platform_admin(user_id: str) -> None:
    """Flip is_platform_admin directly in the DB — mirrors conftest's private helper
    (not imported: it's module-private there) since suspend/unsuspend needs an admin."""
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        await session.execute(
            text("UPDATE users SET is_platform_admin = true WHERE id = :id"),
            {"id": user_id},
        )
        await session.commit()


async def test_non_admin_cannot_suspend(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """An ordinary user (not platform_admin) gets 403, not a suspension."""
    caller = await make_user("Not An Admin")
    target = await make_user("Target")

    response = await client.post(
        f"/moderation/users/{target.id}/suspend",
        json={"reason": "spam"},
        headers=caller.headers,
    )
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "platform_admin_required"}


async def test_suspend_writes_audit_row_and_blocks_the_account(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """Suspending: (1) sets users.suspended_*, (2) writes a moderation_actions row
    (who/what/why), and (3) the suspended user's very next authenticated request is
    rejected — not just future moderation actions, ANY endpoint."""
    admin = await make_user("Platform Admin")
    await _grant_platform_admin(admin.id)
    target = await make_user("Rule Breaker")

    # Sanity: target can act before suspension. Uses POST /moderation/reports (plain
    # get_current_user, nothing else) rather than GET /auth/me — that route resolves
    # the user via get_user_by_uid directly, NOT get_current_user (it needs 404 vs 401
    # for an unregistered caller, see its docstring), so it would never have exercised
    # the suspension check either way.
    report_before = await client.post(
        "/moderation/reports",
        json={"target_type": "user", "reason": "unrelated"},
        headers=target.headers,
    )
    assert report_before.status_code == 201, report_before.text

    suspend = await client.post(
        f"/moderation/users/{target.id}/suspend",
        json={"reason": "harassment"},
        headers=admin.headers,
    )
    assert suspend.status_code == 200, suspend.text
    body = suspend.json()
    assert body["suspended_at"] is not None
    assert body["suspension_reason"] == "harassment"
    assert body["suspended_by"] == admin.id

    rows = await _moderation_action_rows("user", target.id)
    assert len(rows) == 1, rows
    assert rows[0]["action"] == "suspend_user"
    assert rows[0]["actor_id"] == uuid.UUID(admin.id)
    assert rows[0]["reason"] == "harassment"

    # The account is rejected on its NEXT request, at get_current_user — any endpoint
    # that resolves the caller through it, not just the moderation endpoints themselves.
    report_after = await client.post(
        "/moderation/reports",
        json={"target_type": "user", "reason": "unrelated"},
        headers=target.headers,
    )
    assert report_after.status_code == 403, report_after.text
    assert report_after.json() == {"detail": "account_suspended"}

    # Double-suspend is a conflict, not a silent no-op.
    again = await client.post(
        f"/moderation/users/{target.id}/suspend",
        json={"reason": "again"},
        headers=admin.headers,
    )
    assert again.status_code == 409, again.text


async def test_unsuspend_restores_access_and_is_also_audited(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """Unsuspend clears the block and writes its own audit row alongside the original
    suspend row — the history of both actions survives the state being cleared."""
    admin = await make_user("Platform Admin")
    await _grant_platform_admin(admin.id)
    target = await make_user("Reformed User")

    suspend = await client.post(
        f"/moderation/users/{target.id}/suspend",
        json={"reason": "cooldown"},
        headers=admin.headers,
    )
    assert suspend.status_code == 200, suspend.text

    # See test_suspend_writes_audit_row_and_blocks_the_account: POST /moderation/reports
    # is the canary because it (unlike GET /auth/me) actually resolves the caller via
    # get_current_user.
    blocked = await client.post(
        "/moderation/reports",
        json={"target_type": "user", "reason": "unrelated"},
        headers=target.headers,
    )
    assert blocked.status_code == 403, blocked.text

    unsuspend = await client.post(
        f"/moderation/users/{target.id}/unsuspend",
        json={"reason": "appeal granted"},
        headers=admin.headers,
    )
    assert unsuspend.status_code == 200, unsuspend.text
    out = unsuspend.json()
    assert out["suspended_at"] is None
    assert out["suspension_reason"] is None
    assert out["suspended_by"] is None

    restored = await client.post(
        "/moderation/reports",
        json={"target_type": "user", "reason": "unrelated"},
        headers=target.headers,
    )
    assert restored.status_code == 201, restored.text

    rows = await _moderation_action_rows("user", target.id)
    actions = [r["action"] for r in rows]
    assert actions == ["suspend_user", "unsuspend_user"], rows

    # Unsuspending an already-active account is a conflict, not a silent no-op.
    again = await client.post(
        f"/moderation/users/{target.id}/unsuspend",
        json={"reason": "noop"},
        headers=admin.headers,
    )
    assert again.status_code == 409, again.text


async def test_remove_yak_writes_audit_row(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The pre-existing yak-removal route now leaves an audit trail too (it already
    marked removed_at/removed_reason; what was missing was WHO)."""
    chapter = await make_chapter_with("president")
    await verify_campus(chapter.president.id)

    chapter_detail = await client.get(
        f"/chapters/{chapter.chapter_id}", headers=chapter.president.headers
    )
    assert chapter_detail.status_code == 200, chapter_detail.text
    campus_id = chapter_detail.json()["campus_id"]

    uid = f"uid-{uuid.uuid4().hex}"
    headers = {"X-Debug-Firebase-Uid": uid}
    bootstrap = await client.post(
        "/auth/bootstrap",
        json={
            "email": f"{uid}@example.edu",
            "display_name": "Yakker",
            "account_type": "greek",
        },
        headers=headers,
    )
    assert bootstrap.status_code == 201, bootstrap.text
    # c85: campus is server-owned, so pin it directly instead of claiming it in the
    # bootstrap body. Without this the yak POST below 403s and the audit-row assertion
    # never runs — which would look like the audit trail is broken rather than like
    # the yakker simply has no campus.
    await set_campus(bootstrap.json()["id"], campus_id)

    yak = await client.post(
        f"/campuses/{campus_id}/yaks", json={"body": "bad yak"}, headers=headers
    )
    assert yak.status_code == 201, yak.text
    yak_id = yak.json()["id"]

    removal = await client.post(
        f"/moderation/yaks/{yak_id}/remove",
        json={"reason": "rule violation"},
        headers=chapter.president.headers,
    )
    assert removal.status_code == 204, removal.text

    rows = await _moderation_action_rows("yak", yak_id)
    assert len(rows) == 1, rows
    assert rows[0]["action"] == "remove_content"
    assert rows[0]["actor_id"] == uuid.UUID(chapter.president.id)
    assert rows[0]["reason"] == "rule violation"


async def test_remove_post_hides_it_from_the_feed_and_is_audited(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Moderator post removal actually disappears the post from the chapter feed
    (via the same deleted_at column feed.py filters on for self-delete) and is
    distinguishable from a self-delete via removed_reason, plus an audit row."""
    chapter = await make_chapter_with("president")
    await verify_campus(chapter.president.id)

    post = await client.post(
        f"/chapters/{chapter.chapter_id}/posts",
        json={"body": "rule-breaking post"},
        headers=chapter.president.headers,
    )
    assert post.status_code == 201, post.text
    post_id = post.json()["id"]

    removal = await client.post(
        "/moderation/content/remove",
        json={"target_type": "post", "target_id": post_id, "reason": "spam"},
        headers=chapter.president.headers,
    )
    assert removal.status_code == 204, removal.text

    listing = await client.get(
        f"/chapters/{chapter.chapter_id}/posts", headers=chapter.president.headers
    )
    assert listing.status_code == 200, listing.text
    assert all(p["id"] != post_id for p in listing.json())

    rows = await _moderation_action_rows("post", post_id)
    assert len(rows) == 1, rows
    assert rows[0]["action"] == "remove_content"
    assert rows[0]["reason"] == "spam"

    # Already-removed content is a conflict, not a silent duplicate audit row.
    again = await client.post(
        "/moderation/content/remove",
        json={"target_type": "post", "target_id": post_id, "reason": "again"},
        headers=chapter.president.headers,
    )
    assert again.status_code == 409, again.text


async def test_remove_content_cross_campus_is_403(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """An e-board member of an unrelated campus cannot remove another campus's post
    (mirrors test_remove_yak_cross_campus_is_403 for the new generic route)."""
    chapter_a = await make_chapter_with("president")
    chapter_b = await make_chapter_with("president")
    await verify_campus(chapter_a.president.id)
    await verify_campus(chapter_b.president.id)

    post = await client.post(
        f"/chapters/{chapter_a.chapter_id}/posts",
        json={"body": "chapter A content"},
        headers=chapter_a.president.headers,
    )
    assert post.status_code == 201, post.text
    post_id = post.json()["id"]

    cross_campus = await client.post(
        "/moderation/content/remove",
        json={"target_type": "post", "target_id": post_id, "reason": "test"},
        headers=chapter_b.president.headers,
    )
    assert cross_campus.status_code == 403, cross_campus.text
    assert cross_campus.json() == {"detail": "insufficient_role"}
