"""Attendance sheets cannot GROW past MAX_ROSTER_PAGE distinct users (board c359).

GET .../attendance's docstring has always claimed this read cannot exceed one page
because the write side is capped - but c264 only capped a SINGLE PUT's entry count,
and c151 only requires a NEW entry to name an active member. Rows survive a
membership going inactive (c258), so nothing actually stopped a meeting from
accumulating more than 500 distinct attendance rows over a long chapter history, and
the bundle route's own docstring already said as much.

The refined invariant this card enforces is narrower than "the sheet is capped": a
batch that only touches rows ALREADY on the sheet must always succeed, even on a
meeting that (through legacy data predating this check) already exceeds the cap. Only
a batch that would add a NEW distinct user past the cap is refused. Both halves are
tested on both a fresh exactly-500 sheet and an already-over-cap legacy sheet, so the
"cannot grow" rule is proven distinct from a "frozen once big" rule.

500 users are seeded by bulk INSERT, not through real signups or real PUT calls:
building the boundary condition through the write path being tested would make the
test's own setup exercise (and potentially trip) the very check it verifies.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from httpx import AsyncClient
from sqlalchemy import insert, select

from app import models
from app.core.pagination import MAX_ROSTER_PAGE
from app.db import get_session_factory
from tests.conftest import MakeChapterWith, MakeUser


async def _create_meeting(client: AsyncClient, setup, title: str) -> str:
    created = await client.post(
        f"/chapters/{setup.chapter_id}/meetings",
        json={"title": title, "meeting_date": "2026-09-01T19:00:00Z"},
        headers=setup.president.headers,
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


async def _seed_attendance(chapter_id: str, meeting_id: str, count: int) -> list[uuid.UUID]:
    """Bulk-insert `count` distinct ACTIVE members of this chapter, each with one
    'present' row on the meeting. Real memberships, not just users: c151 requires
    every PUT entry - existing or new - to name a currently-active member, so a pure
    update of an already-seeded row (this file's whole point) needs one too."""
    user_ids = [uuid.uuid4() for _ in range(count)]
    async with get_session_factory()() as session:
        await session.execute(insert(models.User), [
            {
                "id": user_id,
                "firebase_uid": str(user_id),
                "email": f"{user_id}@example.edu",
                "display_name": "Seeded Member",
                "account_type": "greek",
            }
            for user_id in user_ids
        ])
        await session.execute(insert(models.Membership), [
            {
                "id": uuid.uuid4(),
                "user_id": user_id,
                "chapter_id": uuid.UUID(chapter_id),
                "role": "member",
                "status": "active",
            }
            for user_id in user_ids
        ])
        await session.execute(insert(models.MeetingAttendance), [
            {"meeting_id": uuid.UUID(meeting_id), "user_id": user_id, "status": "present"}
            for user_id in user_ids
        ])
        await session.commit()
    return user_ids


async def _real_active_member(client: AsyncClient, setup, make_user: MakeUser) -> str:
    """A genuinely active member of this chapter, distinct from the bulk-seeded rows -
    upsert_attendance's active-membership check (c151) must pass for it."""
    extra = await make_user("Newly Joined Member")
    invite = await client.post(
        f"/chapters/{setup.chapter_id}/invites", json={"role": "member"},
        headers=setup.president.headers,
    )
    assert invite.status_code == 201, invite.text
    joined = await client.post(
        "/chapters/join", json={"code": invite.json()["code"]}, headers=extra.headers,
    )
    assert joined.status_code == 201, joined.text
    return extra.id


async def _attendance_row_count(meeting_id: str) -> int:
    async with get_session_factory()() as session:
        result = await session.execute(
            select(models.MeetingAttendance).where(
                models.MeetingAttendance.meeting_id == uuid.UUID(meeting_id)
            )
        )
        return len(result.scalars().all())


async def test_exactly_500_sheet_refuses_a_new_distinct_user_but_allows_pure_updates(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    setup = await make_chapter_with("member")
    meeting_id = await _create_meeting(client, setup, "Exactly 500")
    seeded = await _seed_attendance(setup.chapter_id, meeting_id, MAX_ROSTER_PAGE)
    assert await _attendance_row_count(meeting_id) == MAX_ROSTER_PAGE, (
        "the exact-limit boundary must genuinely be at the cap before either write below"
    )

    # A pure update of two already-seeded rows: new_distinct is empty, so this must
    # succeed even though the sheet is already exactly at the cap.
    touch_a, touch_b = str(seeded[0]), str(seeded[1])
    updated = await client.put(
        f"/chapters/{setup.chapter_id}/meetings/{meeting_id}/attendance",
        json={"entries": [
            {"user_id": touch_a, "status": "absent"},
            {"user_id": touch_b, "status": "excused"},
        ]},
        headers=setup.president.headers,
    )
    assert updated.status_code == 200, updated.text
    assert len(updated.json()) == MAX_ROSTER_PAGE, "a pure update must not change the row count"
    by_user = {row["user_id"]: row["status"] for row in updated.json()}
    assert by_user[touch_a] == "absent" and by_user[touch_b] == "excused"

    # A NEW distinct, genuinely-active member pushes total distinct past the cap.
    new_member_id = await _real_active_member(client, setup, make_user)
    refused = await client.put(
        f"/chapters/{setup.chapter_id}/meetings/{meeting_id}/attendance",
        json={"entries": [{"user_id": new_member_id, "status": "present"}]},
        headers=setup.president.headers,
    )
    assert refused.status_code == 422, refused.text
    assert refused.json() == {"detail": "attendance_sheet_full"}
    assert await _attendance_row_count(meeting_id) == MAX_ROSTER_PAGE, (
        "the refused write must not have partially applied"
    )


async def test_a_legacy_meeting_already_over_the_cap_allows_updates_but_refuses_growth(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """The rule is 'the sheet cannot GROW past the cap', never 'a big sheet is frozen' -
    a meeting one row over the cap (only reachable today via legacy data predating this
    check, since the check itself would have refused reaching it going forward) must
    still accept an update of an existing row."""
    setup = await make_chapter_with("member")
    meeting_id = await _create_meeting(client, setup, "Legacy Over Cap")
    seeded = await _seed_attendance(setup.chapter_id, meeting_id, MAX_ROSTER_PAGE + 1)
    assert await _attendance_row_count(meeting_id) == MAX_ROSTER_PAGE + 1

    existing_user = str(seeded[0])
    updated = await client.put(
        f"/chapters/{setup.chapter_id}/meetings/{meeting_id}/attendance",
        json={"entries": [{"user_id": existing_user, "status": "absent"}]},
        headers=setup.president.headers,
    )
    assert updated.status_code == 200, updated.text
    assert len(updated.json()) == MAX_ROSTER_PAGE + 1, (
        "an already-over-cap sheet must not be frozen against updates of its own rows"
    )

    new_member_id = await _real_active_member(client, setup, make_user)
    refused = await client.put(
        f"/chapters/{setup.chapter_id}/meetings/{meeting_id}/attendance",
        json={"entries": [{"user_id": new_member_id, "status": "present"}]},
        headers=setup.president.headers,
    )
    assert refused.status_code == 422, refused.text
    assert refused.json() == {"detail": "attendance_sheet_full"}
    assert await _attendance_row_count(meeting_id) == MAX_ROSTER_PAGE + 1, (
        "growth must still be refused even though the sheet was already over the cap"
    )


async def test_bundle_attendance_rows_match_get_at_the_same_bound(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """GET .../attendance and the meetings+attendance bundle must agree on one
    meeting's row count - closing the three-copies-of-the-same-read inconsistency
    the card names."""
    setup = await make_chapter_with("member")
    meeting_id = await _create_meeting(client, setup, "Bundle Consistency")
    await _seed_attendance(setup.chapter_id, meeting_id, MAX_ROSTER_PAGE)

    direct = await client.get(
        f"/chapters/{setup.chapter_id}/meetings/{meeting_id}/attendance",
        headers=setup.president.headers,
    )
    assert direct.status_code == 200, direct.text

    bundle = await client.get(
        f"/chapters/{setup.chapter_id}/meetings/with-attendance",
        params={"limit": 1}, headers=setup.president.headers,
    )
    assert bundle.status_code == 200, bundle.text
    bundle_rows = bundle.json()
    assert len(bundle_rows) == 1
    assert bundle_rows[0]["meeting"]["id"] == meeting_id
    assert len(bundle_rows[0]["attendance"]) == len(direct.json()) == MAX_ROSTER_PAGE
