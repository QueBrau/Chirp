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
    """A dues cycle, plus where THE CALLER stands on it (board c258).

    `viewer_paid` / `viewer_on_plan` are the caller's own bucket under the one house
    rule - core/dues_status.py's netting, then the same three-way split
    chapter_overview and the ledger summary apply. They exist so the member's dues
    screen does not have to work its own standing out of the ledger: it used to derive
    "have I paid" by scanning ledger rows, which breaks the moment that list is
    paginated (a payment row falling off a page reads as unpaid - the app appearing to
    lose someone's money) and which had also grown a latch the server had already
    removed, where a COMPLETED plan counted as permanent proof of payment even after
    its installments were corrected away.

    Both are False on cycles for a caller with no contributions, which is the honest
    default for a cycle nobody has paid into yet.
    """

    id: uuid.UUID
    chapter_id: uuid.UUID
    name: str
    amount_cents: int
    due_date: date
    created_at: datetime
    viewer_paid: bool = False
    viewer_on_plan: bool = False


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


class LedgerCategoryTotal(_Schema):
    """One category's total SPENDING for a chapter, as POSITIVE cents.

    `category` is None for entries that carry no category, and stays None rather than
    being labelled here: the display string ("Uncategorised") is the client's, and
    inventing a second copy of it server-side is how two surfaces start disagreeing
    about a label. Income is excluded entirely - this mirrors the client's
    spendByCategory, which skips amount_cents >= 0, because the donut is "where the
    money went" and an income slice would make the total meaningless.
    """

    category: str | None = None
    cents: int


class LedgerBalancePoint(_Schema):
    """Running balance at the END of one calendar month (board c258).

    NOT a per-month delta: the value is the chapter's cumulative balance as of that
    month's end, which is what the client's runningBalance() plotted per-transaction.
    Bucketing by month changes the RESOLUTION of the trend, not its meaning, and bounds
    the series by the chapter's AGE rather than by its transaction count - a four-year-
    old chapter is 48 points forever.

    `partial` marks the bucket the chapter is currently inside. A trend line that dips
    at the end because the month is half over is a confidently-wrong chart, so the
    client is told which point is incomplete rather than having to infer it from a clock.
    """

    period_start: datetime
    balance_cents: int
    partial: bool = False


class LedgerDuesSummary(_Schema):
    """Dues collection for the chapter's current cycle, netted the ONE house way.

    Computed through core/dues_status.py's dues_contributions_subquery, never by
    filtering the ledger here. That module is the single definition of "has this member
    paid" precisely because two surfaces once answered it differently; this is a third
    reader, and it inherits rather than reimplements.
    """

    cycle_id: uuid.UUID
    amount_cents: int
    collected_cents: int
    paid_members: int
    # Reported alongside paid so the two surfaces answering "who has paid" are directly
    # COMPARABLE, which is what the cross-endpoint test asserts. Splitting the roster the
    # same way chapter_overview does is what makes drift detectable instead of arguable.
    on_plan_members: int = 0


class LedgerSummaryOut(_Schema):
    """Every number the treasurer dashboard renders, computed server-side (board c258).

    Exists so that paginating GET /chapters/{id}/ledger cannot silently corrupt a total.
    Before this, the screen reduced the full list client-side for the balance, the trend,
    the category donut and the dues meter - so the first page of a cursor would have
    become "the" balance. The list is now render-only.
    """

    balance_cents: int
    entry_count: int
    categories: list[LedgerCategoryTotal]
    trend: list[LedgerBalancePoint]
    dues: LedgerDuesSummary | None = None


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


# Cap on the installment schedule (board c265, from the c263 abuse sweep). The route
# already forces the schedule to sum to EXACTLY the cycle's amount_cents - but that
# bounds VALUE, not COUNT: every amount_cents is gt=0, i.e. >= 1 cent, so a $325
# cycle legally accepted a 32,500-row schedule of one-cent slices from a single
# request. Real plans are 2-12 payments; 36 is three years of monthly installments,
# generous past any schedule a treasurer would actually offer while three orders of
# magnitude under the degenerate one. installment_count needs no cap of its own:
# the route 422s unless it equals len(installments) (routers/finance.py), so this
# one bound covers both fields.
MAX_PLAN_INSTALLMENTS = 36


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
    installments: list[DuesPlanInstallmentCreate] = Field(
        min_length=1, max_length=MAX_PLAN_INSTALLMENTS
    )


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
