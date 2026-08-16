"""Account suspension + content-removal schemas (board card c76)."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ModerationActionName = Literal[
    "suspend_user", "unsuspend_user", "remove_content", "resolve_report"
]
ModerationTargetType = Literal["user", "yak", "post", "comment", "report"]
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


# ---- report resolution ----

# 'open' is deliberately NOT resolvable-to: this endpoint closes a report, it does not
# reopen one. Reopening would need its own rule about who may undo another moderator's
# decision, and there is no product answer for that yet (board c91).
ReportResolution = Literal["actioned", "dismissed"]


class ReportResolveRequest(_Schema):
    """Body for PATCH /moderation/reports/{report_id}.

    `reason` is required for the same reason it is on every other route in this file:
    the moderation_actions row must always carry a 'why', so the audit trail is worth
    reading later. "actioned" means the report was acted on (content removed, user
    suspended); "dismissed" means it was reviewed and no action was warranted. Both are
    a moderator having LOOKED at it, which is the distinction the queue needs.
    """

    status: ReportResolution
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
