"""Wire schemas for authenticated account export and deletion requests."""

import uuid
from datetime import datetime
from typing import Literal
from pydantic import Field

from app.schemas.base import _Schema

DataRequestKind = Literal["export", "deletion"]
DataRequestStatus = Literal[
    "received", "verifying", "processing", "ready", "completed",
    "partially_completed", "blocked", "failed", "canceled",
]


class DataRequestCreate(_Schema):
    kind: DataRequestKind


class PostDeletionCleanupOut(_Schema):
    category: Literal["temporary_media", "identifiable_analytics"]
    state: Literal["pending", "running", "uncertain", "complete", "manual_review"]


class DataRequestOut(_Schema):
    id: uuid.UUID
    kind: DataRequestKind
    status: DataRequestStatus
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    # Artifacts are downloaded through the authenticated endpoint; this is never a
    # bearer URL and remains null until a separately configured artifact host exists.
    download_url: str | None = None
    expires_at: datetime | None = None
    scope: list[str]
    excluded: list[str]
    retention_reasons: list[str]
    provider_steps: dict[str, str]
    post_deletion_cleanup: list[PostDeletionCleanupOut] = Field(default_factory=list)
    failure_code: str | None = None
