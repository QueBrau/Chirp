"""Shared date-window filters for the chapter reads that are scoped to a term.

Lives in core rather than beside any one router because three surfaces now answer
"this semester" and they must all mean the same thing: the Secretary dashboard's
attendance totals (board c82) and its meetings+attendance bundle (c156), and the
President overview (c171). Two endpoints answering the same question with different
boundary rules is a bug nobody thinks to look for — the numbers just disagree by one
meeting, on the screens most likely to be compared side by side.
"""
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
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


# ---- voting weeks and academic terms (board card c175) ----


def current_week_start(now: datetime | None = None) -> date:
    """The Monday (UTC) of the week `now` falls in.

    THE SERVER OWNS THIS VALUE. It is deliberately a function of the clock rather than
    anything a request can carry: if a client could name the week, anyone could wait to
    see how a week finished and then stuff ballots into it, and the stored rows would
    look identical to honest ones.

    UTC rather than campus-local, and that is a real simplification worth stating: a
    student voting late on Sunday evening in Pacific time is voting in Monday's week.
    Fixing it properly needs a timezone on `campuses`, which does not exist yet. UTC at
    least makes every campus agree with the server about which week it is, which a naive
    server-local implementation would not.
    """
    today = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
    return today - timedelta(days=today.weekday())


@dataclass(frozen=True)
class Term:
    """An academic term: a display label plus its inclusive date bounds."""

    label: str
    start: date
    end: date


def current_term(now: datetime | None = None) -> Term:
    """The academic term `now` falls in - "Fall 26", "Spring 27".

    Fall is August through December, spring is January through July, MATCHING
    app-mobile/src/org/semester.ts exactly. A chapter calendar does not exist in the
    schema, so this is a convention rather than a fact, and the important property is
    not that it is the right convention but that both sides use the SAME one: the
    Secretary dashboard's "this semester" and the "Touse of Fall 26" title must not
    disagree about which weeks belong to a term.

    The label is what gets engraved on the semester title, so it uses the two-digit
    year the product copy uses ("Touse of Fall 26"), not the four-digit one.
    """
    today = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
    is_fall = today.month >= 8
    year = today.year
    if is_fall:
        return Term(f"Fall {year % 100:02d}", date(year, 8, 1), date(year, 12, 31))
    return Term(f"Spring {year % 100:02d}", date(year, 1, 1), date(year, 7, 31))
