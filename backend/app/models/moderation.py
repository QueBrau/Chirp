"""Moderation audit trail (board card c76, migration 0011).

A durable log, deliberately separate from the mutable state columns (users.suspended_*,
posts/post_comments.removed_reason): those columns hold ONLY the current state and get
overwritten by the next action, so a suspend -> unsuspend -> suspend cycle would lose the
first suspension's reason entirely. This table is append-only by application convention
(nothing UPDATEs or DELETEs a row) so "who removed what, when, and why" survives every
subsequent action on the same target.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ModerationAction(Base):
    __tablename__ = "moderation_actions"
    __table_args__ = (
        CheckConstraint(
            "action IN ('suspend_user', 'unsuspend_user', 'remove_content')",
            name="ck_moderation_actions_action",
        ),
        CheckConstraint(
            "target_type IN ('user', 'chirp', 'post', 'comment')",
            name="ck_moderation_actions_target_type",
        ),
        Index("idx_moderation_actions_target", "target_type", "target_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
