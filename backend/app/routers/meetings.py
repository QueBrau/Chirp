"""Secretary: meetings CRUD (minutes) and bulk attendance upsert."""
import uuid

from fastapi import APIRouter, Depends, Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.core.csv_export import csv_response, sanitize_csv_text
from app.core.errors import not_found
from app.core.permissions import MINUTES_ADMIN, Role, require_role
from app.db import get_session
from app.middleware.org_scope import get_current_membership
from app.schemas.meetings import (
    MeetingAttendanceOut,
    MeetingAttendanceUpdate,
    MeetingCreate,
    MeetingOut,
    MeetingUpdate,
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


@router.put("/chapters/{chapter_id}/meetings/{meeting_id}/attendance")
async def upsert_attendance(
    chapter_id: uuid.UUID,
    meeting_id: uuid.UUID,
    body: MeetingAttendanceUpdate,
    _membership: models.Membership = Depends(require_role(*MINUTES_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> list[MeetingAttendanceOut]:
    """Bulk-upsert attendance statuses for a meeting; secretary/president only."""
    meeting = await _get_chapter_meeting(session, chapter_id, meeting_id)
    for entry in body.entries:
        existing = await session.get(
            models.MeetingAttendance, (meeting.id, entry.user_id)
        )
        if existing is not None:
            existing.status = entry.status
        else:
            session.add(
                models.MeetingAttendance(
                    meeting_id=meeting.id, user_id=entry.user_id, status=entry.status
                )
            )
    await session.commit()
    result = await session.execute(
        select(models.MeetingAttendance)
        .where(models.MeetingAttendance.meeting_id == meeting.id)
        .order_by(models.MeetingAttendance.user_id)
    )
    return [MeetingAttendanceOut.model_validate(a) for a in result.scalars().all()]
