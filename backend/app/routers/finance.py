"""Finance: dues cycles, append-only ledger (SPEC §8.2 — no update/delete), spend approvals."""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app import models
from app.core.csv_export import csv_response, sanitize_csv_text
from app.core.errors import conflict, not_found
from app.core.permissions import DUES_ADMIN, Role, require_role
from app.db import get_session
from app.middleware.org_scope import get_current_membership
from app.schemas.finance import (
    DuesCycleCreate,
    DuesCycleOut,
    LedgerEntryCreate,
    LedgerEntryOut,
    SpendApprovalCreate,
    SpendApprovalDecision,
    SpendApprovalOut,
)

router = APIRouter(tags=["finance"])

# NOTE: There is intentionally NO update or delete route for ledger entries anywhere
# (SPEC §2.5, §8.2). Corrections are new entries with entry_type="correction".


# ---- dues cycles ----


@router.get("/chapters/{chapter_id}/dues-cycles")
async def list_dues_cycles(
    chapter_id: uuid.UUID,
    _membership: models.Membership = Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
) -> list[DuesCycleOut]:
    """List the chapter's dues cycles, newest first; any active member."""
    result = await session.execute(
        select(models.DuesCycle)
        .where(models.DuesCycle.chapter_id == chapter_id)
        .order_by(models.DuesCycle.created_at.desc())
    )
    return [DuesCycleOut.model_validate(c) for c in result.scalars().all()]


@router.post("/chapters/{chapter_id}/dues-cycles", status_code=201)
async def create_dues_cycle(
    chapter_id: uuid.UUID,
    body: DuesCycleCreate,
    _membership: models.Membership = Depends(require_role(*DUES_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> DuesCycleOut:
    """Create a dues cycle; treasurer/president only."""
    cycle = models.DuesCycle(
        chapter_id=chapter_id,
        name=body.name,
        amount_cents=body.amount_cents,
        due_date=body.due_date,
    )
    session.add(cycle)
    await session.commit()
    await session.refresh(cycle)
    return DuesCycleOut.model_validate(cycle)


# ---- ledger (append-only) ----


@router.get("/chapters/{chapter_id}/ledger")
async def list_ledger_entries(
    chapter_id: uuid.UUID,
    category: str | None = Query(default=None),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    _membership: models.Membership = Depends(require_role(*DUES_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> list[LedgerEntryOut]:
    """List ledger entries newest-first, optionally filtered; treasurer/president only."""
    query = select(models.LedgerEntry).where(models.LedgerEntry.chapter_id == chapter_id)
    if category is not None:
        query = query.where(models.LedgerEntry.category == category)
    if from_ is not None:
        query = query.where(models.LedgerEntry.created_at >= from_)
    if to is not None:
        query = query.where(models.LedgerEntry.created_at <= to)
    result = await session.execute(query.order_by(models.LedgerEntry.created_at.desc()))
    return [LedgerEntryOut.model_validate(e) for e in result.scalars().all()]


@router.post("/chapters/{chapter_id}/ledger", status_code=201)
async def create_ledger_entry(
    chapter_id: uuid.UUID,
    body: LedgerEntryCreate,
    membership: models.Membership = Depends(require_role(*DUES_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> LedgerEntryOut:
    """Append a ledger entry; treasurer/president only.

    entry_type="correction" MUST reference a prior entry of the SAME chapter via
    corrects_entry_id (422 otherwise). Entries are never updated or deleted.
    """
    if body.entry_type == "correction":
        if body.corrects_entry_id is None:
            raise HTTPException(
                status_code=422, detail="correction_requires_corrects_entry_id"
            )
        target = await session.get(models.LedgerEntry, body.corrects_entry_id)
        if target is None or target.chapter_id != chapter_id:
            raise HTTPException(status_code=422, detail="corrects_entry_not_in_chapter")

    entry = models.LedgerEntry(
        chapter_id=chapter_id,
        entry_type=body.entry_type,
        amount_cents=body.amount_cents,
        category=body.category,
        description=body.description,
        related_user_id=body.related_user_id,
        dues_cycle_id=body.dues_cycle_id,
        corrects_entry_id=body.corrects_entry_id,
        created_by=membership.user_id,
    )
    session.add(entry)
    await session.commit()
    await session.refresh(entry)
    return LedgerEntryOut.model_validate(entry)


def _format_amount_cents(amount_cents: int) -> str:
    """Cents -> decimal dollar string, e.g. -1250 -> "-12.50" (SPEC: positive=in, negative=out).

    Never run through sanitize_csv_text: it's a formatted number, not free text, and
    sanitizing it would corrupt every negative (expense) amount in the export.
    """
    return str((Decimal(amount_cents) / 100).quantize(Decimal("0.01")))


@router.get("/chapters/{chapter_id}/ledger/export.csv")
async def export_ledger_csv(
    chapter_id: uuid.UUID,
    category: str | None = Query(default=None),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    _membership: models.Membership = Depends(require_role(*DUES_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Export the (optionally filtered) ledger as CSV; treasurer/president only.

    Exports the COMPLETE ledger for the chapter — this is the financial record of
    truth, and a partial export is worse than none. Supports the same category/from/to
    filters as list_ledger_entries, so a filtered view exports exactly what it shows.
    corrects_entry_id is included: the ledger is append-only and corrections are new
    offsetting entries, so that linkage has to survive export or the CSV misrepresents
    the record.
    """
    related_user = aliased(models.User)
    creator_user = aliased(models.User)
    query = (
        select(models.LedgerEntry, related_user.display_name, creator_user.display_name)
        .outerjoin(related_user, related_user.id == models.LedgerEntry.related_user_id)
        .join(creator_user, creator_user.id == models.LedgerEntry.created_by)
        .where(models.LedgerEntry.chapter_id == chapter_id)
    )
    if category is not None:
        query = query.where(models.LedgerEntry.category == category)
    if from_ is not None:
        query = query.where(models.LedgerEntry.created_at >= from_)
    if to is not None:
        query = query.where(models.LedgerEntry.created_at <= to)
    result = await session.execute(query.order_by(models.LedgerEntry.created_at.desc()))

    header = [
        "date",
        "entry_type",
        "amount",
        "category",
        "description",
        "related_member",
        "dues_cycle_id",
        "corrects_entry_id",
        "created_by",
    ]
    rows = [
        [
            entry.created_at.isoformat(),
            entry.entry_type,
            _format_amount_cents(entry.amount_cents),
            sanitize_csv_text(entry.category),
            sanitize_csv_text(entry.description),
            sanitize_csv_text(related_name),
            str(entry.dues_cycle_id) if entry.dues_cycle_id is not None else "",
            str(entry.corrects_entry_id) if entry.corrects_entry_id is not None else "",
            sanitize_csv_text(creator_name),
        ]
        for entry, related_name, creator_name in result.all()
    ]
    return csv_response(f"ledger_{chapter_id}.csv", header, rows)


# ---- spend approvals ----


@router.post("/chapters/{chapter_id}/spend-approvals", status_code=201)
async def create_spend_approval(
    chapter_id: uuid.UUID,
    body: SpendApprovalCreate,
    membership: models.Membership = Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
) -> SpendApprovalOut:
    """Request a spend approval; any active member."""
    approval = models.SpendApproval(
        chapter_id=chapter_id,
        requested_by=membership.user_id,
        amount_cents=body.amount_cents,
        description=body.description,
    )
    session.add(approval)
    await session.commit()
    await session.refresh(approval)
    return SpendApprovalOut.model_validate(approval)


@router.get("/chapters/{chapter_id}/spend-approvals")
async def list_spend_approvals(
    chapter_id: uuid.UUID,
    _membership: models.Membership = Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
) -> list[SpendApprovalOut]:
    """List the chapter's spend approvals, newest first; any active member."""
    result = await session.execute(
        select(models.SpendApproval)
        .where(models.SpendApproval.chapter_id == chapter_id)
        .order_by(models.SpendApproval.created_at.desc())
    )
    return [SpendApprovalOut.model_validate(a) for a in result.scalars().all()]


@router.post("/chapters/{chapter_id}/spend-approvals/{approval_id}/decide")
async def decide_spend_approval(
    chapter_id: uuid.UUID,
    approval_id: uuid.UUID,
    body: SpendApprovalDecision,
    membership: models.Membership = Depends(require_role(*DUES_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> SpendApprovalOut:
    """Approve/reject a pending spend approval; treasurer/president; 409 if decided."""
    approval = await session.get(models.SpendApproval, approval_id)
    if approval is None or approval.chapter_id != chapter_id:
        raise not_found("spend_approval_not_found")
    if approval.status != "pending":
        raise conflict("already_decided")
    approval.status = body.status
    approval.decided_by = membership.user_id
    approval.decided_at = datetime.now(timezone.utc)
    await session.commit()
    return SpendApprovalOut.model_validate(approval)
