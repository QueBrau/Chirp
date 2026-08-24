"""GET /chapters/{chapter_id}/meetings/with-attendance (board card c156).

The secretary dashboard built this shape itself: list_meetings, then get_attendance
once per meeting. A semester of meetings was a semester of requests on every load.

The load-bearing test here is the equivalence one - this endpoint has to return
exactly what the N+1 it replaces returned, or the screen changes behavior while
claiming to be a performance fix.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from httpx import AsyncClient

from tests.conftest import ChapterSetup, MakeChapterWith, MakeUser

SPRING = datetime(2026, 3, 1, 18, 0, tzinfo=timezone.utc)


async def _create_meeting(
    client: AsyncClient,
    setup: ChapterSetup,
    title: str = "Chapter meeting",
    when: datetime | None = None,
) -> str:
    created = await client.post(
        f"/chapters/{setup.chapter_id}/meetings",
        json={"title": title, "meeting_date": (when or SPRING).isoformat()},
        headers=setup.president.headers,
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


async def _mark(
    client: AsyncClient,
    setup: ChapterSetup,
    meeting_id: str,
    entries: list[tuple[str, str]],
) -> None:
    response = await client.put(
        f"/chapters/{setup.chapter_id}/meetings/{meeting_id}/attendance",
        json={"entries": [{"user_id": uid, "status": status} for uid, status in entries]},
        headers=setup.member.headers,
    )
    assert response.status_code == 200, response.text


async def _bundle(client: AsyncClient, setup: ChapterSetup, **params: str) -> list[dict]:
    response = await client.get(
        f"/chapters/{setup.chapter_id}/meetings/with-attendance",
        params=params or None,
        headers=setup.member.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---- the shape ----


async def test_each_meeting_carries_its_own_sheet(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """Rows must land on the meeting they belong to, not be pooled across meetings."""
    setup = await make_chapter_with("secretary")
    first = await _create_meeting(client, setup, "Week 1", SPRING)
    second = await _create_meeting(client, setup, "Week 2", SPRING + timedelta(days=7))
    await _mark(client, setup, first, [(setup.president.id, "present")])
    await _mark(client, setup, second, [(setup.president.id, "absent")])

    bundles = {b["meeting"]["id"]: b for b in await _bundle(client, setup)}

    assert [(a["user_id"], a["status"]) for a in bundles[first]["attendance"]] == [
        (setup.president.id, "present")
    ]
    assert [(a["user_id"], a["status"]) for a in bundles[second]["attendance"]] == [
        (setup.president.id, "absent")
    ]


async def test_a_meeting_with_no_sheet_comes_back_empty_not_missing(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """LEFT join, deliberately. An inner join drops the meeting entirely, and a meeting
    that vanishes from the dashboard is worse than one showing zero attendance - the
    secretary would think the create failed and log it a second time."""
    setup = await make_chapter_with("secretary")
    marked = await _create_meeting(client, setup, "Marked", SPRING)
    unmarked = await _create_meeting(client, setup, "Never marked", SPRING + timedelta(days=1))
    await _mark(client, setup, marked, [(setup.president.id, "present")])

    bundles = {b["meeting"]["id"]: b for b in await _bundle(client, setup)}

    assert unmarked in bundles, "a meeting with no attendance must still be returned"
    assert bundles[unmarked]["attendance"] == []


async def test_it_returns_exactly_what_the_n_plus_1_returned(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """THE contract for this card: one call, same answer.

    Walks the old path by hand - list_meetings, then get_attendance per meeting - and
    asserts the bundle matches it meeting for meeting, row for row, in the same order.
    A faster endpoint that returns something subtly different is not a refactor.
    """
    setup = await make_chapter_with("secretary")
    other = await make_user("Second Member")
    invite = await client.post(
        f"/chapters/{setup.chapter_id}/invites",
        json={"role": "member"},
        headers=setup.president.headers,
    )
    assert invite.status_code == 201, invite.text
    joined = await client.post(
        "/chapters/join", json={"code": invite.json()["code"]}, headers=other.headers
    )
    assert joined.status_code == 201, joined.text

    for index in range(3):
        meeting_id = await _create_meeting(
            client, setup, f"Week {index}", SPRING + timedelta(days=7 * index)
        )
        if index != 1:  # leave one meeting unmarked on purpose
            await _mark(
                client,
                setup,
                meeting_id,
                [(setup.president.id, "present"), (other.id, "excused")],
            )

    listed = await client.get(
        f"/chapters/{setup.chapter_id}/meetings", headers=setup.member.headers
    )
    assert listed.status_code == 200, listed.text
    expected = []
    for meeting in listed.json():
        sheet = await client.get(
            f"/chapters/{setup.chapter_id}/meetings/{meeting['id']}/attendance",
            headers=setup.member.headers,
        )
        assert sheet.status_code == 200, sheet.text
        expected.append({"meeting": meeting, "attendance": sheet.json()})

    assert await _bundle(client, setup) == expected


async def test_most_recent_meeting_first(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Same order as list_meetings, so swapping the call cannot reorder the screen."""
    setup = await make_chapter_with("secretary")
    await _create_meeting(client, setup, "Oldest", SPRING)
    await _create_meeting(client, setup, "Newest", SPRING + timedelta(days=14))
    await _create_meeting(client, setup, "Middle", SPRING + timedelta(days=7))

    titles = [b["meeting"]["title"] for b in await _bundle(client, setup)]

    assert titles == ["Newest", "Middle", "Oldest"]


async def test_another_chapters_meetings_never_appear(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("secretary")
    other = await make_chapter_with("secretary")
    mine = await _create_meeting(client, setup, "Mine", SPRING)
    await _create_meeting(client, other, "Theirs", SPRING)

    bundles = await _bundle(client, setup)

    assert [b["meeting"]["id"] for b in bundles] == [mine]


# ---- the window ----


async def test_the_window_bounds_which_meetings_come_back(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("secretary")
    await _create_meeting(
        client, setup, "Last fall", datetime(2025, 11, 4, 18, 0, tzinfo=timezone.utc)
    )
    await _create_meeting(client, setup, "This spring", SPRING)

    bundles = await _bundle(
        client,
        setup,
        start="2026-01-01T00:00:00+00:00",
        end="2026-06-01T00:00:00+00:00",
    )

    assert [b["meeting"]["title"] for b in bundles] == ["This spring"]


async def test_an_inverted_window_is_refused(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Shares _meeting_window with attendance-summary, so the two cannot disagree
    about what a window means - including how they refuse a nonsensical one."""
    setup = await make_chapter_with("secretary")
    response = await client.get(
        f"/chapters/{setup.chapter_id}/meetings/with-attendance",
        params={"start": "2026-06-01T00:00:00+00:00", "end": "2026-01-01T00:00:00+00:00"},
        headers=setup.member.headers,
    )
    assert response.status_code == 422, response.text


async def test_the_route_is_not_swallowed_by_the_meeting_id_route(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Declared below GET /meetings/{meeting_id}, "with-attendance" parses as a uuid
    and every call 422s. A 200 proves the declaration order still holds."""
    setup = await make_chapter_with("secretary")
    response = await client.get(
        f"/chapters/{setup.chapter_id}/meetings/with-attendance",
        headers=setup.member.headers,
    )
    assert response.status_code == 200, response.text
    assert response.json() == []


# ---- authorization ----


async def test_president_can_read_the_bundle(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("president")
    await _create_meeting(client, setup)
    response = await client.get(
        f"/chapters/{setup.chapter_id}/meetings/with-attendance",
        headers=setup.president.headers,
    )
    assert response.status_code == 200, response.text


async def test_plain_member_cannot_read_the_bundle(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """list_meetings is member-readable but this is not: it carries the attendance
    sheets, which get_attendance already gates on MINUTES_ADMIN. Bundling a
    member-readable list with officer-only data must take the stricter gate."""
    setup = await make_chapter_with("member")
    attempt = await client.get(
        f"/chapters/{setup.chapter_id}/meetings/with-attendance",
        headers=setup.member.headers,
    )
    assert attempt.status_code == 403, attempt.text


async def test_treasurer_cannot_read_the_bundle(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("treasurer")
    attempt = await client.get(
        f"/chapters/{setup.chapter_id}/meetings/with-attendance",
        headers=setup.member.headers,
    )
    assert attempt.status_code == 403, attempt.text


async def test_a_secretary_of_another_chapter_is_refused(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    target = await make_chapter_with("secretary")
    outsider = await make_chapter_with("secretary")
    await _create_meeting(client, target)

    attempt = await client.get(
        f"/chapters/{target.chapter_id}/meetings/with-attendance",
        headers=outsider.member.headers,
    )
    assert attempt.status_code == 403, attempt.text
    assert attempt.json()["detail"] == "not_a_member"
