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
