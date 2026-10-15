"""Durable registrations for account-scoped external operations."""
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AccountExternalOperation(Base):
    __tablename__ = "account_external_operations"
    __table_args__ = (
        UniqueConstraint(
            "actor_user_id", "kind", "effect_key",
            name="uq_account_external_operation_effect",
        ),
        CheckConstraint(
            "kind IN ('media_finalize_post', 'media_finalize_avatar')",
            name="ck_account_external_operation_kind",
        ),
        CheckConstraint(
            "effect_key ~ '^[0-9a-f]{64}$'",
            name="ck_account_external_operation_effect_key",
        ),
        CheckConstraint(
            "state IN ('running', 'finished', 'uncertain')",
            name="ck_account_external_operation_state",
        ),
        CheckConstraint(
            "(state = 'running' AND completed_at IS NULL) OR "
            "(state IN ('finished', 'uncertain') AND completed_at IS NOT NULL)",
            name="ck_account_external_operation_completion_time",
        ),
        Index("ix_account_external_operations_actor", "actor_user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    effect_key: Mapped[str] = mapped_column(Text, nullable=False)
    owner_token: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'running'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        # owner_token is a fencing secret and must never enter debug representations.
        return (
            f"<AccountExternalOperation id={self.id!s} actor_user_id={self.actor_user_id!s} "
            f"kind={self.kind!r} effect_key={self.effect_key!r} state={self.state!r}>"
        )


class AccountExternalOperationScope(Base):
    __tablename__ = "account_external_operation_scopes"
    __table_args__ = (
        Index("ix_account_external_operation_scopes_user", "user_id"),
    )

    operation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account_external_operations.id"),
        primary_key=True,
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True, nullable=False
    )
