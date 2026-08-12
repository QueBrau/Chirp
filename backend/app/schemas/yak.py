"""Yak (anonymous board) schemas: yaks, votes, content reports, user blocks."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ReportTargetType = Literal["yak", "post", "comment", "message_forward", "user"]
ReportStatus = Literal["open", "actioned", "dismissed"]


class _Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


# ---- yaks ----


class YakCreate(_Schema):
    body: str = Field(min_length=1)


class YakOut(_Schema):
    """Anonymous to peers: NO author field of any kind (SPEC §8.3)."""

    id: uuid.UUID
    campus_id: uuid.UUID
    body: str
    score: int
    created_at: datetime


# ---- votes ----


class YakVoteCreate(_Schema):
    """Body for PUT /yaks/{yak_id}/vote."""

    value: Literal[-1, 1]


class YakVoteOut(_Schema):
    yak_id: uuid.UUID
    value: int


# ---- moderation: reports ----


class ContentReportCreate(_Schema):
    target_type: ReportTargetType
    target_id: uuid.UUID | None = None
    forwarded_plaintext: str | None = None
    reason: str = Field(min_length=1)


class ContentReportOut(_Schema):
    id: uuid.UUID
    reporter_id: uuid.UUID
    target_type: ReportTargetType
    target_id: uuid.UUID | None = None
    forwarded_plaintext: str | None = None
    reason: str
    status: ReportStatus
    created_at: datetime


# ---- moderation: yak removal ----


class YakRemoveRequest(_Schema):
    """Body for POST /moderation/yaks/{yak_id}/remove."""

    reason: str = Field(min_length=1)


# ---- blocks ----


class UserBlockCreate(_Schema):
    blocked_id: uuid.UUID


class UserBlockOut(_Schema):
    blocker_id: uuid.UUID
    blocked_id: uuid.UUID
    created_at: datetime
