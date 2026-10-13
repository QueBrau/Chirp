"""Durable account-deletion write holds (primitive only; no route integration)."""
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AccountDeletionHold(Base):
    __tablename__ = "account_deletion_holds"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_account_deletion_hold_request"),
        Index(
            "uq_account_deletion_hold_active_user",
            "user_id",
            unique=True,
            postgresql_where=text("state = 'active'"),
        ),
        CheckConstraint("state = 'active'", name="ck_account_deletion_hold_state"),
        CheckConstraint("reason = 'account_deletion'", name="ck_account_deletion_hold_reason"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("account_data_requests.id"), nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    reason: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'account_deletion'"))
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
