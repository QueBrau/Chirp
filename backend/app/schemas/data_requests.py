"""Wire schemas for authenticated account export and deletion requests."""

import uuid
from datetime import datetime
from typing import Literal

from app.schemas.base import _Schema

DataRequestKind = Literal["export", "deletion"]
DataRequestStatus = Literal[
    "received", "verifying", "processing", "ready", "completed",
    "partially_completed", "blocked", "failed", "canceled",
]


class DataRequestCreate(_Schema):
    kind: DataRequestKind


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
    failure_code: str | None = None
