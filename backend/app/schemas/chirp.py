"""Chirp (anonymous board) schemas: chirps, votes, content reports, user blocks."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field

from app.core.validation import MAX_CHIRP_BODY_LENGTH, MAX_REASON_LENGTH
from app.schemas.base import _Schema

ReportTargetType = Literal["chirp", "post", "comment", "message_forward", "user"]
ReportStatus = Literal["open", "actioned", "dismissed"]


# ---- chirps ----


class ChirpCreate(_Schema):
    body: str = Field(min_length=1, max_length=MAX_CHIRP_BODY_LENGTH)


class ChirpOut(_Schema):
    """Anonymous to peers: NO author field of any kind (SPEC §8.3)."""

    id: uuid.UUID
    campus_id: uuid.UUID
    body: str
    score: int
    created_at: datetime


# ---- votes ----


class ChirpVoteCreate(_Schema):
    """Body for PUT /chirps/{chirp_id}/vote."""

    value: Literal[-1, 1]


class ChirpVoteOut(_Schema):
    chirp_id: uuid.UUID
    value: int


# ---- moderation: reports ----


class ContentReportCreate(_Schema):
    target_type: ReportTargetType
    target_id: uuid.UUID | None = None
    forwarded_plaintext: str | None = None
    reason: str = Field(min_length=1, max_length=MAX_REASON_LENGTH)


class ContentReportOut(_Schema):
    id: uuid.UUID
    reporter_id: uuid.UUID
    # Resolved server-side (finding 1: moderation scoping); never client-supplied.
    campus_id: uuid.UUID | None = None
    target_type: ReportTargetType
    target_id: uuid.UUID | None = None
    forwarded_plaintext: str | None = None
    reason: str
    status: ReportStatus
    created_at: datetime


# ---- moderation: chirp removal ----


class ChirpRemoveRequest(_Schema):
    """Body for POST /moderation/chirps/{chirp_id}/remove."""

    reason: str = Field(min_length=1, max_length=MAX_REASON_LENGTH)


# ---- blocks ----


class UserBlockCreate(_Schema):
    blocked_id: uuid.UUID


class UserBlockOut(_Schema):
    blocker_id: uuid.UUID
    blocked_id: uuid.UUID
    created_at: datetime
