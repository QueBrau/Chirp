"""Account export and deletion request records with immutable fulfillment metadata."""

import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    DateTime,
    ForeignKey,
    Index,
    JSON,
    UniqueConstraint,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AccountDataRequest(Base):
    __tablename__ = "account_data_requests"
    __table_args__ = (
        Index("ix_account_data_requests_user_created", "user_id", "created_at"),
        Index("uq_account_data_request_open", "user_id", "kind", "open_key", unique=True, postgresql_where=text("open_key IS NOT NULL")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'received'"))
    open_key: Mapped[str | None] = mapped_column(Text)
    scope: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    excluded: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    retention_reasons: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    failure_code: Mapped[str | None] = mapped_column(Text)
    provider_steps: Mapped[dict] = mapped_column(JSON, nullable=False, server_default=text("'{}'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fulfillment_lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    fulfillment_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AccountDataArtifact(Base):
    __tablename__ = "account_data_artifacts"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("account_data_requests.id"), nullable=False, unique=True)
    content: Mapped[dict] = mapped_column(JSON, nullable=False)
    content_sha256: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

class AccountFulfillmentStep(Base):
    __tablename__ = "account_fulfillment_steps"
    __table_args__ = (
        Index("ix_account_fulfillment_steps_request", "request_id", "step_key"),
        UniqueConstraint("request_id", "step_key", name="uq_account_fulfillment_step"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("account_data_requests.id"), nullable=False)
    step_key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    attempt_count: Mapped[int] = mapped_column(nullable=False, server_default=text("0"))
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    provider_ref: Mapped[dict | None] = mapped_column(JSON)
    last_error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
