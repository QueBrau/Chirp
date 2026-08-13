"""Finance models (append-only ledger): dues cycles, ledger entries, spend approvals (SPEC §3)."""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class DuesCycle(Base):
    __tablename__ = "dues_cycles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    chapter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chapters.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)  # "Spring 2027 Dues"
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class LedgerEntry(Base):
    """Append-only by design: no updated_at, no soft delete (SPEC §2.5, §8.2)."""

    __tablename__ = "ledger_entries"
    __table_args__ = (
        CheckConstraint(
            "entry_type IN ('dues_payment','expense','budget_allocation',"
            "'correction','payout')",
            name="ck_ledger_entries_entry_type",
        ),
        Index(
            "idx_ledger_chapter_time",
            "chapter_id",
            text("created_at DESC"),
        ),
        # Stripe delivers webhooks at-least-once and retries for days. The ledger is
        # append-only, so a replayed payment_intent.succeeded would append a SECOND
        # permanent dues payment. This constraint — not a check-then-insert, which
        # races — is what makes replay handling correct.
        Index(
            "uq_ledger_stripe_payment_intent",
            "stripe_payment_intent_id",
            unique=True,
            postgresql_where=text("stripe_payment_intent_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    chapter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chapters.id"), nullable=False
    )
    entry_type: Mapped[str] = mapped_column(Text, nullable=False)
    # Positive = in, negative = out.
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    category: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    # Who paid, for dues.
    related_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id")
    )
    dues_cycle_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dues_cycles.id")
    )
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(Text)
    # For corrections.
    corrects_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ledger_entries.id")
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class SpendApproval(Base):
    __tablename__ = "spend_approvals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','approved','rejected')",
            name="ck_spend_approvals_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    chapter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chapters.id"), nullable=False
    )
    requested_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id")
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class ChapterStripeCustomer(Base):
    """Maps (member, chapter) to a Stripe Customer on that chapter's connected account.

    Under direct charges the Customer lives on the connected account, not the
    platform — so a member in two chapters legitimately has two Customers. A
    users.stripe_customer_id column would work for the first chapter and silently
    charge the wrong account for the second.
    """

    __tablename__ = "chapter_stripe_customers"
    __table_args__ = (PrimaryKeyConstraint("user_id", "chapter_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    chapter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chapters.id"), nullable=False
    )
    stripe_customer_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class ProcessedStripeEvent(Base):
    """Event-level webhook dedup: the PK insert fails on a replayed event id."""

    __tablename__ = "processed_stripe_events"

    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
