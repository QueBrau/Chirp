"""Finance: dues cycles, append-only ledger (SPEC §8.2 — no update/delete), spend
approvals, dues payment plans (board card c195)."""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app import models
from app.core.csv_export import csv_response, sanitize_csv_text
from app.core.dues_status import dues_contributions_subquery
from app.core.errors import conflict, not_found
from app.core.permissions import DUES_ADMIN, Role, require_role
from app.db import get_session
from app.middleware.org_scope import get_current_membership
from app.schemas.finance import (
    DuesCycleCreate,
    DuesCycleOut,
    DuesInstallmentRecordPaymentRequest,
    DuesPaymentPlanCreate,
    DuesPaymentPlanOut,
    DuesPlanInstallmentOut,
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
    corrects_entry_id (422 otherwise), and that target must not itself be a
    correction (422 otherwise -- board c194). Entries are never updated or
    deleted.

    Corrections may target any non-correction entry_type (dues_payment, expense,
    budget_allocation, payout) -- SPEC.md §2.5 rule 5 defines a correction as an
    offsetting entry for the append-only ledger generally, not only for dues
    payments, and test_ledger_append_only.py already exercises correcting an
    expense entry. What must be rejected is chaining: C2(correction) ->
    C1(correction) -> P is accepted at write time but dues_contributions_
    subquery (app/core/dues_status.py) joins a correction only directly against
    the dues_payment row it corrects, so C2 would silently net to zero effect
    on the number it was meant to move while still returning 201 -- money-
    correctness finding c191/c194. Rejecting any correction-of-a-correction here
    closes that hole without narrowing what a correction may otherwise target.

    entry_type="dues_installment" is ALWAYS rejected here (422
    dues_installment_requires_plan_route -- c232). The only coherent way an
    installment ledger row comes into existence is record_dues_installment_payment:
    it stamps the specific plan installment's paid_at/ledger_entry_id in the same
    transaction that inserts the row, which is what lets the plan's read path
    (_load_installments/_plan_out, c233) net corrections back against a real
    installment. A dues_installment row created here would have no seq, no
    plan_id, nothing for that read path to attach it to -- exactly the plan/ledger
    incoherence c233 exists to close, so this route refuses to manufacture one
    rather than accept it and leave the plan's own view of itself wrong.

    entry_type="dues_payment" for a (dues_cycle_id, related_user_id) that
    currently has an ACTIVE DuesPaymentPlan 409s member_on_payment_plan (c232),
    mirroring payments.py's create_dues_payment_intent guard (c195) from the
    manual-entry side: a plan member pays through record_dues_installment_payment
    so the plan's own state (paid_at, completion) stays coherent with the ledger.
    A treasurer hand-entering a "dues_payment" against a plan member here would
    collect real money the plan's installments already account for, double-
    collecting them silently -- the plan and the ledger would each believe the
    member still owes the full cycle amount independently. Checked only when both
    dues_cycle_id and related_user_id are given: with either missing there is no
    (cycle, member) pair to look up, same as every other guard on this route.
    """
    if body.entry_type == "correction":
        if body.corrects_entry_id is None:
            raise HTTPException(
                status_code=422, detail="correction_requires_corrects_entry_id"
            )
        target = await session.get(models.LedgerEntry, body.corrects_entry_id)
        if target is None or target.chapter_id != chapter_id:
            raise HTTPException(status_code=422, detail="corrects_entry_not_in_chapter")
        if target.entry_type == "correction":
            raise HTTPException(
                status_code=422, detail="correction_target_is_correction"
            )
    elif body.entry_type == "dues_installment":
        # c232: only record_dues_installment_payment may create these — see the
        # docstring above.
        raise HTTPException(
            status_code=422, detail="dues_installment_requires_plan_route"
        )
    elif (
        body.entry_type == "dues_payment"
        and body.dues_cycle_id is not None
        and body.related_user_id is not None
    ):
        active_plan = await session.execute(
            select(models.DuesPaymentPlan.id).where(
                models.DuesPaymentPlan.dues_cycle_id == body.dues_cycle_id,
                models.DuesPaymentPlan.user_id == body.related_user_id,
                models.DuesPaymentPlan.status == "active",
            )
        )
        if active_plan.scalar_one_or_none() is not None:
            raise conflict("member_on_payment_plan")

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

    # The 409 above USED to be a read-check-then-write, and that was wrong in exactly
    # the case this route exists to arbitrate. A treasurer and a president deciding at
    # the same moment would both read "pending", both pass the guard, and both write:
    # last-write-wins on status, decided_by naming whichever committed first, and a
    # conflict for neither. One approves, the other rejects, and the record silently
    # keeps one of them with no indication a decision was overwritten. On a
    # money-adjacent audit row that is the wrong failure mode — worse than an error,
    # because both officers walk away believing their decision stood.
    #
    # So the guard IS the write. UPDATE ... WHERE status = 'pending' lets the database
    # pick the winner, and a rowcount of 0 means someone else got there first. Same
    # shape as c51's dues reservation, c105's invite seat claim, and c91's report
    # resolution — the fourth time this pattern has been the right answer here.
    decided = await session.execute(
        update(models.SpendApproval)
        .where(
            models.SpendApproval.id == approval_id,
            models.SpendApproval.chapter_id == chapter_id,
            models.SpendApproval.status == "pending",
        )
        .values(
            status=body.status,
            decided_by=membership.user_id,
            decided_at=datetime.now(timezone.utc),
        )
        .returning(models.SpendApproval.id)
        .execution_options(synchronize_session=False)
    )
    if decided.scalar_one_or_none() is None:
        raise conflict("already_decided")

    await session.commit()
    # synchronize_session=False leaves the identity-mapped `approval` holding the
    # pre-UPDATE values, so it must be re-read before it is serialized. Without this
    # the response body would report the row as still pending.
    await session.refresh(approval)
    return SpendApprovalOut.model_validate(approval)


# ---- dues payment plans (board card c195) ----
#
# A member pays one dues cycle either in full (the Stripe/payments.py path or a
# hand-entered dues_payment ledger row) OR through a plan set up here — never both,
# which is exactly what these routes and payments.py's create_dues_payment_intent
# guard (c195 addition) enforce from both directions. Every route below is
# DUES_ADMIN except the member's own read, matching create_dues_cycle's gate: a plan
# is treasurer/president-administered, same as the cycle it pays into.


async def _corrections_by_entry(
    session: AsyncSession, chapter_id: uuid.UUID, entry_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Sum of correction amount_cents per corrected ledger entry, for exactly the
    entry ids the caller cares about (c233).

    Same corrects_entry_id join dues_status.py's dues_contributions_subquery uses
    to net a correction against its target, narrowed to a specific set of entries
    rather than "every payment in the cycle" — this is answering "is THIS
    installment's ledger row still net-positive", not a member's whole-cycle
    standing. A correction whose corrects_entry_id isn't in entry_ids (i.e. it
    targets some other ledger entry entirely) is correctly excluded by the
    .in_() filter.
    """
    if not entry_ids:
        return {}
    result = await session.execute(
        select(
            models.LedgerEntry.corrects_entry_id,
            func.coalesce(func.sum(models.LedgerEntry.amount_cents), 0),
        )
        .where(
            models.LedgerEntry.chapter_id == chapter_id,
            models.LedgerEntry.entry_type == "correction",
            models.LedgerEntry.corrects_entry_id.in_(entry_ids),
        )
        .group_by(models.LedgerEntry.corrects_entry_id)
    )
    return {corrects_entry_id: total for corrects_entry_id, total in result.all()}


def _installment_out(
    installment: models.DuesPlanInstallment, corrections_by_entry: dict[uuid.UUID, int]
) -> DuesPlanInstallmentOut:
    """Assemble one installment's response, adding effective_paid (c233) on top of
    the write-once paid_at/ledger_entry_id columns — see DuesPlanInstallmentOut's
    docstring for why both are exposed. effective_paid nets any corrections
    targeting ledger_entry_id against that entry's own amount_cents; net>0 is the
    same "money genuinely still in hand" threshold payments.py's guard and
    dues_status.py's subquery both use for "paid", applied per-installment instead
    of per-member/cycle.
    """
    effective_paid = installment.paid_at is not None
    if effective_paid and installment.ledger_entry_id is not None:
        net = installment.amount_cents + corrections_by_entry.get(
            installment.ledger_entry_id, 0
        )
        effective_paid = net > 0
    return DuesPlanInstallmentOut(
        id=installment.id,
        plan_id=installment.plan_id,
        seq=installment.seq,
        amount_cents=installment.amount_cents,
        due_date=installment.due_date,
        paid_at=installment.paid_at,
        ledger_entry_id=installment.ledger_entry_id,
        effective_paid=effective_paid,
    )


def _plan_out(
    plan: models.DuesPaymentPlan,
    installments: list[models.DuesPlanInstallment],
    corrections_by_entry: dict[uuid.UUID, int],
) -> DuesPaymentPlanOut:
    """Assemble the response shape by hand — like EventOut.rsvps, this codebase has
    no ORM relationship() wired between the two tables, only the FK column.

    corrections_by_entry is precomputed by the caller (not queried in here) so a
    multi-plan caller (list_dues_payment_plans) can fetch it ONCE across every
    plan's installments instead of once per plan — the same N+1-avoidance rule
    that function's own docstring already names for the installments query itself.
    """
    return DuesPaymentPlanOut(
        id=plan.id,
        chapter_id=plan.chapter_id,
        dues_cycle_id=plan.dues_cycle_id,
        user_id=plan.user_id,
        total_cents=plan.total_cents,
        installment_count=plan.installment_count,
        status=plan.status,
        note=plan.note,
        created_by=plan.created_by,
        created_at=plan.created_at,
        installments=[
            _installment_out(i, corrections_by_entry) for i in installments
        ],
    )


async def _load_installments(
    session: AsyncSession, plan_id: uuid.UUID
) -> list[models.DuesPlanInstallment]:
    result = await session.execute(
        select(models.DuesPlanInstallment)
        .where(models.DuesPlanInstallment.plan_id == plan_id)
        .order_by(models.DuesPlanInstallment.seq)
    )
    return list(result.scalars().all())


@router.post("/chapters/{chapter_id}/dues-cycles/{cycle_id}/plans", status_code=201)
async def create_dues_payment_plan(
    chapter_id: uuid.UUID,
    cycle_id: uuid.UUID,
    body: DuesPaymentPlanCreate,
    membership: models.Membership = Depends(require_role(*DUES_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> DuesPaymentPlanOut:
    """Set up an installment plan for one member's dues cycle; treasurer/president only.

    installments must sum to EXACTLY the cycle's amount_cents (422 otherwise) — the
    plan's total is never client-asserted, matching how create_dues_payment_intent
    always charges cycle.amount_cents rather than trusting the request body.
    installment_count must equal len(installments) (422) so the stored count can
    never silently disagree with the schedule actually written.

    409 already_paid if the member's NET standing for this cycle is still positive
    (dues_contributions_subquery, app/core/dues_status.py — the same netting
    create_dues_payment_intent's own guard uses, c232). This covers a full
    dues_payment, a completed plan's dues_installment rows, or a partial payment
    that has not been refunded back to zero — anyone still net-positive does not
    need a new plan. A member fully refunded for this cycle (net <= 0) is NOT
    blocked here, unlike payments.py's self-serve endpoint: reopening a plan for
    them is exactly the treasurer-administered path this route exists for, not the
    same-day guard change payments.py's own docstring says self-serve repayment
    still needs. Also 409 if the member already has an ACTIVE plan for it
    (uq_dues_payment_plans_active_per_member, migration 0023, is the real guard
    under concurrency — the read here only picks the honest 409 reason before the
    race), or has a LIVE self-serve reservation in flight (an open/succeeded
    DuesPaymentIntent with no ledger row yet — see the reservation check below).
    """
    cycle = await session.get(models.DuesCycle, cycle_id)
    if cycle is None or cycle.chapter_id != chapter_id:
        raise not_found("dues_cycle_not_found")

    member = await session.execute(
        select(models.Membership.id).where(
            models.Membership.chapter_id == chapter_id,
            models.Membership.user_id == body.user_id,
            models.Membership.status == "active",
        )
    )
    if member.scalar_one_or_none() is None:
        raise not_found("membership_not_found")

    if body.installment_count != len(body.installments):
        raise HTTPException(status_code=422, detail="installment_count_mismatch")
    installments_total = sum(item.amount_cents for item in body.installments)
    if installments_total != cycle.amount_cents:
        raise HTTPException(
            status_code=422, detail="installments_must_sum_to_cycle_amount"
        )

    # c232: NET the member's contributions for this cycle rather than checking raw
    # row existence. The old query matched entry_type IN ('dues_payment',
    # 'dues_installment') and 409'd the instant ANY such row existed — which is
    # right for a member who still holds the money, but wrong for one who was
    # fully refunded: their (now fully offset) dues_payment/dues_installment rows
    # still exist, so existence alone 409'd already_paid FOREVER, with no way for
    # a treasurer to ever set them up on a fresh plan. Netting (same
    # dues_contributions_subquery chapter_overview and payments.py's own guard
    # read, board c172/c195) fixes that: only a genuinely positive net — money
    # still actually in hand — blocks a new plan.
    contributions = dues_contributions_subquery(chapter_id, cycle_id)
    net_cents = await session.scalar(
        select(func.coalesce(func.sum(contributions.c.amount_cents), 0)).where(
            contributions.c.user_id == body.user_id
        )
    )
    if net_cents > 0:
        raise conflict("already_paid")

    existing_active_plan = await session.execute(
        select(models.DuesPaymentPlan.id).where(
            models.DuesPaymentPlan.dues_cycle_id == cycle_id,
            models.DuesPaymentPlan.user_id == body.user_id,
            models.DuesPaymentPlan.status == "active",
        )
    )
    if existing_active_plan.scalar_one_or_none() is not None:
        raise conflict("on_payment_plan")

    # LIVE-RESERVATION GUARD, mirroring payments.py's create_dues_payment_intent
    # reservation check (c51) exactly: a member with an in-flight self-serve
    # payment — an ACH debit still 'processing', say — has an OPEN
    # DuesPaymentIntent and NO ledger row yet, so neither guard above can see them.
    # Without this, a plan gets created underneath the in-flight payment; the ACH
    # later settles into a full dues_payment AND the treasurer keeps recording
    # installments on top of it, over-collecting the cycle. uq_dues_intent_live
    # caps this at exactly one open/succeeded row per (cycle, member), so
    # scalar_one_or_none() is safe here without a LIMIT, same as payments.py's own
    # use of this query shape.
    live_reservation = await session.execute(
        select(models.DuesPaymentIntent.id).where(
            models.DuesPaymentIntent.dues_cycle_id == cycle_id,
            models.DuesPaymentIntent.user_id == body.user_id,
            models.DuesPaymentIntent.status.in_(("open", "succeeded")),
        )
    )
    if live_reservation.scalar_one_or_none() is not None:
        raise conflict("payment_in_progress")

    plan = models.DuesPaymentPlan(
        chapter_id=chapter_id,
        dues_cycle_id=cycle_id,
        user_id=body.user_id,
        total_cents=cycle.amount_cents,
        installment_count=body.installment_count,
        note=body.note,
        created_by=membership.user_id,
    )
    session.add(plan)
    await session.flush()  # assign plan.id for the installment rows below

    for seq, item in enumerate(body.installments, start=1):
        session.add(
            models.DuesPlanInstallment(
                plan_id=plan.id,
                seq=seq,
                amount_cents=item.amount_cents,
                due_date=item.due_date,
            )
        )

    try:
        await session.commit()
    except IntegrityError:
        # The read above narrows the race but is not the guard — two concurrent
        # create-plan calls for the same (cycle, member) can both pass it; only one
        # wins uq_dues_payment_plans_active_per_member (migration 0023), same shape
        # as c51's uq_dues_intent_live.
        await session.rollback()
        raise conflict("on_payment_plan") from None

    await session.refresh(plan)
    # Freshly created installments have no ledger_entry_id yet (none has been paid),
    # so there is nothing to net — corrections_by_entry is trivially empty.
    return _plan_out(plan, await _load_installments(session, plan.id), {})


@router.post(
    "/chapters/{chapter_id}/dues-plans/{plan_id}/installments/{seq}/record-payment"
)
async def record_dues_installment_payment(
    chapter_id: uuid.UUID,
    plan_id: uuid.UUID,
    seq: int,
    body: DuesInstallmentRecordPaymentRequest,
    membership: models.Membership = Depends(require_role(*DUES_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> DuesPlanInstallmentOut:
    """Record one installment as paid; treasurer/president only.

    Appends a NEW entry_type='dues_installment' ledger row — never 'dues_payment',
    which uq_ledger_dues_payment_once (migration 0010) already limits to at most one
    per (cycle, member) EVER and an installment plan needs several. Marks the plan
    'completed' once every installment has a paid_at.

    THE GUARD IS THE WRITE, same shape as decide_spend_approval directly above and
    join_chapter's invite-seat claim: a conditional UPDATE on paid_at IS NULL, not a
    read-check-then-write, so two treasurers recording the same installment at once
    cannot both succeed and post two ledger rows for one installment. Recording an
    already-paid installment 409s.

    409 plan_not_active if the plan is not 'active' (canceled or already completed).
    Without this, a CANCELED plan's installments are still paid_at NULL and fully
    recordable — recording one posts a dues_installment ledger row the cancellation
    was meant to stop, and once the LAST one is recorded the completion check below
    would flip status back to 'completed', resurrecting a plan the treasurer
    explicitly killed. Checked before the paid_at guard so a canceled plan's reason
    is specific (plan_not_active), not the generic installment_already_paid a
    fully-paid plan would otherwise also produce.
    """
    plan = await session.get(models.DuesPaymentPlan, plan_id)
    if plan is None or plan.chapter_id != chapter_id:
        raise not_found("dues_payment_plan_not_found")
    if plan.status != "active":
        raise conflict("plan_not_active")

    installment_result = await session.execute(
        select(models.DuesPlanInstallment).where(
            models.DuesPlanInstallment.plan_id == plan_id,
            models.DuesPlanInstallment.seq == seq,
        )
    )
    installment = installment_result.scalar_one_or_none()
    if installment is None:
        raise not_found("dues_plan_installment_not_found")

    claimed = await session.execute(
        update(models.DuesPlanInstallment)
        .where(
            models.DuesPlanInstallment.id == installment.id,
            models.DuesPlanInstallment.paid_at.is_(None),
        )
        .values(paid_at=datetime.now(timezone.utc))
        .returning(models.DuesPlanInstallment.id)
        .execution_options(synchronize_session=False)
    )
    if claimed.scalar_one_or_none() is None:
        raise conflict("installment_already_paid")
    # synchronize_session=False left the identity-mapped `installment` holding the
    # pre-UPDATE paid_at (None) — re-read before this request writes anything else
    # to it or serializes it, same reasoning as decide_spend_approval's refresh.
    await session.refresh(installment)

    description = f"Dues installment {seq}/{plan.installment_count}"
    if body.note:
        description = f"{description} ({body.note})"
    entry = models.LedgerEntry(
        chapter_id=chapter_id,
        entry_type="dues_installment",
        amount_cents=installment.amount_cents,
        category="dues",
        description=description,
        related_user_id=plan.user_id,
        dues_cycle_id=plan.dues_cycle_id,
        created_by=membership.user_id,
    )
    session.add(entry)
    await session.flush()  # assign entry.id
    installment.ledger_entry_id = entry.id

    remaining_unpaid = await session.scalar(
        select(func.count())
        .select_from(models.DuesPlanInstallment)
        .where(
            models.DuesPlanInstallment.plan_id == plan_id,
            models.DuesPlanInstallment.paid_at.is_(None),
        )
    )
    if remaining_unpaid == 0:
        plan.status = "completed"

    await session.commit()
    await session.refresh(installment)
    # No correction can exist yet against an entry this same request just created
    # (corrects_entry_id would have to reference a row that didn't exist a moment
    # ago), so effective_paid is trivially True here — {} skips the query.
    return _installment_out(installment, {})


@router.get("/chapters/{chapter_id}/dues-cycles/{cycle_id}/plans")
async def list_dues_payment_plans(
    chapter_id: uuid.UUID,
    cycle_id: uuid.UUID,
    _membership: models.Membership = Depends(require_role(*DUES_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> list[DuesPaymentPlanOut]:
    """Every plan against this cycle, any status, newest first; treasurer/president only."""
    plans_result = await session.execute(
        select(models.DuesPaymentPlan)
        .where(
            models.DuesPaymentPlan.chapter_id == chapter_id,
            models.DuesPaymentPlan.dues_cycle_id == cycle_id,
        )
        .order_by(models.DuesPaymentPlan.created_at.desc())
    )
    plans = list(plans_result.scalars().all())
    if not plans:
        return []

    # ONE query for every plan's installments, not one per plan — the same
    # N+1-avoidance rule chapter_overview's docstring names (c82/c156).
    installments_result = await session.execute(
        select(models.DuesPlanInstallment)
        .where(models.DuesPlanInstallment.plan_id.in_([p.id for p in plans]))
        .order_by(models.DuesPlanInstallment.seq)
    )
    installments_by_plan: dict[uuid.UUID, list[models.DuesPlanInstallment]] = {}
    all_installments: list[models.DuesPlanInstallment] = []
    for installment in installments_result.scalars().all():
        installments_by_plan.setdefault(installment.plan_id, []).append(installment)
        all_installments.append(installment)

    # ONE corrections query across every plan's installments, not one per plan —
    # same N+1-avoidance rule as the installments query just above (c233).
    entry_ids = [
        i.ledger_entry_id for i in all_installments if i.ledger_entry_id is not None
    ]
    corrections_by_entry = await _corrections_by_entry(session, chapter_id, entry_ids)

    return [
        _plan_out(plan, installments_by_plan.get(plan.id, []), corrections_by_entry)
        for plan in plans
    ]


@router.get("/chapters/{chapter_id}/dues-cycles/{cycle_id}/plans/mine")
async def get_my_dues_payment_plan(
    chapter_id: uuid.UUID,
    cycle_id: uuid.UUID,
    membership: models.Membership = Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
) -> DuesPaymentPlanOut:
    """The caller's own plan for this cycle, whatever its status; any active member.

    Newest first + limit 1 rather than filtering to status='active': a member whose
    plan just completed (or was canceled) should still be able to read it here
    rather than get a 404 the moment it stops being active.
    """
    plan = await session.scalar(
        select(models.DuesPaymentPlan)
        .where(
            models.DuesPaymentPlan.chapter_id == chapter_id,
            models.DuesPaymentPlan.dues_cycle_id == cycle_id,
            models.DuesPaymentPlan.user_id == membership.user_id,
        )
        .order_by(models.DuesPaymentPlan.created_at.desc())
        .limit(1)
    )
    if plan is None:
        raise not_found("dues_payment_plan_not_found")
    installments = await _load_installments(session, plan.id)
    entry_ids = [i.ledger_entry_id for i in installments if i.ledger_entry_id is not None]
    corrections_by_entry = await _corrections_by_entry(session, chapter_id, entry_ids)
    return _plan_out(plan, installments, corrections_by_entry)


@router.post("/chapters/{chapter_id}/dues-plans/{plan_id}/cancel")
async def cancel_dues_payment_plan(
    chapter_id: uuid.UUID,
    plan_id: uuid.UUID,
    _membership: models.Membership = Depends(require_role(*DUES_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> DuesPaymentPlanOut:
    """Cancel an active plan; treasurer/president only. 409 if it is not active
    (already completed, or already canceled) — same conditional-UPDATE-as-guard
    shape as decide_spend_approval, so two officers cancelling at once cannot both
    believe their action was the one that stuck."""
    plan = await session.get(models.DuesPaymentPlan, plan_id)
    if plan is None or plan.chapter_id != chapter_id:
        raise not_found("dues_payment_plan_not_found")

    canceled = await session.execute(
        update(models.DuesPaymentPlan)
        .where(
            models.DuesPaymentPlan.id == plan_id,
            models.DuesPaymentPlan.chapter_id == chapter_id,
            models.DuesPaymentPlan.status == "active",
        )
        .values(status="canceled")
        .returning(models.DuesPaymentPlan.id)
        .execution_options(synchronize_session=False)
    )
    if canceled.scalar_one_or_none() is None:
        raise conflict("plan_not_active")

    await session.commit()
    await session.refresh(plan)
    installments = await _load_installments(session, plan.id)
    entry_ids = [i.ledger_entry_id for i in installments if i.ledger_entry_id is not None]
    corrections_by_entry = await _corrections_by_entry(session, chapter_id, entry_ids)
    return _plan_out(plan, installments, corrections_by_entry)
