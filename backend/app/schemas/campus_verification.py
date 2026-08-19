""".edu verification schemas (c86).

NOTE WHAT IS ABSENT: no campus_id on the way IN. The caller names an address and the
server resolves the campus from its domain. Accepting both would let a caller pair a
domain they control with the campus they want to read, which is c85's vulnerability
rebuilt one layer up.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class _Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class CampusVerificationStart(_Schema):
    """Body for POST /auth/campus-verification."""

    edu_email: str = Field(min_length=3, max_length=320)


class CampusVerificationRedeem(_Schema):
    """Body for POST /auth/campus-verification/redeem."""

    code: str = Field(min_length=1, max_length=16)


class PendingVerificationOut(_Schema):
    """Confirmation that a code is on its way. Carries no code and no token."""

    edu_email: str
    campus_id: uuid.UUID
    expires_at: datetime


class CampusVerificationStatus(_Schema):
    """The caller's verification state, shaped for c90's screen.

    `verified` is the answer to "may I see campus content" and already accounts for the
    yearly re-check. `verified_at` is returned regardless so the client can distinguish
    a LAPSED verification from one that never happened.
    """

    verified: bool
    verified_at: datetime | None = None
    campus_id: uuid.UUID | None = None
