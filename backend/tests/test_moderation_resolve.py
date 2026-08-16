"""Resolving a report — PATCH /moderation/reports/{id} (board card c91).

content_reports has always had a status column and GET /moderation/reports has always
returned it, but nothing could change it. The c35 moderation queue could remove a
reported yak and still not mark the report handled, so it faked the transition
client-side and every handled item reappeared as open on reload.

The happy path is the least interesting thing here. What these tests actually pin is
the scoping: being able to CLOSE another campus's report is SECURITY-REVIEW finding 1
with a write attached, and it is worse than the read was, because dismissing a report
is how you make a complaint disappear.
"""

from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import MakeChapterWith


async def _file_report_on_chapter_post(
    client: AsyncClient, setup, reason: str = "Spam"
) -> str:
    """Create a post in the chapter and report it; returns the report id."""
    post = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "reportable content"},
        headers=setup.president.headers,
    )
    assert post.status_code == 201, post.text
    report = await client.post(
        "/moderation/reports",
        json={"target_type": "post", "target_id": post.json()["id"], "reason": reason},
        headers=setup.president.headers,
    )
    assert report.status_code == 201, report.text
    return report.json()["id"]


async def test_resolving_a_report_takes_it_out_of_the_open_queue(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The actual c91 bug: handled items used to come back as open on every reload."""
    setup = await make_chapter_with("president")
    report_id = await _file_report_on_chapter_post(client, setup)

    before = await client.get("/moderation/reports", headers=setup.president.headers)
    assert before.status_code == 200, before.text
    assert [r["status"] for r in before.json() if r["id"] == report_id] == ["open"]

    resolved = await client.patch(
        f"/moderation/reports/{report_id}",
        json={"status": "actioned", "reason": "Removed the post"},
        headers=setup.president.headers,
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "actioned"

    after = await client.get("/moderation/reports", headers=setup.president.headers)
    assert [r["status"] for r in after.json() if r["id"] == report_id] == ["actioned"], (
        "the report must still be listed, but no longer open — the queue is a history, "
        "not a delete"
    )


async def test_dismissed_is_distinct_from_actioned(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Both mean 'a moderator looked at it', which is what empties the queue; the
    difference is whether anything happened, and that must survive."""
    setup = await make_chapter_with("president")
    report_id = await _file_report_on_chapter_post(client, setup)

    resolved = await client.patch(
        f"/moderation/reports/{report_id}",
        json={"status": "dismissed", "reason": "Not actually spam"},
        headers=setup.president.headers,
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "dismissed"


async def test_eboard_of_another_campus_cannot_resolve(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """THE ONE THAT MATTERS.

    make_chapter_with builds each chapter on its own campus, so chapter B's president
    is genuinely e-board — they pass _require_any_eboard — and must still be refused.
    A 403 here is the difference between "you must be a moderator" and "you must be a
    moderator OF THIS CAMPUS", and only the second one is worth anything: dismissing a
    report is how a complaint gets buried.
    """
    chapter_a = await make_chapter_with("president")
    chapter_b = await make_chapter_with("president")
    report_id = await _file_report_on_chapter_post(client, chapter_a)

    attempt = await client.patch(
        f"/moderation/reports/{report_id}",
        json={"status": "dismissed", "reason": "nothing to see here"},
        headers=chapter_b.president.headers,
    )
    assert attempt.status_code == 403, attempt.text

    # And it really did not land — checked from chapter A's side, not by trusting the code.
    still_open = await client.get("/moderation/reports", headers=chapter_a.president.headers)
    assert [r["status"] for r in still_open.json() if r["id"] == report_id] == ["open"]


async def test_plain_member_cannot_resolve(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Same campus, no e-board role — refused before campus scoping is even reached."""
    setup = await make_chapter_with("member")
    report_id = await _file_report_on_chapter_post(client, setup)

    attempt = await client.patch(
        f"/moderation/reports/{report_id}",
        json={"status": "dismissed", "reason": "let me through"},
        headers=setup.member.headers,
    )
    assert attempt.status_code == 403, attempt.text


async def test_double_resolve_is_a_conflict_not_a_silent_success(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Two moderators working one queue is the normal case. The second must be told
    their decision did not land rather than believing it overwrote the first."""
    setup = await make_chapter_with("president")
    report_id = await _file_report_on_chapter_post(client, setup)

    first = await client.patch(
        f"/moderation/reports/{report_id}",
        json={"status": "actioned", "reason": "handled"},
        headers=setup.president.headers,
    )
    assert first.status_code == 200, first.text

    second = await client.patch(
        f"/moderation/reports/{report_id}",
        json={"status": "dismissed", "reason": "changed my mind"},
        headers=setup.president.headers,
    )
    assert second.status_code == 409, second.text

    unchanged = await client.get("/moderation/reports", headers=setup.president.headers)
    assert [r["status"] for r in unchanged.json() if r["id"] == report_id] == ["actioned"]


async def test_unknown_report_is_404(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("president")
    attempt = await client.patch(
        "/moderation/reports/00000000-0000-0000-0000-000000000000",
        json={"status": "dismissed", "reason": "nope"},
        headers=setup.president.headers,
    )
    assert attempt.status_code == 404, attempt.text


async def test_reopening_is_rejected_by_the_schema(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """'open' is deliberately not a resolvable-to value: closing is a decision, undoing
    another moderator's decision is a different feature with no product answer yet."""
    setup = await make_chapter_with("president")
    report_id = await _file_report_on_chapter_post(client, setup)

    attempt = await client.patch(
        f"/moderation/reports/{report_id}",
        json={"status": "open", "reason": "reopening"},
        headers=setup.president.headers,
    )
    assert attempt.status_code == 422, attempt.text
