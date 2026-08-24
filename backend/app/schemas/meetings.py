"""Secretary schemas: meetings (minutes) and attendance."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AttendanceStatus = Literal["present", "absent", "excused"]


class _Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


# ---- meetings ----


class MeetingCreate(_Schema):
    title: str = Field(min_length=1)
    meeting_date: datetime
    minutes_md: str | None = None


class MeetingUpdate(_Schema):
    title: str | None = None
    meeting_date: datetime | None = None
    minutes_md: str | None = None


class MeetingOut(_Schema):
    id: uuid.UUID
    chapter_id: uuid.UUID
    title: str
    meeting_date: datetime
    minutes_md: str | None = None
    created_by: uuid.UUID
    created_at: datetime


# ---- attendance (PUT /chapters/{chapter_id}/meetings/{meeting_id}/attendance) ----


class MeetingAttendanceItem(_Schema):
    user_id: uuid.UUID
    status: AttendanceStatus


class MeetingAttendanceUpdate(_Schema):
    """Full attendance sheet for one meeting — PUT replaces existing records."""

    # Bounded because the route writes one row per entry (board c149): an unbounded
    # list lets the caller choose how much work the request costs. 500 is far above
    # any real chapter roster and far below a payload that could tie up a connection.
    entries: list[MeetingAttendanceItem] = Field(default_factory=list, max_length=500)


class MeetingAttendanceOut(_Schema):
    meeting_id: uuid.UUID
    user_id: uuid.UUID
    status: AttendanceStatus


# ---- meetings + their sheets in one read (GET .../meetings/with-attendance) ----


class MeetingWithAttendanceOut(_Schema):
    """One meeting and the attendance recorded against it (board c156).

    Shaped as {meeting, attendance} rather than a flattened row per (meeting, member)
    because that is the shape the secretary screen already holds in state - the client
    that used to build it with an N+1 can now assign the response straight through.
    """

    meeting: MeetingOut
    attendance: list[MeetingAttendanceOut]


# ---- per-member aggregate (GET /chapters/{chapter_id}/meetings/attendance-summary) ----


class MemberAttendanceSummary(_Schema):
    """One active member's attendance totals over the requested window (board c82)."""

    user_id: uuid.UUID
    display_name: str
    role: str
    present: int
    absent: int
    excused: int
    # present + absent + excused. Sent rather than left to the client because the
    # useful number is the one it does NOT equal: meetings_in_window - recorded is
    # how many meetings nobody marked this member either way, which reads very
    # differently from an absence and must not be shown as one.
    recorded: int


class ChapterAttendanceSummary(_Schema):
    """Roster-wide attendance totals: the "how many has this person missed" answer.

    `meetings_in_window` is the denominator and is deliberately part of the payload:
    three absences out of four meetings and three out of thirty are opposite facts,
    and a client holding only the numerator can render either one.
    """

    meetings_in_window: int
    start: datetime | None = None
    end: datetime | None = None
    members: list[MemberAttendanceSummary]
