"""GET /chapters/{chapter_id}/meetings/{meeting_id}/attendance (board card c124).

The secretary screen has been silently 405ing on every attendance load. Found by a
static contract check, not by driving the UI: getAttendance() in meetings.ts and its
one call site both existed, pointed at a route the backend never registered — only
PUT (upsert_attendance) was ever wired up.

Gated on MINUTES_ADMIN rather than plain membership, deliberately checked against
precedent rather than assumed: list_meetings is member-readable, but
export_meetings_csv — which already exports this exact per-(meeting, member) data —
is already MINUTES_ADMIN-only. This route matches that existing precedent.
"""

from __future__ import annotations

from datetime import datetime, timezone

from httpx import AsyncClient

from tests.conftest import MakeChapterWith


async def _create_meeting(client: AsyncClient, setup, title: str = "Chapter meeting") -> str:
    created = await client.post(
        f"/chapters/{setup.chapter_id}/meetings",
        json={"title": title, "meeting_date": datetime.now(timezone.utc).isoformat()},
        headers=setup.president.headers,
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


async def test_secretary_can_read_an_empty_attendance_sheet(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("secretary")
    meeting_id = await _create_meeting(client, setup)

    read = await client.get(
        f"/chapters/{setup.chapter_id}/meetings/{meeting_id}/attendance",
        headers=setup.member.headers,
    )
    assert read.status_code == 200, read.text
    assert read.json() == []


async def test_the_read_route_returns_what_the_write_route_wrote(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The actual regression: a real client trusting getAttendance() must see the
    same rows putAttendance() already returns from its own response, not a 405."""
    setup = await make_chapter_with("secretary")
    meeting_id = await _create_meeting(client, setup)

    written = await client.put(
        f"/chapters/{setup.chapter_id}/meetings/{meeting_id}/attendance",
        json={"entries": [{"user_id": setup.president.id, "status": "present"}]},
        headers=setup.member.headers,
    )
    assert written.status_code == 200, written.text

    read = await client.get(
        f"/chapters/{setup.chapter_id}/meetings/{meeting_id}/attendance",
        headers=setup.member.headers,
    )
    assert read.status_code == 200, read.text
    assert [(r["user_id"], r["status"]) for r in read.json()] == [
        (setup.president.id, "present")
    ]


async def test_president_can_also_read_attendance(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """MINUTES_ADMIN is secretary-OR-president, same as the write it mirrors."""
    setup = await make_chapter_with("president")
    meeting_id = await _create_meeting(client, setup)

    read = await client.get(
        f"/chapters/{setup.chapter_id}/meetings/{meeting_id}/attendance",
        headers=setup.president.headers,
    )
    assert read.status_code == 200, read.text


async def test_plain_member_cannot_read_attendance(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Deliberately stricter than list_meetings: attendance is per-member presence
    data, checked against export_meetings_csv's precedent, not assumed."""
    setup = await make_chapter_with("member")
    meeting_id = await _create_meeting(client, setup)

    attempt = await client.get(
        f"/chapters/{setup.chapter_id}/meetings/{meeting_id}/attendance",
        headers=setup.member.headers,
    )
    assert attempt.status_code == 403, attempt.text


async def test_treasurer_cannot_read_attendance(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """A different e-board seat than secretary/president is still refused —
    MINUTES_ADMIN is not the whole EBOARD set."""
    setup = await make_chapter_with("treasurer")
    meeting_id = await _create_meeting(client, setup)

    attempt = await client.get(
        f"/chapters/{setup.chapter_id}/meetings/{meeting_id}/attendance",
        headers=setup.member.headers,
    )
    assert attempt.status_code == 403, attempt.text


async def test_unknown_meeting_is_404(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("secretary")
    attempt = await client.get(
        f"/chapters/{setup.chapter_id}/meetings/00000000-0000-0000-0000-000000000000/attendance",
        headers=setup.member.headers,
    )
    assert attempt.status_code == 404, attempt.text


async def test_a_meeting_from_another_chapter_is_404_not_leaked(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """_get_chapter_meeting scopes on (chapter_id, meeting_id) together — a
    secretary of chapter B must not be able to read chapter A's attendance by
    guessing a real meeting id and pairing it with their own chapter_id."""
    chapter_a = await make_chapter_with("president")
    chapter_b = await make_chapter_with("secretary")
    meeting_id = await _create_meeting(client, chapter_a)

    attempt = await client.get(
        f"/chapters/{chapter_b.chapter_id}/meetings/{meeting_id}/attendance",
        headers=chapter_b.member.headers,
    )
    assert attempt.status_code == 404, attempt.text
