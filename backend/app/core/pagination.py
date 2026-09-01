"""Ceilings for list responses that are bounded by construction (board card c258).

Every list endpoint here returns rows whose count is limited by something real - a
chapter's roster, one member's role history, one meeting's sheet - rather than by
time. They get a generous hard cap instead of a cursor, deliberately: cursoring a
list that cannot realistically reach its ceiling adds a paging protocol, a client
change and a class of boundary bugs to buy nothing. Endpoints that grow with TIME
(comments, ledger, reports, meetings) are the opposite case and take real cursors.

THE CAP MUST NEVER TRUNCATE SILENTLY. That is the failure mode a bare LIMIT
introduces: the response looks complete, the client renders it as complete, and a
chapter that outgrew the ceiling shows a short roster with nothing anywhere saying
so. `warn_if_capped` is what makes it observable - if one of these ever fires in
prod, that endpoint has outgrown this file and needs a cursor, and the log is the
evidence that decides it rather than a guess.
"""

from __future__ import annotations

import logging
from collections.abc import Sized

# Roster-bounded: a chapter's members, and anything keyed one row per member.
#
# The SAME 500 c264 put on the write side (MeetingAttendanceUpdate.entries,
# EventInviteCreate.user_ids), on purpose. A read that cannot return what a write was
# allowed to store would be its own bug, so the two ceilings are one number.
MAX_ROSTER_PAGE = 500

# Slow-growing per-chapter or per-member history: role terms, invite codes. Generous
# for anything a human body actually produces, and small enough to bound the response.
MAX_HISTORY_PAGE = 200


def warn_if_capped(
    logger: logging.Logger, rows: Sized, cap: int, endpoint: str, **context: object
) -> None:
    """Log a warning when a capped list came back exactly full.

    A full page means the result was PROBABLY truncated - it can also mean the data
    happens to be exactly `cap` rows, which is why this warns rather than raises. Either
    way it is the signal that this endpoint has outgrown a cap-only treatment.
    """
    if len(rows) >= cap:
        logger.warning(
            "list response hit its cap and may be truncated: %s returned %d rows "
            "(cap %d). This endpoint needs a cursor if it keeps happening. context=%r",
            endpoint,
            len(rows),
            cap,
            context,
        )
