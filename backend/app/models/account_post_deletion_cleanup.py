"""Private durable obligations for account-scoped cleanup after tombstoning."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, JSON, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AccountPostDeletionCleanup(Base):
    """A private cleanup obligation that never changes the completed tombstone."""

    __tablename__ = "account_post_deletion_cleanup"
    __table_args__ = (
        UniqueConstraint("request_id", "category", name="uq_account_post_deletion_cleanup_request_category"),
        CheckConstraint("category IN ('temporary_media', 'identifiable_analytics')", name="ck_account_post_deletion_cleanup_category"),
        CheckConstraint("state IN ('pending', 'running', 'uncertain', 'complete', 'manual_review')", name="ck_account_post_deletion_cleanup_state"),
        CheckConstraint("attempt_count >= 0 AND attempt_count <= 1000000", name="ck_account_post_deletion_cleanup_attempts"),
        CheckConstraint("scope_sha256 ~ '^[0-9a-f]{64}$'", name="ck_account_post_deletion_cleanup_scope_digest"),
        CheckConstraint("last_pass_digest ~ '^[0-9a-f]{64}$'", name="ck_account_post_deletion_cleanup_pass_digest"),
        CheckConstraint("remaining_count IS NULL OR (remaining_count >= 0 AND remaining_count <= 10000)", name="ck_account_post_deletion_cleanup_remaining_count"),
        Index("ix_account_post_deletion_cleanup_due", "state", "next_attempt_at"),
        Index("ix_account_post_deletion_cleanup_user", "user_id", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("account_data_requests.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    scope: Mapped[dict] = mapped_column(JSON, nullable=False)
    scope_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    last_pass_digest: Mapped[str] = mapped_column(Text, nullable=False)
    remaining_count: Mapped[int | None] = mapped_column()
    attempt_count: Mapped[int] = mapped_column(nullable=False, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    owner_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
