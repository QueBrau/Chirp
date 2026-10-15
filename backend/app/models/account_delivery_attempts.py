"""Bounded recipient delivery ownership, independent of coalesced outbox rows."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, SmallInteger, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AccountDeliveryAttempt(Base):
    __tablename__ = "account_delivery_attempts"
    __table_args__ = (
        CheckConstraint("slot >= 0 AND slot < 32", name="ck_account_delivery_attempt_slot"),
        CheckConstraint("state IN ('running', 'uncertain')", name="ck_account_delivery_attempt_state"),
        UniqueConstraint("user_id", "slot", name="uq_account_delivery_attempt_slot"),
        UniqueConstraint("outbox_id", "user_id", name="uq_account_delivery_attempt_recipient"),
        Index("ix_account_delivery_attempts_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    slot: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    # Deliberately not a foreign key: coalescing or retiring an outbox row must
    # never erase evidence of a still-running or ambiguous external effect.
    outbox_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    owner_token: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'running'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    def __repr__(self) -> str:
        return f"<AccountDeliveryAttempt id={self.id!s} user_id={self.user_id!s} state={self.state!r}>"
