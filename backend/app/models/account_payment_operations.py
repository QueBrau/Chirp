"""Durable logical payment effects and fenced provider attempts."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AccountPaymentEffect(Base):
    """Immutable Stripe request identity shared across retries and officers."""

    __tablename__ = "account_payment_effects"
    __table_args__ = (
        UniqueConstraint("kind", "effect_key", name="uq_account_payment_effect_key"),
        CheckConstraint(
            "kind IN ('connect_account_create', 'connect_account_link', "
            "'customer_create', 'customer_session_create', "
            "'payment_intent_create', 'payment_intent_cancel')",
            name="ck_account_payment_effect_kind",
        ),
        CheckConstraint("effect_key ~ '^[0-9a-f]{64}$'", name="ck_account_payment_effect_key_sha256"),
        CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_account_payment_effect_fingerprint_sha256",
        ),
        CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 255",
            name="ck_account_payment_effect_idempotency_key",
        ),
        CheckConstraint(
            "connected_account_id IS NULL OR connected_account_id ~ '^acct_[A-Za-z0-9]{1,240}$'",
            name="ck_account_payment_effect_connected_account",
        ),
        CheckConstraint(
            "(kind = 'connect_account_create' AND connected_account_id IS NULL AND reservation_id IS NULL) OR "
            "(kind = 'connect_account_link' AND connected_account_id IS NOT NULL AND reservation_id IS NULL) OR "
            "(kind IN ('customer_create', 'customer_session_create') AND connected_account_id IS NOT NULL AND reservation_id IS NULL) OR "
            "(kind IN ('payment_intent_create', 'payment_intent_cancel') AND connected_account_id IS NOT NULL AND reservation_id IS NOT NULL)",
            name="ck_account_payment_effect_scope_shape",
        ),
        CheckConstraint(
            "provider_object_id IS NULL OR provider_object_id ~ '^(acct|cus|pi)_[A-Za-z0-9]{1,240}$'",
            name="ck_account_payment_effect_provider_id",
        ),
        Index("ix_account_payment_effects_kind_created", "kind", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    effect_key: Mapped[str] = mapped_column(Text, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    connected_account_id: Mapped[str | None] = mapped_column(Text)
    # Intentionally no FK: callers may already hold the reservation row lock.
    reservation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    provider_object_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    def __repr__(self) -> str:
        return (
            f"<AccountPaymentEffect id={self.id!s} kind={self.kind!r} "
            f"effect_key={self.effect_key!r} provider_object_id={self.provider_object_id!r}>"
        )


class AccountPaymentAttempt(Base):
    """One owner-fenced Stripe I/O attempt under an admitted request."""

    __tablename__ = "account_payment_attempts"
    __table_args__ = (
        CheckConstraint(
            "state IN ('running', 'uncertain', 'finished', 'reconciled')",
            name="ck_account_payment_attempt_state",
        ),
        CheckConstraint(
            "(state = 'running' AND completed_at IS NULL) OR "
            "(state IN ('uncertain', 'finished', 'reconciled') AND completed_at IS NOT NULL)",
            name="ck_account_payment_attempt_completion_time",
        ),
        Index("ix_account_payment_attempts_user_state", "actor_user_id", "state"),
        Index("ix_account_payment_attempts_effect_state", "effect_id", "state"),
        Index("ix_account_payment_attempts_parent", "parent_operation_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    effect_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("account_payment_effects.id"), nullable=False
    )
    parent_operation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("account_external_operations.id"), nullable=False
    )
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    owner_token: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'running'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return (
            f"<AccountPaymentAttempt id={self.id!s} effect_id={self.effect_id!s} "
            f"actor_user_id={self.actor_user_id!s} state={self.state!r}>"
        )
