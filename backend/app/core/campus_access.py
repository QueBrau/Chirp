"""The single campus authorization gate (board c88).

EVERY campus-wide read and write goes through here. Not "should" — the whole reason
this module exists rather than an if-statement per route is that routers/feed.py and
routers/yaks.py each grew their own copy of the old check, and yaks.py then grew a
THIRD copy inline in vote_yak that the shared dependency never covered. A check that
lives in three places is a check someone forgets to add to place number four.

WHAT CHANGED AND WHY IT IS NOT A REFACTOR. The old check was:

    if user.campus_id != campus_id: raise forbidden("not_your_campus")

That asks "is this user associated with this campus". It was safe only while nothing
wrote users.campus_id. c96 made redeeming a chapter invite write it, and per c105 an
invite code is an unlimited-use bearer token with no revocation, so that question
became answerable by anyone holding a forwarded code. Jose ruled on Aug 16 that
chapter membership grants CHAPTER content only and that the campus feed and Yak both
require a real .edu. So the question changed to "has this user PROVED an address at
this campus, recently". Both halves are required, and the second is the new one.

DO NOT reintroduce a `campus_id is not None` shortcut anywhere. It reads as equivalent
and is not: it is satisfiable without an email, which is the exact bypass this closes.

TWO DISTINCT 403s, deliberately, because the app has to tell them apart:
  not_your_campus   - right question, wrong campus. Nothing the user can do here.
  campus_unverified - the user simply has not proved their .edu yet, and c90's verify
                      screen is the designed destination. Collapsing these into one
                      error is how a verifiable user gets shown a dead end.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Path

from app import models
from app.core.errors import forbidden
from app.middleware.auth import get_current_user

# Jose's ruling, Aug 16: verification is re-checked YEARLY rather than being permanent,
# so the verified population stays close to actually-enrolled instead of drifting into
# a 2035 account still reading a 2026 campus. A lapsed verification is not an error
# state - c90 lands the user on the same screen with different copy.
VERIFICATION_TTL = timedelta(days=365)


def is_campus_verified(user: models.User, *, now: datetime | None = None) -> bool:
    """True if the user holds a CURRENT .edu verification.

    Never merely "has a campus". A NULL campus_verified_at means never verified; a
    timestamp older than VERIFICATION_TTL means it lapsed and must be renewed.
    """
    if user.campus_verified_at is None:
        return False
    return user.campus_verified_at > (now or datetime.now(timezone.utc)) - VERIFICATION_TTL


def require_verified_campus(
    user: models.User, campus_id: uuid.UUID, *, now: datetime | None = None
) -> None:
    """Raise unless the user belongs to `campus_id` AND is currently verified there.

    Call this directly from any handler that reaches campus-wide content without a
    campus_id in its path — resolve the campus off the row being touched and pass it in.
    Routes that DO have the campus in the path should depend on `require_campus_member`
    instead, so the check cannot be forgotten.
    """
    if user.campus_id != campus_id:
        raise forbidden("not_your_campus")
    if not is_campus_verified(user, now=now):
        raise forbidden("campus_unverified")


async def require_campus_member(
    campus_id: uuid.UUID = Path(...),
    user: models.User = Depends(get_current_user),
) -> models.User:
    """FastAPI dependency for routes with `{campus_id}` in the path.

    Replaces the `_require_campus_user` helpers that used to live in feed.py and
    yaks.py separately.
    """
    require_verified_campus(user, campus_id)
    return user
