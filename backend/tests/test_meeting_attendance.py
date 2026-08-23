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


# ---- board c149: the upsert became one statement, so these guard its new edges ----


async def test_rewriting_the_sheet_updates_rather_than_duplicating(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """PUT twice for the same member updates the row; it does not insert a second one.

    This is the property the old read-then-branch loop provided by hand and that
    ON CONFLICT DO UPDATE now provides in the database. Worth its own test because
    losing it would not raise - it would silently return two rows for one member,
    and the composite primary key means the second insert would actually error, so
    the failure would surface as a 500 on the secretary's second save.
    """
    setup = await make_chapter_with("secretary")
    meeting_id = await _create_meeting(client, setup)
    url = f"/chapters/{setup.chapter_id}/meetings/{meeting_id}/attendance"

    first = await client.put(
        url,
        json={"entries": [{"user_id": setup.president.id, "status": "present"}]},
        headers=setup.member.headers,
    )
    assert first.status_code == 200, first.text

    second = await client.put(
        url,
        json={"entries": [{"user_id": setup.president.id, "status": "excused"}]},
        headers=setup.member.headers,
    )
    assert second.status_code == 200, second.text

    read = await client.get(url, headers=setup.member.headers)
    assert read.status_code == 200, read.text
    assert [(r["user_id"], r["status"]) for r in read.json()] == [
        (setup.president.id, "excused")
    ], "second PUT should have updated the existing row, not added another"


async def test_the_same_member_listed_twice_in_one_sheet_does_not_500(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """A duplicated member within ONE payload resolves to the last status given.

    Postgres cannot apply ON CONFLICT to two rows that collide inside the same
    statement - it raises `ON CONFLICT DO UPDATE command cannot affect row a second
    time`. That makes a client-side duplicate a 500 rather than a validation error,
    so the route collapses duplicates before the insert. Sending the same member
    twice is a client bug, but it must not take the whole sheet down with it.
    """
    setup = await make_chapter_with("secretary")
    meeting_id = await _create_meeting(client, setup)
    url = f"/chapters/{setup.chapter_id}/meetings/{meeting_id}/attendance"

    response = await client.put(
        url,
        json={
            "entries": [
                {"user_id": setup.president.id, "status": "present"},
                {"user_id": setup.president.id, "status": "absent"},
            ]
        },
        headers=setup.member.headers,
    )

    assert response.status_code == 200, response.text
    assert [(r["user_id"], r["status"]) for r in response.json()] == [
        (setup.president.id, "absent")
    ]


async def test_an_oversized_sheet_is_refused_by_validation(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """`entries` is bounded, so the caller cannot choose how much work the request costs.

    Unbounded, one PUT was an arbitrary number of writes from a single authenticated
    officer. The bound is deliberately far above any real roster, so this asserts the
    limit exists rather than pinning a product decision about chapter size.
    """
    setup = await make_chapter_with("secretary")
    meeting_id = await _create_meeting(client, setup)

    response = await client.put(
        f"/chapters/{setup.chapter_id}/meetings/{meeting_id}/attendance",
        json={
            "entries": [
                {"user_id": setup.president.id, "status": "present"} for _ in range(501)
            ]
        },
        headers=setup.member.headers,
    )

    assert response.status_code == 422, response.text
