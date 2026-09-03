"""c258: meetings and polls are cursored, and attendance sheets stay WHOLE.

Both lists grow with TIME - a chapter meets weekly and may poll at every meeting - so
they take real cursors rather than caps, like comments and the ledger before them.

THE DIVERGENCE FROM THE EVENTS FAMILY IS DELIBERATE and this file is where it is proven.
c280 gave events a bounded `going_preview` plus counts, because a public event's RSVP
list is genuinely unbounded. An attendance sheet is not: c264 caps the WRITE at
MAX_ROSTER_PAGE per request and c151 requires every entry to name an ACTIVE member, so
the sheet is roster-shaped by construction. Applying the preview shape here would break
the caller outright - the secretary screen counts present/absent/excused directly off
this array, so a preview would turn those into counts over a sample.

The bound is EVERY MEMBER EVER ACTIVE AT WRITE TIME, not the current roster, and that
distinction is tested rather than asserted: rows survive a membership going inactive.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy import text as sa_text

from tests.conftest import MakeChapterWith, MakeUser

BASE = datetime(2026, 3, 1, 19, 0, 0, tzinfo=timezone.utc)


async def _make_meetings(client: AsyncClient, setup, count: int, *, tied: bool = False):
    titles = [f"meeting-{i:02d}" for i in range(count)]
    for i, title in enumerate(titles):
        when = BASE if tied else BASE + timedelta(days=i)
        created = await client.post(
            f"/chapters/{setup.chapter_id}/meetings",
            json={"title": title, "meeting_date": when.isoformat().replace("+00:00", "Z")},
            headers=setup.president.headers,
        )
        assert created.status_code == 201, created.text
    return titles


async def _walk_meetings(client: AsyncClient, setup, page: int) -> list[str]:
    collected: list[str] = []
    cursor = ""
    for _ in range(20):
        response = await client.get(
            f"/chapters/{setup.chapter_id}/meetings?limit={page}{cursor}",
            headers=setup.president.headers,
        )
        assert response.status_code == 200, response.text
        rows = response.json()
        if not rows:
            break
        collected += [r["title"] for r in rows]
        cursor = f"&before={rows[-1]['meeting_date']}&before_id={rows[-1]['id']}"
        if len(rows) < page:
            break
    return collected


async def test_meetings_page_without_gaps_or_repeats(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member")
    titles = await _make_meetings(client, setup, 11)
    assert await _walk_meetings(client, setup, 4) == list(reversed(titles))


async def test_meetings_sharing_a_date_survive_a_page_boundary(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The compound half, with REAL ties: every meeting is held at the same instant, so
    a date-only cursor drops or repeats the whole tied run at the boundary."""
    setup = await make_chapter_with("member")
    titles = await _make_meetings(client, setup, 9, tied=True)

    listed = await client.get(
        f"/chapters/{setup.chapter_id}/meetings", headers=setup.president.headers
    )
    assert len({r["meeting_date"] for r in listed.json()}) == 1, "the ties must be real"

    collected = await _walk_meetings(client, setup, 3)
    assert sorted(collected) == sorted(titles), "no meeting may be skipped or repeated"
    assert len(collected) == len(titles)


async def test_an_attendance_sheet_is_never_split_across_a_page(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """THE WRAPPER CASE. Paging the OUTER list must not truncate an inner sheet.

    A naive LIMIT over the joined (meeting x attendance) rows would cut wherever the row
    count landed, handing back a partial sheet as if it were whole. Here one meeting has
    a multi-member sheet and the page size is 1, so a joined-and-limited query would
    return a fraction of it.
    """
    setup = await make_chapter_with("member")
    extra = await make_user("Second Member")
    invite = await client.post(
        f"/chapters/{setup.chapter_id}/invites", json={"role": "member"},
        headers=setup.president.headers,
    )
    joined = await client.post(
        "/chapters/join", json={"code": invite.json()["code"]}, headers=extra.headers
    )
    assert joined.status_code == 201, joined.text

    await _make_meetings(client, setup, 2)
    meetings = (
        await client.get(
            f"/chapters/{setup.chapter_id}/meetings", headers=setup.president.headers
        )
    ).json()
    target = meetings[0]["id"]
    put = await client.put(
        f"/chapters/{setup.chapter_id}/meetings/{target}/attendance",
        json={
            "entries": [
                {"user_id": setup.president.id, "status": "present"},
                {"user_id": setup.member.id, "status": "absent"},
                {"user_id": extra.id, "status": "excused"},
            ]
        },
        headers=setup.president.headers,
    )
    assert put.status_code == 200, put.text

    page = await client.get(
        f"/chapters/{setup.chapter_id}/meetings/with-attendance?limit=1",
        headers=setup.president.headers,
    )
    assert page.status_code == 200, page.text
    bundles = page.json()
    assert len(bundles) == 1, "the page must genuinely be one meeting"
    assert len(bundles[0]["attendance"]) == 3, (
        "the sheet must come back WHOLE - a partial sheet on a full page is the "
        "truncation-as-fact bug, on attendance"
    )


async def test_the_sheet_bound_is_historical_roster_not_current(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Why cap-only is honest here, and what the cap is actually sized against.

    c151 gates NEW writes to ACTIVE members, but rows already written survive a
    membership going inactive - so the sheet is bounded by everyone who was ever active
    at write time, which is roster-shaped but can exceed today's roster. Both halves are
    asserted, because the bound argument depends on both being true.
    """
    setup = await make_chapter_with("member")
    await _make_meetings(client, setup, 1)
    meeting_id = (
        await client.get(
            f"/chapters/{setup.chapter_id}/meetings", headers=setup.president.headers
        )
    ).json()[0]["id"]
    await client.put(
        f"/chapters/{setup.chapter_id}/meetings/{meeting_id}/attendance",
        json={"entries": [{"user_id": setup.member.id, "status": "present"}]},
        headers=setup.president.headers,
    )

    from app.db import get_session_factory

    async with get_session_factory()() as session:
        await session.execute(
            sa_text(
                "UPDATE memberships SET status='inactive'"
                " WHERE user_id=:u AND chapter_id=:c"
            ),
            {"u": uuid.UUID(setup.member.id), "c": uuid.UUID(setup.chapter_id)},
        )
        await session.commit()

    still_there = await client.get(
        f"/chapters/{setup.chapter_id}/meetings/{meeting_id}/attendance",
        headers=setup.president.headers,
    )
    assert len(still_there.json()) == 1, "historical rows must survive a membership change"

    refused = await client.put(
        f"/chapters/{setup.chapter_id}/meetings/{meeting_id}/attendance",
        json={"entries": [{"user_id": setup.member.id, "status": "absent"}]},
        headers=setup.president.headers,
    )
    assert refused.status_code == 422, "c151 must still gate NEW writes to active members"


async def test_polls_page_without_gaps_or_repeats(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member")
    questions = [f"question-{i:02d}" for i in range(9)]
    for q in questions:
        created = await client.post(
            f"/chapters/{setup.chapter_id}/polls",
            json={"question": q, "options": ["yes", "no"]},
            headers=setup.president.headers,
        )
        assert created.status_code == 201, created.text

    collected: list[str] = []
    cursor = ""
    for _ in range(20):
        response = await client.get(
            f"/chapters/{setup.chapter_id}/polls?limit=4{cursor}",
            headers=setup.president.headers,
        )
        assert response.status_code == 200, response.text
        rows = response.json()
        if not rows:
            break
        collected += [r["question"] for r in rows]
        cursor = f"&before={rows[-1]['created_at']}&before_id={rows[-1]['id']}"
        if len(rows) < 4:
            break

    assert collected == list(reversed(questions))


async def test_polls_sharing_a_timestamp_survive_a_page_boundary(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The compound half for polls, with MANUFACTURED ties.

    The walk test above creates polls over HTTP, so they get distinct microsecond
    timestamps and never exercise the tie-break - removing it left that test green,
    which is the vacuous-green shape this repo keeps hitting. These rows are written
    straight to the database sharing one created_at, so a boundary falls inside the
    tied run and only the id tie-break walks it exactly once.
    """
    setup = await make_chapter_with("member")
    from app.db import get_session_factory

    tied_at = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    ids = sorted(uuid.uuid4() for _ in range(7))
    questions = [f"tied-{i}" for i in range(7)]
    async with get_session_factory()() as session:
        for poll_id, question in zip(ids, questions):
            await session.execute(
                sa_text(
                    "INSERT INTO polls (id, chapter_id, question, status, created_by,"
                    " created_at) VALUES (:id, :cid, :q, 'open', :by, :at)"
                ),
                {
                    "id": poll_id, "cid": uuid.UUID(setup.chapter_id), "q": question,
                    "by": uuid.UUID(setup.president.id), "at": tied_at,
                },
            )
            for idx, label in enumerate(("yes", "no")):
                await session.execute(
                    sa_text(
                        "INSERT INTO poll_options (id, poll_id, text, position)"
                        " VALUES (:id, :pid, :text, :pos)"
                    ),
                    {"id": uuid.uuid4(), "pid": poll_id, "text": label, "pos": idx},
                )
        await session.commit()

    listed = await client.get(
        f"/chapters/{setup.chapter_id}/polls", headers=setup.president.headers
    )
    assert listed.status_code == 200, listed.text
    assert len({r["created_at"] for r in listed.json()}) == 1, "the ties must be real"

    collected: list[str] = []
    cursor = ""
    for _ in range(20):
        response = await client.get(
            f"/chapters/{setup.chapter_id}/polls?limit=3{cursor}",
            headers=setup.president.headers,
        )
        assert response.status_code == 200, response.text
        rows = response.json()
        if not rows:
            break
        collected += [r["question"] for r in rows]
        cursor = f"&before={rows[-1]['created_at']}&before_id={rows[-1]['id']}"
        if len(rows) < 3:
            break

    assert sorted(collected) == sorted(questions), "no poll may be skipped or repeated"
    assert len(collected) == len(questions)


async def test_limits_are_bounded(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member")
    for path in ("meetings", "polls", "meetings/with-attendance"):
        for bad in (0, 5000):
            response = await client.get(
                f"/chapters/{setup.chapter_id}/{path}?limit={bad}",
                headers=setup.president.headers,
            )
            assert response.status_code == 422, f"{path} limit={bad}: {response.text}"
