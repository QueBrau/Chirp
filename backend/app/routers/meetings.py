"""Secretary: meetings CRUD (minutes), bulk attendance upsert, roster attendance totals."""
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import and_, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.core.csv_export import csv_response, sanitize_csv_text
from app.core.errors import not_found
from app.core.permissions import MINUTES_ADMIN, require_role
from app.core.windows import meeting_window
from app.db import get_session
from app.middleware.org_scope import get_current_membership
from app.schemas.meetings import (
    ChapterAttendanceSummary,
    MeetingAttendanceOut,
    MeetingAttendanceUpdate,
    MeetingCreate,
    MeetingOut,
    MeetingUpdate,
    MeetingWithAttendanceOut,
    MemberAttendanceSummary,
)

router = APIRouter(tags=["meetings"])


async def _get_chapter_meeting(
    session: AsyncSession, chapter_id: uuid.UUID, meeting_id: uuid.UUID
) -> models.Meeting:
    """Load a meeting scoped to the path's chapter, or raise 404."""
    meeting = await session.get(models.Meeting, meeting_id)
    if meeting is None or meeting.chapter_id != chapter_id:
        raise not_found("meeting_not_found")
    return meeting


@router.get("/chapters/{chapter_id}/meetings")
async def list_meetings(
    chapter_id: uuid.UUID,
    _membership: models.Membership = Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
) -> list[MeetingOut]:
    """List the chapter's meetings, most recent meeting date first; any member."""
    result = await session.execute(
        select(models.Meeting)
        .where(models.Meeting.chapter_id == chapter_id)
        .order_by(models.Meeting.meeting_date.desc())
    )
    return [MeetingOut.model_validate(m) for m in result.scalars().all()]


@router.get("/chapters/{chapter_id}/meetings/export.csv")
async def export_meetings_csv(
    chapter_id: uuid.UUID,
    _membership: models.Membership = Depends(require_role(*MINUTES_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Export chapter attendance as CSV, one row per (meeting, member); secretary/president only.

    Long format, not a wide meeting-by-member matrix: it pivots cleanly in a
    spreadsheet and keeps working as members are added later.
    """
    result = await session.execute(
        select(
            models.Meeting.meeting_date,
            models.Meeting.title,
            models.User.display_name,
            models.MeetingAttendance.status,
        )
        .join(
            models.MeetingAttendance,
            models.MeetingAttendance.meeting_id == models.Meeting.id,
        )
        .join(models.User, models.User.id == models.MeetingAttendance.user_id)
        .where(models.Meeting.chapter_id == chapter_id)
        .order_by(models.Meeting.meeting_date, models.User.display_name)
    )
    header = ["meeting_date", "meeting_title", "member", "status"]
    rows = [
        [
            meeting_date.isoformat(),
            sanitize_csv_text(title),
            sanitize_csv_text(display_name),
            status,
        ]
        for meeting_date, title, display_name, status in result.all()
    ]
    return csv_response(f"meetings_{chapter_id}.csv", header, rows)


@router.get("/chapters/{chapter_id}/meetings/with-attendance")
async def list_meetings_with_attendance(
    chapter_id: uuid.UUID,
    start: datetime | None = None,
    end: datetime | None = None,
    _membership: models.Membership = Depends(require_role(*MINUTES_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> list[MeetingWithAttendanceOut]:
    """Every meeting in the window with its attendance sheet, in ONE read (board c156).

    The secretary dashboard used to build this client-side: list_meetings, then
    get_attendance once per meeting inside a Promise.all. A semester of meetings was a
    semester of requests on every dashboard load, and it grew with chapter history
    rather than with anything the secretary asked for. c82's aggregate deliberately did
    not fix this - the per-meeting present/absent/excused chips need per-meeting rows,
    not totals - so this is the other half.

    ONE query, not one per meeting: a LEFT JOIN from meetings to their attendance rows,
    grouped in Python. The join direction is what makes it safe - the spine is this
    chapter's meetings, so attendance can only arrive through a meeting that already
    passed the chapter filter. That is the opposite situation from attendance_summary
    below, where the spine is members and a careless join CAN pull in another chapter's
    rows; worth stating so nobody "makes them consistent" in the wrong direction.

    LEFT, not inner: a meeting with no sheet taken yet must come back with an empty
    list. An inner join would silently drop it, and a meeting that vanishes from the
    dashboard is worse than one showing zero attendance - the secretary would think
    they never created it.

    Declared above the /{meeting_id} routes, same constraint as export.csv and
    attendance-summary: below them, "with-attendance" parses as meeting_id: uuid.UUID.

    Gated MINUTES_ADMIN, matching get_attendance - this returns exactly what that route
    returns, for several meetings at once, so it cannot be gated more loosely.
    """
    window = meeting_window(chapter_id, start, end)
    result = await session.execute(
        select(models.Meeting, models.MeetingAttendance)
        .outerjoin(
            models.MeetingAttendance,
            models.MeetingAttendance.meeting_id == models.Meeting.id,
        )
        .where(*window)
        # Most recent meeting first, matching list_meetings so the two reads cannot
        # disagree about order. id breaks ties: two meetings can share a date, and
        # without it their relative order changes between calls.
        .order_by(
            models.Meeting.meeting_date.desc(),
            models.Meeting.id,
            models.MeetingAttendance.user_id,
        )
    )

    bundles: dict[uuid.UUID, MeetingWithAttendanceOut] = {}
    for meeting, attendance in result.all():
        bundle = bundles.get(meeting.id)
        if bundle is None:
            bundle = MeetingWithAttendanceOut(
                meeting=MeetingOut.model_validate(meeting), attendance=[]
            )
            bundles[meeting.id] = bundle
        if attendance is not None:
            bundle.attendance.append(MeetingAttendanceOut.model_validate(attendance))
    # dicts preserve insertion order, so this is still the ORDER BY above.
    return list(bundles.values())


@router.get("/chapters/{chapter_id}/meetings/attendance-summary")
async def attendance_summary(
    chapter_id: uuid.UUID,
    start: datetime | None = None,
    end: datetime | None = None,
    _membership: models.Membership = Depends(require_role(*MINUTES_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> ChapterAttendanceSummary:
    """Per-member attendance totals for the chapter over an optional window (board c82).

    The question a secretary is actually asked is "how many has this person missed
    this semester", and until now the only way to answer it was to open every meeting
    in turn - listMeetings plus getAttendance per meeting, an N+1 over a semester of
    chapter meetings that the client would have had to aggregate itself.

    DECLARED ABOVE THE /{meeting_id} ROUTES ON PURPOSE. FastAPI matches in declaration
    order, so below them "attendance-summary" is parsed as meeting_id: uuid.UUID and
    every call 422s. export_meetings_csv sits above them for the same reason.

    Gated on MINUTES_ADMIN, matching get_attendance and export_meetings_csv rather than
    list_meetings: this is per-member presence data, and export_meetings_csv already
    ships exactly this data one row per (meeting, member), so an aggregate over it
    cannot be less sensitive than the export.

    THE JOIN IS THE WHOLE CORRECTNESS ARGUMENT. Attendance is joined against meeting ids
    already filtered to this chapter and window, NOT joined out to `meetings` afterwards.
    The natural-looking version - LEFT JOIN attendance ON user, LEFT JOIN meetings ON
    attendance.meeting_id AND meetings.chapter_id = :chapter_id - silently counts a
    dual-chapter member's attendance from their OTHER chapter: on a LEFT JOIN the
    meetings side goes NULL while `status` stays non-null, so every FILTER below still
    counts the row. That is a cross-chapter disclosure that passes every single-chapter
    test; test_attendance_summary.py builds the two-chapter member to hold this line.

    Two statements, not one, and neither is per-member: the totals below, and a COUNT of
    meetings in the window. Folding the count in as a subquery would tie the denominator
    to there being at least one active member, and a roster-spined query returns no rows
    when the roster is empty - the count has to survive that.

    THE ROSTER IS WHO IS ACTIVE NOW, DELIBERATELY. A member who goes inactive mid-window
    disappears from this report while their rows stay inside the window and keep coming
    out of export.csv, which is spined on attendance rather than on membership. That is
    the intended split: this endpoint answers a forward-looking question - who currently
    owes a fine, who is at risk of losing good standing - and the CSV is the historical
    record. test_attendance_summary.py asserts both halves so neither can flip alone.
    """
    window = meeting_window(chapter_id, start, end)

    meetings_in_window = await session.scalar(
        select(func.count()).select_from(models.Meeting).where(*window)
    )

    # Every ACTIVE member is a row, whether or not they were ever marked: the member
    # nobody has recorded is precisely the one a secretary needs to see, and an
    # attendance-spined query would drop them.
    windowed_meeting_ids = select(models.Meeting.id).where(*window)
    counted = func.count(models.MeetingAttendance.user_id)
    result = await session.execute(
        select(
            models.User.id,
            models.User.display_name,
            models.Membership.role,
            counted.filter(models.MeetingAttendance.status == "present").label("present"),
            counted.filter(models.MeetingAttendance.status == "absent").label("absent"),
            counted.filter(models.MeetingAttendance.status == "excused").label("excused"),
            counted.label("recorded"),
        )
        .select_from(models.Membership)
        .join(models.User, models.User.id == models.Membership.user_id)
        .outerjoin(
            models.MeetingAttendance,
            and_(
                models.MeetingAttendance.user_id == models.Membership.user_id,
                models.MeetingAttendance.meeting_id.in_(windowed_meeting_ids),
            ),
        )
        .where(
            models.Membership.chapter_id == chapter_id,
            models.Membership.status == "active",
        )
        .group_by(models.User.id, models.User.display_name, models.Membership.role)
        # display_name is not unique, so it cannot order this on its own without
        # leaving two same-named members in an order that changes between calls.
        .order_by(models.User.display_name, models.User.id)
    )

    return ChapterAttendanceSummary(
        meetings_in_window=meetings_in_window or 0,
        start=start,
        end=end,
        members=[
            MemberAttendanceSummary(
                user_id=user_id,
                display_name=display_name,
                role=role,
                present=present,
                absent=absent,
                excused=excused,
                recorded=recorded,
            )
            for user_id, display_name, role, present, absent, excused, recorded in result.all()
        ],
    )


@router.post("/chapters/{chapter_id}/meetings", status_code=201)
async def create_meeting(
    chapter_id: uuid.UUID,
    body: MeetingCreate,
    membership: models.Membership = Depends(require_role(*MINUTES_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> MeetingOut:
    """Create a meeting; secretary/president only."""
    meeting = models.Meeting(
        chapter_id=chapter_id,
        title=body.title,
        meeting_date=body.meeting_date,
        minutes_md=body.minutes_md,
        created_by=membership.user_id,
    )
    session.add(meeting)
    await session.commit()
    await session.refresh(meeting)
    return MeetingOut.model_validate(meeting)


@router.get("/chapters/{chapter_id}/meetings/{meeting_id}")
async def get_meeting(
    chapter_id: uuid.UUID,
    meeting_id: uuid.UUID,
    _membership: models.Membership = Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
) -> MeetingOut:
    """Read one meeting (title, date, minutes); any member."""
    meeting = await _get_chapter_meeting(session, chapter_id, meeting_id)
    return MeetingOut.model_validate(meeting)


@router.patch("/chapters/{chapter_id}/meetings/{meeting_id}")
async def update_meeting(
    chapter_id: uuid.UUID,
    meeting_id: uuid.UUID,
    body: MeetingUpdate,
    _membership: models.Membership = Depends(require_role(*MINUTES_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> MeetingOut:
    """Update title/date/minutes; secretary/president only."""
    meeting = await _get_chapter_meeting(session, chapter_id, meeting_id)
    if body.title is not None:
        meeting.title = body.title
    if body.meeting_date is not None:
        meeting.meeting_date = body.meeting_date
    if body.minutes_md is not None:
        meeting.minutes_md = body.minutes_md
    await session.commit()
    return MeetingOut.model_validate(meeting)


@router.delete("/chapters/{chapter_id}/meetings/{meeting_id}", status_code=204)
async def delete_meeting(
    chapter_id: uuid.UUID,
    meeting_id: uuid.UUID,
    _membership: models.Membership = Depends(require_role(*MINUTES_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete a meeting and its attendance records; secretary/president only."""
    meeting = await _get_chapter_meeting(session, chapter_id, meeting_id)
    await session.execute(
        delete(models.MeetingAttendance).where(
            models.MeetingAttendance.meeting_id == meeting.id
        )
    )
    await session.delete(meeting)
    await session.commit()


@router.get("/chapters/{chapter_id}/meetings/{meeting_id}/attendance")
async def get_attendance(
    chapter_id: uuid.UUID,
    meeting_id: uuid.UUID,
    _membership: models.Membership = Depends(require_role(*MINUTES_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> list[MeetingAttendanceOut]:
    """Read a meeting's attendance sheet; secretary/president only (board card c124).

    Found by a static contract check, not by driving the UI - same class of gap as
    c77's chapter PATCH, this time the client function (getAttendance, meetings.ts:73)
    and its one call site existed, and every call has been a silent 405 since the
    screen was written, because the backend only ever registered the PUT.

    Gated on MINUTES_ADMIN, not plain membership, DESPITE list_meetings (above) being
    member-readable - checked this rather than assumed it should mirror the write.
    Attendance is per-member presence data, one tier more sensitive than "a meeting
    exists," and export_meetings_csv - which already exports this exact data, one row
    per (meeting, member) - is ALREADY MINUTES_ADMIN-only. This route matches that
    existing, deliberate precedent, not just the PUT it sits next to.
    """
    meeting = await _get_chapter_meeting(session, chapter_id, meeting_id)
    result = await session.execute(
        select(models.MeetingAttendance)
        .where(models.MeetingAttendance.meeting_id == meeting.id)
        .order_by(models.MeetingAttendance.user_id)
    )
    return [MeetingAttendanceOut.model_validate(a) for a in result.scalars().all()]


@router.put("/chapters/{chapter_id}/meetings/{meeting_id}/attendance")
async def upsert_attendance(
    chapter_id: uuid.UUID,
    meeting_id: uuid.UUID,
    body: MeetingAttendanceUpdate,
    _membership: models.Membership = Depends(require_role(*MINUTES_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> list[MeetingAttendanceOut]:
    """Bulk-upsert attendance statuses for a meeting; secretary/president only.

    Every entry must name an active member of the chapter, or the whole sheet is
    refused with a 422 naming the offending ids (board c151) - attendance is a record
    officers act on, so "this person attended" must not be assertable about someone
    who is not in the chapter.
    """
    meeting = await _get_chapter_meeting(session, chapter_id, meeting_id)

    # ONE statement, not one per entry (board c149). This used to `session.get` each
    # (meeting_id, user_id) in a loop and branch on the result, so marking an
    # 80-member roster cost 80 sequential round trips before the commit - latency that
    # scales with chapter size, on the screen a secretary uses during a meeting.
    #
    # ON CONFLICT DO UPDATE also removes the read-then-branch entirely: the database
    # decides insert-vs-update atomically, so two secretaries submitting the same sheet
    # at once cannot race between the SELECT and the INSERT the way the old shape
    # could. Same reasoning as c51, c105, c91 and c114 - let the constraint arbitrate.
    if body.entries:
        # Last write wins within a single payload: a sheet listing the same member
        # twice is the client's bug, but ON CONFLICT cannot see a row twice in one
        # statement, so collapse duplicates here rather than fail the whole request.
        collapsed = {entry.user_id: entry.status for entry in body.entries}

        # Every entry must name an ACTIVE member of THIS chapter (board c151).
        # _get_chapter_meeting above scopes the MEETING to the chapter, but nothing
        # scoped the ENTRIES, so any user_id at all got a row. One SELECT closes both
        # halves of that: a real-but-non-member id used to insert silently, fabricating
        # attendance in the record that export_meetings_csv hands officers for dues and
        # good-standing calls, and an id belonging to nobody reached the users FK and
        # 500'd the entire sheet at commit instead of being refused.
        #
        # ONE statement for the whole sheet, deliberately not one per entry: c149
        # rewrote this route to stop paying a round trip per member, and validating in
        # a loop would hand that cost straight back. Index-backed by
        # idx_memberships_chapter, which is partial on status = 'active'.
        requested_ids = sorted(collapsed)
        result = await session.execute(
            select(models.Membership.user_id).where(
                models.Membership.chapter_id == chapter_id,
                models.Membership.status == "active",
                models.Membership.user_id.in_(requested_ids),
            )
        )
        active_member_ids = set(result.scalars().all())
        not_members = [uid for uid in requested_ids if uid not in active_member_ids]
        if not_members:
            # `detail` stays a STRING. The mobile client only surfaces detail when it
            # is one (parseResponse, api/client.ts) and falls back to statusText
            # otherwise, so a dict here would show the secretary "Unprocessable Entity"
            # and hide the very ids this error exists to name.
            raise HTTPException(
                status_code=422,
                detail="not_chapter_members: "
                + ", ".join(str(uid) for uid in not_members),
            )

        await session.execute(
            pg_insert(models.MeetingAttendance)
            .values(
                [
                    {"meeting_id": meeting.id, "user_id": user_id, "status": status}
                    for user_id, status in collapsed.items()
                ]
            )
            .on_conflict_do_update(
                index_elements=["meeting_id", "user_id"],
                set_={"status": pg_insert(models.MeetingAttendance).excluded.status},
            )
        )
    await session.commit()
    result = await session.execute(
        select(models.MeetingAttendance)
        .where(models.MeetingAttendance.meeting_id == meeting.id)
        .order_by(models.MeetingAttendance.user_id)
    )
    return [MeetingAttendanceOut.model_validate(a) for a in result.scalars().all()]
