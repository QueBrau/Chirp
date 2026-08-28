"""Finance models (append-only ledger): dues cycles, ledger entries, spend approvals,
dues payment plans (SPEC §3, board card c195)."""

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
            "'correction','payout','dues_installment')",
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


class DuesPaymentIntent(Base):
    """One member's attempt to pay one dues cycle (board card c51).

    This exists because nothing else knew a payment was IN FLIGHT. A dues payment
    was only recorded once its webhook settled, so across ACH's multi-day
    "processing" window the cycle still read as unpaid — and since the Stripe
    idempotency key was scoped per rail, a retry on the other rail created a
    genuinely different PaymentIntent rather than a deduped one. Both settled,
    both appended to an append-only ledger, and the member paid twice.

    The row is inserted BEFORE Stripe is called, so a second attempt loses the
    race against uq_dues_intent_live at the database rather than at Stripe.
    Only 'open' and 'succeeded' hold the reservation: a genuinely failed or
    canceled payment must stay retryable, which is why payment_failed and
    payment_intent.canceled move the row out of those states.

    uq_dues_intent_live only guards THIS table. The cross_table_dues_guard_intents
    trigger (migration 0028, board c230) is where the OTHER half of this row's
    invariant lives: a row entering 'open' or 'succeeded' here is refused if the
    same (dues_cycle_id, user_id) already has an ACTIVE DuesPaymentPlan, closing
    the TOCTOU a plain read-then-insert left between this table and that one.
    """

    __tablename__ = "dues_payment_intents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    chapter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chapters.id"), nullable=False
    )
    dues_cycle_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dues_cycles.id"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    rail: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'"))
    # Null between reserving the slot and Stripe answering.
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
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


class DuesPaymentPlan(Base):
    """One member's installment plan for one dues cycle (board card c195).

    total_cents/installment_count are the plan as agreed; the actual schedule lives
    on DuesPlanInstallment rows. status starts 'active', ends 'completed' once every
    installment is paid, or 'canceled' if DUES_ADMIN calls it off early.

    uq_dues_payment_plans_active_per_member (migration 0023) enforces AT MOST ONE
    active plan per (dues_cycle_id, user_id) at the database, the same shape as
    uq_dues_intent_live (c51) and uq_role_terms_open_per_membership (c83) — a second
    create-plan call for the same cycle/member loses the race here, not in the route.

    That index only guards THIS table. The cross_table_dues_guard_plans trigger
    (migration 0028, board c230) is where the OTHER half of this row's invariant
    lives: a row entering 'active' here is refused if the same
    (dues_cycle_id, user_id) already has a LIVE DuesPaymentIntent reservation,
    closing the TOCTOU a plain read-then-insert left between this table and that one.
    """

    __tablename__ = "dues_payment_plans"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','completed','canceled')",
            name="ck_dues_payment_plans_status",
        ),
        Index(
            "uq_dues_payment_plans_active_per_member",
            "dues_cycle_id",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    chapter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chapters.id"), nullable=False
    )
    dues_cycle_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dues_cycles.id"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    total_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    installment_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'")
    )
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class DuesPlanInstallment(Base):
    """One scheduled slice of a DuesPaymentPlan (board card c195).

    paid_at/ledger_entry_id are both NULL until the installment is recorded paid,
    at which point they are set together with the entry_type='dues_installment'
    ledger row that records the money (see routers — the ledger stays append-only;
    this row is never later reversed here, only via a correction on the ledger).
    ON DELETE CASCADE on plan_id: an installment has no meaning once its plan is
    gone. UNIQUE(plan_id, seq) so a plan can never carry two rows for one slot.
    """

    __tablename__ = "dues_plan_installments"
    __table_args__ = (
        Index("uq_dues_plan_installments_seq", "plan_id", "seq", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dues_payment_plans.id", ondelete="CASCADE"),
        nullable=False,
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ledger_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ledger_entries.id")
    )
