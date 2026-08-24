"""Shared date-window filters for the chapter reads that are scoped to a term.

Lives in core rather than beside any one router because three surfaces now answer
"this semester" and they must all mean the same thing: the Secretary dashboard's
attendance totals (board c82) and its meetings+attendance bundle (c156), and the
President overview (c171). Two endpoints answering the same question with different
boundary rules is a bug nobody thinks to look for — the numbers just disagree by one
meeting, on the screens most likely to be compared side by side.
"""
import uuid
from datetime import datetime
from typing import Any

from fastapi import HTTPException

from app import models


def meeting_window(
    chapter_id: uuid.UUID, start: datetime | None, end: datetime | None
) -> list[Any]:
    """Filters selecting a chapter's meetings inside an inclusive [start, end] window.

    Both bounds inclusive: a meeting exactly on the boundary belongs to the window
    rather than falling between two semesters queried back to back.

    Omitting a bound leaves that side open, so passing neither means all time.
    """
    if start is not None and end is not None and start > end:
        raise HTTPException(status_code=422, detail="invalid_window")
    window: list[Any] = [models.Meeting.chapter_id == chapter_id]
    if start is not None:
        window.append(models.Meeting.meeting_date >= start)
    if end is not None:
        window.append(models.Meeting.meeting_date <= end)
    return window
