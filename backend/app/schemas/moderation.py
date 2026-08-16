"""Account suspension + content-removal schemas (board card c76)."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ModerationActionName = Literal["suspend_user", "unsuspend_user", "remove_content"]
ModerationTargetType = Literal["user", "yak", "post", "comment"]
# Yaks have their own dedicated removal route (POST /moderation/yaks/{yak_id}/remove,
# anonymous-content shaped); this generic endpoint covers the named-author content
# types instead of a third near-identical route.
RemovableContentType = Literal["post", "comment"]


class _Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


# ---- account suspension ----


class SuspendUserRequest(_Schema):
    """Body for both POST /moderation/users/{id}/suspend and .../unsuspend — a reason
    is required either way so the moderation_actions audit row always has a 'why'."""

    reason: str = Field(min_length=1)


class SuspensionStateOut(_Schema):
    """Current suspension state of a user row — mirrors users.suspended_* columns."""

    id: uuid.UUID
    suspended_at: datetime | None = None
    suspension_reason: str | None = None
    suspended_by: uuid.UUID | None = None


# ---- content removal ----


class ContentRemoveRequest(_Schema):
    """Body for POST /moderation/content/remove."""

    target_type: RemovableContentType
    target_id: uuid.UUID
    reason: str = Field(min_length=1)


# ---- audit trail ----


class ModerationActionOut(_Schema):
    id: uuid.UUID
    actor_id: uuid.UUID
    action: ModerationActionName
    target_type: ModerationTargetType
    target_id: uuid.UUID
    reason: str
    created_at: datetime
