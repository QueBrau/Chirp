"""Finance schemas: dues cycles, append-only ledger entries, spend approvals.

Per SPEC §8.2 there is intentionally NO LedgerEntryUpdate schema — corrections are
new entries with entry_type="correction" and corrects_entry_id set.
"""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import Field

from app.schemas.base import _Schema

LedgerEntryType = Literal[
    "dues_payment",
    "expense",
    "budget_allocation",
    "correction",
    "payout",
    "dues_installment",
]
SpendApprovalStatus = Literal["pending", "approved", "rejected"]
DuesPaymentPlanStatus = Literal["active", "completed", "canceled"]


# ---- dues cycles ----


class DuesCycleCreate(_Schema):
    name: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)
    due_date: date


class DuesCycleOut(_Schema):
    id: uuid.UUID
    chapter_id: uuid.UUID
    name: str
    amount_cents: int
    due_date: date
    created_at: datetime


# ---- ledger (append-only) ----


class LedgerEntryCreate(_Schema):
    """Body for POST /chapters/{chapter_id}/ledger. created_by comes from auth."""

    entry_type: LedgerEntryType
    amount_cents: int
    category: str | None = None
    description: str | None = None
    related_user_id: uuid.UUID | None = None
    dues_cycle_id: uuid.UUID | None = None
    corrects_entry_id: uuid.UUID | None = None


class LedgerEntryOut(_Schema):
    id: uuid.UUID
    chapter_id: uuid.UUID
    entry_type: LedgerEntryType
    amount_cents: int
    category: str | None = None
    description: str | None = None
    related_user_id: uuid.UUID | None = None
    dues_cycle_id: uuid.UUID | None = None
    stripe_payment_intent_id: str | None = None
    corrects_entry_id: uuid.UUID | None = None
    created_by: uuid.UUID
    created_at: datetime


# ---- spend approvals ----


class SpendApprovalCreate(_Schema):
    amount_cents: int = Field(gt=0)
    description: str = Field(min_length=1)


class SpendApprovalUpdate(_Schema):
    """Decision body for the spend-approval decide route."""

    status: Literal["approved", "rejected"]


SpendApprovalDecision = SpendApprovalUpdate


class SpendApprovalOut(_Schema):
    id: uuid.UUID
    chapter_id: uuid.UUID
    requested_by: uuid.UUID
    amount_cents: int
    description: str
    status: SpendApprovalStatus
    decided_by: uuid.UUID | None = None
    decided_at: datetime | None = None
    created_at: datetime


# ---- dues payment plans (board card c195) ----


class DuesPlanInstallmentCreate(_Schema):
    """One scheduled slice in the body of POST .../dues-cycles/{cycle_id}/plans."""

    amount_cents: int = Field(gt=0)
    due_date: date


class DuesPaymentPlanCreate(_Schema):
    """Body for POST /chapters/{chapter_id}/dues-cycles/{cycle_id}/plans.

    total_cents is NOT accepted here — it is always the cycle's own amount_cents,
    and the route 422s unless the installments sum to exactly that, the same "server
    computes the money, client never asserts it" rule create_dues_payment_intent
    already follows for the lump-sum path.
    """

    user_id: uuid.UUID
    installment_count: int = Field(gt=0)
    note: str | None = None
    installments: list[DuesPlanInstallmentCreate] = Field(min_length=1)


class DuesPlanInstallmentOut(_Schema):
    """paid_at/ledger_entry_id are write-once on the model (app/models/finance.py's
    DuesPlanInstallment docstring) — set together the moment record_dues_installment_
    payment records the payment, never touched again, so paid_at still shows WHEN an
    installment was originally recorded even after the money moves back out.

    effective_paid (c233) is the derived, current truth: it nets any correction rows
    pointing at ledger_entry_id (same corrects_entry_id chain dues_status.py's
    dues_contributions_subquery reads) against that entry's own amount_cents, and is
    True only when paid_at is set AND that net is still positive — i.e. the money is
    genuinely still in hand, same net>0 threshold the rest of the codebase uses for
    "paid". A fully (or over-) refunded installment reads effective_paid=False while
    paid_at stays exactly as it was. paid_at is history; effective_paid is status —
    prefer effective_paid for anything that decides whether an installment still
    needs collecting. app-mobile/src/payments/dues.tsx currently reads paid_at
    directly to render "paid" state; switching it to effective_paid is a follow-up,
    not done here (this wave is backend-only, finance.py + schemas/finance.py).
    """

    id: uuid.UUID
    plan_id: uuid.UUID
    seq: int
    amount_cents: int
    due_date: date
    paid_at: datetime | None = None
    ledger_entry_id: uuid.UUID | None = None
    effective_paid: bool


class DuesPaymentPlanOut(_Schema):
    id: uuid.UUID
    chapter_id: uuid.UUID
    dues_cycle_id: uuid.UUID
    user_id: uuid.UUID
    total_cents: int
    installment_count: int
    status: DuesPaymentPlanStatus
    note: str | None = None
    created_by: uuid.UUID
    created_at: datetime
    installments: list[DuesPlanInstallmentOut] = []


class DuesInstallmentRecordPaymentRequest(_Schema):
    """Body for POST .../dues-plans/{plan_id}/installments/{seq}/record-payment.

    The ledger entry is what records the money; `note` is free text for a
    treasurer to note how it arrived (e.g. "cash", "venmo, confirmed 8/24") since
    an installment plan has no PaymentIntent/rail of its own.
    """

    note: str | None = None
