"""Bounded active WebSocket ownership rows used by account deletion drain."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, SmallInteger, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AccountWsConnection(Base):
    __tablename__ = "account_ws_connections"
    __table_args__ = (
        CheckConstraint("slot >= 0 AND slot < 32", name="ck_account_ws_connection_slot"),
        CheckConstraint("state IN ('running', 'uncertain')", name="ck_account_ws_connection_state"),
        UniqueConstraint("user_id", "slot", name="uq_account_ws_connection_slot"),
        Index("ix_account_ws_connections_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    slot: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    owner_token: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'running'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    def __repr__(self) -> str:
        return (
            f"<AccountWsConnection id={self.id!s} user_id={self.user_id!s} "
            f"slot={self.slot} state={self.state!r}>"
        )
