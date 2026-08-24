"""GET /chapters/{chapter_id}/meetings/attendance-summary (board card c82).

The secretary's real question - "how many has this person missed this semester" -
had no server answer, only listMeetings plus getAttendance per meeting and a client
loop. These tests pin the aggregate that replaced it, and one of them exists purely
to hold a line that is easy to cross by accident: attendance must be counted from
THIS chapter's meetings only. See test_a_dual_chapter_member_is_counted_per_chapter.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from httpx import AsyncClient

from tests.conftest import ApiUser, ChapterSetup, MakeChapterWith, MakeUser

SPRING = datetime(2026, 3, 1, 18, 0, tzinfo=timezone.utc)


async def _create_meeting(
    client: AsyncClient,
    setup: ChapterSetup,
    title: str = "Chapter meeting",
    when: datetime | None = None,
) -> str:
    created = await client.post(
        f"/chapters/{setup.chapter_id}/meetings",
        json={
            "title": title,
            "meeting_date": (when or SPRING).isoformat(),
        },
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


async def _join(
    client: AsyncClient, setup: ChapterSetup, user: ApiUser, role: str = "member"
) -> None:
    """Add an existing user to a chapter through the real invite/join flow."""
    invite = await client.post(
        f"/chapters/{setup.chapter_id}/invites",
        json={"role": role},
        headers=setup.president.headers,
    )
    assert invite.status_code == 201, invite.text
    joined = await client.post(
        "/chapters/join",
        json={"code": invite.json()["code"]},
        headers=user.headers,
    )
    assert joined.status_code == 201, joined.text


async def _summary(
    client: AsyncClient, setup: ChapterSetup, **params: str
) -> dict:
    response = await client.get(
        f"/chapters/{setup.chapter_id}/meetings/attendance-summary",
        params=params or None,
        headers=setup.member.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _member(payload: dict, user_id: str) -> dict:
    match = [m for m in payload["members"] if m["user_id"] == user_id]
    assert len(match) == 1, f"{user_id} appears {len(match)} times in {payload['members']}"
    return match[0]


# ---- the aggregate itself ----


async def test_a_member_who_was_never_marked_still_appears_with_zeros(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The unrecorded member is the one the secretary most needs to see.

    An attendance-spined query would return nothing for them and the screen would
    silently show a short roster - the member with no data would read as "fine".
    """
    setup = await make_chapter_with("secretary")
    await _create_meeting(client, setup)

    payload = await _summary(client, setup)

    assert payload["meetings_in_window"] == 1
    row = _member(payload, setup.president.id)
    assert (row["present"], row["absent"], row["excused"], row["recorded"]) == (0, 0, 0, 0)


async def test_counts_split_by_status_across_several_meetings(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("secretary")
    first = await _create_meeting(client, setup, "Week 1", SPRING)
    second = await _create_meeting(client, setup, "Week 2", SPRING + timedelta(days=7))
    third = await _create_meeting(client, setup, "Week 3", SPRING + timedelta(days=14))

    await _mark(client, setup, first, [(setup.president.id, "present")])
    await _mark(client, setup, second, [(setup.president.id, "absent")])
    await _mark(client, setup, third, [(setup.president.id, "excused")])

    payload = await _summary(client, setup)

    assert payload["meetings_in_window"] == 3
    row = _member(payload, setup.president.id)
    assert (row["present"], row["absent"], row["excused"]) == (1, 1, 1)
    assert row["recorded"] == 3


async def test_recorded_is_below_the_meeting_count_when_a_sheet_skipped_someone(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """meetings_in_window - recorded is "nobody marked them", which is not an absence.

    Both halves have to come back for the client to tell those apart, so this asserts
    the denominator and the numerator together rather than the counts alone.
    """
    setup = await make_chapter_with("secretary")
    quiet = await make_user("Never Marked")
    await _join(client, setup, quiet)

    first = await _create_meeting(client, setup, "Week 1", SPRING)
    await _create_meeting(client, setup, "Week 2", SPRING + timedelta(days=7))
    await _mark(client, setup, first, [(quiet.id, "present")])

    payload = await _summary(client, setup)

    row = _member(payload, quiet.id)
    assert payload["meetings_in_window"] == 2
    assert row["recorded"] == 1
    assert row["absent"] == 0, "an unrecorded meeting must not be reported as an absence"


async def test_every_active_member_gets_exactly_one_row(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """The join must not fan out: one row per member, however many meetings they attended."""
    setup = await make_chapter_with("secretary")
    extra = await make_user("Second Member")
    await _join(client, setup, extra)
    for index in range(3):
        meeting_id = await _create_meeting(
            client, setup, f"Week {index}", SPRING + timedelta(days=7 * index)
        )
        await _mark(client, setup, meeting_id, [(extra.id, "present")])

    payload = await _summary(client, setup)

    ids = [m["user_id"] for m in payload["members"]]
    assert len(ids) == len(set(ids)) == 3  # president, secretary, extra
    assert _member(payload, extra.id)["present"] == 3


async def test_a_member_who_went_inactive_mid_window_leaves_the_summary_but_not_the_record(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """DECIDED, not incidental: the roster spine is who is active NOW, so a member who
    goes inactive mid-window drops out of this report even though their attendance rows
    are inside the window and still exported by export.csv.

    That asymmetry is the point rather than a gap. The summary answers a forward-looking
    question - who currently owes a fine, who is at risk of losing good standing - and
    those are decisions about people still in the chapter. The historical record is the
    CSV, which is spined on attendance rows and keeps everyone who was ever marked.
    Both halves are asserted here so a future change cannot quietly flip one without
    the other going red.
    """
    setup = await make_chapter_with("secretary")
    departed = await make_user("Departed Member")
    await _join(client, setup, departed)
    meeting_id = await _create_meeting(client, setup, "Before leaving", SPRING)
    await _mark(client, setup, meeting_id, [(departed.id, "absent")])

    deactivated = await client.patch(
        f"/chapters/{setup.chapter_id}/members",
        json={"user_id": departed.id, "status": "inactive"},
        headers=setup.president.headers,
    )
    assert deactivated.status_code == 200, deactivated.text

    payload = await _summary(client, setup)
    assert departed.id not in [m["user_id"] for m in payload["members"]]
    assert payload["meetings_in_window"] == 1, "the meeting itself is still in the window"

    exported = await client.get(
        f"/chapters/{setup.chapter_id}/meetings/export.csv",
        headers=setup.member.headers,
    )
    assert exported.status_code == 200, exported.text
    assert "Departed Member" in exported.text, (
        "the attendance record must survive the member leaving - only the live "
        "roster report drops them"
    )


# ---- the cross-chapter line ----


async def test_a_dual_chapter_member_is_counted_per_chapter(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """A member of two chapters must not carry the other chapter's attendance in.

    This is the test the endpoint exists around. Joining attendance to the user and
    then LEFT JOINing out to `meetings ON chapter_id = :chapter_id` reads correct and
    is not: on a LEFT JOIN the meetings side goes NULL for the other chapter's rows
    while `status` stays non-null, so a COUNT(...) FILTER (WHERE status = 'present')
    counts them anyway. Every single-chapter test in this file still passes with that
    bug in place - only a member who belongs to two chapters can see it.
    """
    home = await make_chapter_with("secretary")
    other = await make_chapter_with("secretary")
    shared = await make_user("Dual Member")
    await _join(client, home, shared)
    await _join(client, other, shared)

    home_meeting = await _create_meeting(client, home, "Home week 1", SPRING)
    await _mark(client, home, home_meeting, [(shared.id, "present")])
    for index in range(2):
        away = await _create_meeting(
            client, other, f"Other week {index}", SPRING + timedelta(days=index)
        )
        await _mark(client, other, away, [(shared.id, "present")])

    payload = await _summary(client, home)

    assert payload["meetings_in_window"] == 1, "only the home chapter's meetings count"
    row = _member(payload, shared.id)
    assert row["present"] == 1, "the other chapter's two meetings leaked into this total"
    assert row["recorded"] == 1


# ---- the window ----


async def test_the_window_bounds_both_the_counts_and_the_denominator(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("secretary")
    fall = await _create_meeting(
        client, setup, "Last fall", datetime(2025, 11, 4, 18, 0, tzinfo=timezone.utc)
    )
    spring = await _create_meeting(client, setup, "This spring", SPRING)
    await _mark(client, setup, fall, [(setup.president.id, "absent")])
    await _mark(client, setup, spring, [(setup.president.id, "present")])

    payload = await _summary(
        client,
        setup,
        start="2026-01-01T00:00:00+00:00",
        end="2026-06-01T00:00:00+00:00",
    )

    assert payload["meetings_in_window"] == 1
    row = _member(payload, setup.president.id)
    assert (row["present"], row["absent"]) == (1, 0), "last fall's absence is outside the window"


async def test_the_window_bounds_are_inclusive(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """A meeting exactly on the boundary is inside it - the alternative is a meeting
    that silently belongs to neither semester when a secretary queries them back to back."""
    setup = await make_chapter_with("secretary")
    meeting_id = await _create_meeting(client, setup, "Boundary", SPRING)
    await _mark(client, setup, meeting_id, [(setup.president.id, "present")])

    payload = await _summary(
        client, setup, start=SPRING.isoformat(), end=SPRING.isoformat()
    )

    assert payload["meetings_in_window"] == 1
    assert _member(payload, setup.president.id)["present"] == 1


async def test_an_inverted_window_is_refused(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """start after end returns 422 rather than an empty report that looks like "no meetings"."""
    setup = await make_chapter_with("secretary")
    response = await client.get(
        f"/chapters/{setup.chapter_id}/meetings/attendance-summary",
        params={"start": "2026-06-01T00:00:00+00:00", "end": "2026-01-01T00:00:00+00:00"},
        headers=setup.member.headers,
    )
    assert response.status_code == 422, response.text


async def test_the_summary_path_is_not_swallowed_by_the_meeting_id_route(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Route ORDER, not route existence: declared after GET /meetings/{meeting_id},
    "attendance-summary" is parsed as a uuid path param and every call 422s. A 200
    here proves the declaration order in meetings.py still holds."""
    setup = await make_chapter_with("secretary")
    response = await client.get(
        f"/chapters/{setup.chapter_id}/meetings/attendance-summary",
        headers=setup.member.headers,
    )
    assert response.status_code == 200, response.text
    assert "members" in response.json()


# ---- authorization ----


async def test_president_can_read_the_summary(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("president")
    await _create_meeting(client, setup)
    response = await client.get(
        f"/chapters/{setup.chapter_id}/meetings/attendance-summary",
        headers=setup.president.headers,
    )
    assert response.status_code == 200, response.text


async def test_plain_member_cannot_read_the_summary(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Same gate as get_attendance and export_meetings_csv: this is per-member presence
    data in aggregate, so it cannot be looser than the export that already ships it."""
    setup = await make_chapter_with("member")
    attempt = await client.get(
        f"/chapters/{setup.chapter_id}/meetings/attendance-summary",
        headers=setup.member.headers,
    )
    assert attempt.status_code == 403, attempt.text


async def test_treasurer_cannot_read_the_summary(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """MINUTES_ADMIN is secretary-or-president, not the whole e-board."""
    setup = await make_chapter_with("treasurer")
    attempt = await client.get(
        f"/chapters/{setup.chapter_id}/meetings/attendance-summary",
        headers=setup.member.headers,
    )
    assert attempt.status_code == 403, attempt.text


async def test_a_secretary_of_another_chapter_is_refused(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Org scoping, not role: a real secretary pointing the URL at someone else's
    chapter is a non-member there and must never see that roster's attendance."""
    target = await make_chapter_with("secretary")
    outsider = await make_chapter_with("secretary")
    await _create_meeting(client, target)

    attempt = await client.get(
        f"/chapters/{target.chapter_id}/meetings/attendance-summary",
        headers=outsider.member.headers,
    )
    assert attempt.status_code == 403, attempt.text
    assert attempt.json()["detail"] == "not_a_member"
