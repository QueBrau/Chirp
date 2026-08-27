"""Finance schemas: dues cycles, append-only ledger entries, spend approvals.

Per SPEC §8.2 there is intentionally NO LedgerEntryUpdate schema — corrections are
new entries with entry_type="correction" and corrects_entry_id set.
"""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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


class _Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


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
    id: uuid.UUID
    plan_id: uuid.UUID
    seq: int
    amount_cents: int
    due_date: date
    paid_at: datetime | None = None
    ledger_entry_id: uuid.UUID | None = None


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
