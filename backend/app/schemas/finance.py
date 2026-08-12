"""Finance schemas: dues cycles, append-only ledger entries, spend approvals.

Per SPEC §8.2 there is intentionally NO LedgerEntryUpdate schema — corrections are
new entries with entry_type="correction" and corrects_entry_id set.
"""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

LedgerEntryType = Literal[
    "dues_payment", "expense", "budget_allocation", "correction", "payout"
]
SpendApprovalStatus = Literal["pending", "approved", "rejected"]


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
