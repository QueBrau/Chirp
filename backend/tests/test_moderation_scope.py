"""Moderation scoping (SECURITY-REVIEW finding 1): reports and yak removal must be
scoped to the target's campus, not readable/actionable by e-board members platform-wide."""
from __future__ import annotations

import uuid

from httpx import AsyncClient

from tests.conftest import ApiUser, MakeChapterWith, MakeUser


async def _bootstrap_user_with_campus(
    client: AsyncClient, campus_id: str, display_name: str = "Campus User"
) -> ApiUser:
    """Bootstrap a user pinned to a specific campus_id.

    make_user (conftest) doesn't expose campus_id, and posting a yak requires
    user.campus_id == the target campus (routers/yaks.py _require_campus_user), so
    this mirrors make_user's flow with campus_id added to the bootstrap body.
    """
    uid = f"uid-{uuid.uuid4().hex}"
    headers = {"X-Debug-Firebase-Uid": uid}
    email = f"{uid}@example.edu"
    response = await client.post(
        "/auth/bootstrap",
        json={
            "email": email,
            "display_name": display_name,
            "account_type": "greek",
            "campus_id": campus_id,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return ApiUser(id=response.json()["id"], firebase_uid=uid, email=email, headers=headers)


async def test_reporter_in_other_campus_cannot_see_report(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """A report filed against chapter A's post is invisible to chapter B's e-board (a
    different campus) and visible to chapter A's e-board (the matching campus).

    Before the fix, list_reports had no WHERE clause at all: any e-board member of ANY
    chapter saw every report system-wide, including forwarded_plaintext of reported
    E2EE messages.
    """
    chapter_a = await make_chapter_with("president")
    chapter_b = await make_chapter_with("president")

    post = await client.post(
        f"/chapters/{chapter_a.chapter_id}/posts",
        json={"body": "hello chapter A"},
        headers=chapter_a.president.headers,
    )
    assert post.status_code == 201, post.text
    post_id = post.json()["id"]

    reporter = await make_user("Reporter")
    report = await client.post(
        "/moderation/reports",
        json={"target_type": "post", "target_id": post_id, "reason": "spam"},
        headers=reporter.headers,
    )
    assert report.status_code == 201, report.text
    report_id = report.json()["id"]
    assert report.json()["campus_id"] is not None, "campus_id should resolve via post->chapter"

    # Chapter B's president: e-board of SOME active chapter (the old, vulnerable check
    # would pass) but not of campus A -> must NOT see the report.
    other_campus_view = await client.get(
        "/moderation/reports", headers=chapter_b.president.headers
    )
    assert other_campus_view.status_code == 200, other_campus_view.text
    assert all(r["id"] != report_id for r in other_campus_view.json())

    # Chapter A's president: e-board of the matching campus -> DOES see it.
    same_campus_view = await client.get(
        "/moderation/reports", headers=chapter_a.president.headers
    )
    assert same_campus_view.status_code == 200, same_campus_view.text
    assert any(r["id"] == report_id for r in same_campus_view.json())


async def test_remove_yak_cross_campus_is_403(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """An e-board member of an unrelated campus's chapter cannot remove another
    campus's yak; the matching campus's e-board still can.

    Before the fix, any e-board member of any chapter could remove any campus's yaks.
    """
    chapter_a = await make_chapter_with("president")
    chapter_b = await make_chapter_with("president")

    chapter_a_detail = await client.get(
        f"/chapters/{chapter_a.chapter_id}", headers=chapter_a.president.headers
    )
    assert chapter_a_detail.status_code == 200, chapter_a_detail.text
    campus_a_id = chapter_a_detail.json()["campus_id"]

    yakker = await _bootstrap_user_with_campus(client, campus_a_id, "Yakker")
    yak = await client.post(
        f"/campuses/{campus_a_id}/yaks",
        json={"body": "anonymous yak"},
        headers=yakker.headers,
    )
    assert yak.status_code == 201, yak.text
    yak_id = yak.json()["id"]

    # Chapter B's president is e-board somewhere, but not on campus A -> 403.
    cross_campus_removal = await client.post(
        f"/moderation/yaks/{yak_id}/remove",
        json={"reason": "test"},
        headers=chapter_b.president.headers,
    )
    assert cross_campus_removal.status_code == 403, cross_campus_removal.text
    assert cross_campus_removal.json() == {"detail": "insufficient_role"}

    # Sanity: chapter A's president (matching campus) CAN remove it.
    same_campus_removal = await client.post(
        f"/moderation/yaks/{yak_id}/remove",
        json={"reason": "test"},
        headers=chapter_a.president.headers,
    )
    assert same_campus_removal.status_code == 204, same_campus_removal.text
