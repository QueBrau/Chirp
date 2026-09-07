"""Payments: Stripe Connect onboarding, dues PaymentIntents, and the webhook sink."""
import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.config import get_settings
from app.core.analytics import emit
from app.core.dues_status import dues_contributions_subquery
from app.core.errors import conflict, forbidden, is_cross_table_dues_guard_conflict, not_found
from app.core.permissions import Role, require_role
from app.db import get_session
from app.middleware.auth import get_current_user
from app.middleware.org_scope import get_current_membership
from app.schemas.payments import (
    ChapterPaymentsStatusOut,
    ConnectOnboardingOut,
    ConnectOnboardingRequest,
    DuesIntentCreate,
    DuesIntentOut,
)
from app.services import stripe_service
from app.services.settlement_binding import BoundSettlement, bind_settlement

router = APIRouter(tags=["payments"])
logger = logging.getLogger(__name__)

# c234: how long an 'open' reservation can sit unresolved before it is treated as
# abandoned rather than a live rail-lock. This is a PRODUCT choice, not a Stripe
# constraint — c231's per-reservation idempotency nonce means a superseded
# reservation can no longer resurrect a cached Stripe intent, so there is no cache
# window to protect here. 24h is chosen because it covers a member who steps away
# mid-checkout (spotty connection, distracted, waiting on a bank prompt) and comes
# back later the same day or the next morning to finish on the SAME rail, while
# still being short enough that a genuinely abandoned attempt does not lock a
# member out of switching rails, or retrying at all, for days. It intentionally
# matches the number Stripe's own idempotency window used to use — not because the
# two interact anymore, but because a second, unrelated meaning for "24h" here
# would be a needless surprise to whoever reads this next.
RESERVATION_TTL = timedelta(hours=24)

# c234: PaymentIntent statuses that mean Stripe has moved past "waiting on the
# member" — a same-rail retrieve landing on one of these must not hand the client
# a client_secret that LOOKS like a fresh checkout.
_INTENT_NOT_AWAITING_PAYMENT = {"processing", "succeeded"}


def _onboarding_urls() -> tuple[str, str]:
    """(return_url, refresh_url) for Connect onboarding.

    Stripe rejects custom schemes, so these cannot be chirp:// deep links — they
    point at APP_PUBLIC_BASE_URL, which is expected to bounce the member back into
    the app. 503 rather than sending Stripe a URL we know is wrong.
    """
    base = get_settings().app_public_base_url
    if not base:
        raise HTTPException(status_code=503, detail="app_public_base_url_not_configured")
    base = base.rstrip("/")
    return f"{base}/stripe/connect/return", f"{base}/stripe/connect/refresh"


@router.post("/payments/connect/onboarding-link")
async def create_connect_onboarding_link(
    body: ConnectOnboardingRequest,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ConnectOnboardingOut:
    """Start (or resume) Stripe Connect onboarding for a chapter; treasurer/president.

    Creates the Express account on first call and reuses it afterwards, so a member
    who abandons onboarding halfway resumes the same account instead of orphaning it.
    """
    result = await session.execute(
        select(models.Membership.role).where(
            models.Membership.user_id == user.id,
            models.Membership.chapter_id == body.chapter_id,
            models.Membership.status == "active",
        )
    )
    role = result.scalar_one_or_none()
    if role is None:
        raise forbidden("not_a_member")
    if role not in (Role.treasurer.value, Role.president.value):
        raise forbidden("insufficient_role")

    chapter = await session.get(models.Chapter, body.chapter_id)
    if chapter is None:
        raise not_found("chapter_not_found")

    account_id = chapter.stripe_account_id
    if account_id is None:
        account_id = await stripe_service.create_express_account(
            chapter.id, chapter.org_name
        )
        chapter.stripe_account_id = account_id
        await session.commit()

    return_url, refresh_url = _onboarding_urls()
    link = await stripe_service.create_account_link(account_id, return_url, refresh_url)
    return ConnectOnboardingOut(
        url=link.url,
        expires_at=datetime.fromtimestamp(link.expires_at, tz=timezone.utc),
    )


@router.get("/chapters/{chapter_id}/payments/status")
async def get_chapter_payments_status(
    chapter_id: uuid.UUID,
    _membership: models.Membership = Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
) -> ChapterPaymentsStatusOut:
    """Whether the chapter can accept dues yet; read live from Stripe, any member.

    Status is not mirrored into our tables on purpose: onboarding state changes on
    Stripe's side (verification, document review) with no request to us, so a stored
    copy would go stale and tell members dues are payable when they are not.
    """
    chapter = await session.get(models.Chapter, chapter_id)
    if chapter is None:
        raise not_found("chapter_not_found")
    if chapter.stripe_account_id is None:
        return ChapterPaymentsStatusOut(
            onboarded=False, charges_enabled=False, details_submitted=False
        )

    account = await stripe_service.retrieve_account(chapter.stripe_account_id)
    charges_enabled = bool(account.charges_enabled)
    details_submitted = bool(account.details_submitted)
    return ChapterPaymentsStatusOut(
        onboarded=charges_enabled and details_submitted,
        charges_enabled=charges_enabled,
        details_submitted=details_submitted,
    )


async def _get_or_create_customer(
    session: AsyncSession, user: models.User, chapter_id: uuid.UUID, account_id: str
) -> str:
    """Stripe Customer for this (member, chapter) pair, creating it on first payment."""
    existing = await session.get(
        models.ChapterStripeCustomer, {"user_id": user.id, "chapter_id": chapter_id}
    )
    if existing is not None:
        return existing.stripe_customer_id

    customer_id = await stripe_service.create_customer(
        account_id, user.email, user.display_name
    )
    session.add(
        models.ChapterStripeCustomer(
            user_id=user.id, chapter_id=chapter_id, stripe_customer_id=customer_id
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        # Concurrent first payment (double-tap, or a client retry on a flaky
        # connection) raced us to insert the same (user, chapter) row. Defer to
        # the winner rather than 500ing; our own Stripe Customer is left unused
        # on the connected account, which is harmless and inert.
        await session.rollback()
        winner = await session.get(
            models.ChapterStripeCustomer, {"user_id": user.id, "chapter_id": chapter_id}
        )
        if winner is None:
            raise
        return winner.stripe_customer_id
    return customer_id


@router.post("/payments/dues/{cycle_id}/intent")
async def create_dues_payment_intent(
    cycle_id: uuid.UUID,
    body: DuesIntentCreate,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> DuesIntentOut:
    """Create a dues PaymentIntent for the caller; active members of the cycle's chapter.

    The amount comes from the dues cycle, never the client. The rail arrives in the
    body because application_fee_amount is fixed at creation time.
    """
    cycle = await session.get(models.DuesCycle, cycle_id)
    if cycle is None:
        raise not_found("dues_cycle_not_found")

    membership = await session.execute(
        select(models.Membership.id).where(
            models.Membership.user_id == user.id,
            models.Membership.chapter_id == cycle.chapter_id,
            models.Membership.status == "active",
        )
    )
    if membership.scalar_one_or_none() is None:
        raise forbidden("not_a_member")

    # c195: a member on an ACTIVE payment plan pays this cycle in installments, not
    # through this self-serve full-cycle charge path. Checked BEFORE the existence
    # guard below so the reason surfaced is specific (on_payment_plan) rather than
    # the generic already_paid/refunded_contact_treasurer split, which assumes a
    # single lump-sum obligation and would be a confusing lie to a plan member who
    # has paid some, but not all, of what they owe. The mobile client is expected to
    # gate this itself (it shows the installment schedule instead of a pay button for
    # a plan member), but that is a client-side courtesy, not the security boundary —
    # this is.
    active_plan = await session.execute(
        select(models.DuesPaymentPlan.id).where(
            models.DuesPaymentPlan.dues_cycle_id == cycle_id,
            models.DuesPaymentPlan.user_id == user.id,
            models.DuesPaymentPlan.status == "active",
        )
    )
    if active_plan.scalar_one_or_none() is not None:
        raise conflict("on_payment_plan")

    # EXISTENCE still blocks the charge path (board c172) — re-payment for one dues
    # cycle is structurally UNREPRESENTABLE in this ledger today, not merely
    # undesirable: uq_ledger_dues_payment_once allows at most one dues_payment row
    # per (cycle, member) EVER, so a second Stripe payment settling here would
    # capture real money at Stripe and then silently have no ledger row to show for
    # it when the webhook's insert loses to that constraint (see stripe_webhook
    # and the RESIDUAL EDGE note below). An earlier version of this fix let net<=0
    # (a full refund) through to Stripe, which is exactly the money-loss path — closed
    # by going back to existence, not by NOT netting at all: dues_contributions_subquery
    # (same definition chapter_overview reads, board c171) still decides WHICH honest
    # reason accompanies the block, so the two surfaces agree on whether the member
    # owes money even though this endpoint cannot yet let them pay again. Actually
    # reopening self-serve repayment after a refund needs the cycle/member's dues
    # status modeled explicitly (c83-shaped work), not a same-day guard change.
    #
    # c195 ADDS 'dues_installment' to this existence check's entry_type filter. A
    # member who FINISHED a payment plan has entry_type='dues_installment' rows on
    # the ledger and — because they never paid through this lump-sum path — NO
    # dues_payment row. Leaving this filtered to 'dues_payment' alone would silently
    # reopen exactly the double-charge this whole guard exists to close: the active-
    # plan check above only catches an IN-PROGRESS plan, so a completed one would
    # sail through both checks and let this route create a second, full-price Stripe
    # intent for a cycle already paid off in installments. Matching both types here
    # reuses the existing net-based reason split below rather than inventing a new
    # one — dues_contributions_subquery already nets both types together (c195,
    # app/core/dues_status.py), so a completed plan's net reads as >= the cycle
    # amount and this correctly resolves to already_paid.
    # LIMIT 1: unlike a bare 'dues_payment' row (uq_ledger_dues_payment_once caps that
    # at exactly one per cycle/member, so scalar_one_or_none() used to be safe on its
    # own), a member can have SEVERAL 'dues_installment' rows for one cycle — one per
    # installment recorded. Without the limit, scalar_one_or_none() raises
    # MultipleResultsFound the moment a plan member has paid a second installment;
    # this query only needs to know whether at least one qualifying row exists.
    existing_payment = await session.execute(
        select(models.LedgerEntry.id)
        .where(
            models.LedgerEntry.dues_cycle_id == cycle_id,
            models.LedgerEntry.related_user_id == user.id,
            models.LedgerEntry.entry_type.in_(("dues_payment", "dues_installment")),
        )
        .limit(1)
    )
    if existing_payment.scalar_one_or_none() is not None:
        contributions = dues_contributions_subquery(cycle.chapter_id, cycle.id)
        net_cents = await session.scalar(
            select(func.coalesce(func.sum(contributions.c.amount_cents), 0)).where(
                contributions.c.user_id == user.id
            )
        )
        # net > 0: the money is genuinely still in hand — "already_paid" (unchanged).
        # net <= 0: a correction refunded it, and the member DOES owe again, but this
        # endpoint cannot self-serve that yet — say so rather than repeating the
        # already_paid lie the President overview no longer tells.
        raise conflict("already_paid" if net_cents > 0 else "refunded_contact_treasurer")

    chapter = await session.get(models.Chapter, cycle.chapter_id)
    if chapter is None or chapter.stripe_account_id is None:
        raise conflict("chapter_not_onboarded")
    account_id = chapter.stripe_account_id
    account = await stripe_service.retrieve_account(account_id)
    if not account.charges_enabled:
        raise conflict("chapter_not_onboarded")

    customer_id = await _get_or_create_customer(session, user, chapter.id, account_id)

    # RESERVE BEFORE CHARGING (c51). The already_paid check above can only see
    # SETTLED payments, so it cannot stop a member re-paying a cycle whose ACH
    # debit is still processing — and because the Stripe idempotency key is
    # per-rail, that retry would mint a genuinely different PaymentIntent.
    # uq_dues_intent_live makes the database, not Stripe, arbitrate: the second
    # attempt loses here, before any money moves.
    live = await session.execute(
        select(models.DuesPaymentIntent).where(
            models.DuesPaymentIntent.dues_cycle_id == cycle.id,
            models.DuesPaymentIntent.user_id == user.id,
            models.DuesPaymentIntent.status.in_(("open", "succeeded")),
        )
    )
    reservation = live.scalar_one_or_none()
    if (
        reservation is not None
        and reservation.status == "open"
        and reservation.created_at < datetime.now(timezone.utc) - RESERVATION_TTL
    ):
        # Maybe abandoned (c234): never resolved by a webhook and long past any
        # reasonable window for the member to still be mid-checkout. AGE ALONE IS
        # NOT PROOF OF ABANDONMENT (adversarial-review catch, c234 amendment): an
        # ACH payment ordinarily sits in 'processing' for 1-3 business days -
        # longer than this TTL - and a lost webhook can leave a succeeded intent
        # looking 'open' here indefinitely. Expiring either and minting a fresh
        # intent charges the member TWICE at Stripe, with the second capture only
        # ever surfacing as the settlement reconciliation log's ERROR line.
        #
        # So Stripe's cancel is used as the TEST, not as best-effort cleanup:
        # Stripe accepts cancellation exactly in the states where no money is
        # moving (requires_payment_method / _confirmation / _action) and refuses
        # it once the money is in motion (processing, succeeded). Only a cancel
        # Stripe accepts - or an intent it reports already canceled - releases
        # this reservation. Anything else keeps the row: a stuck rail switch is
        # a recoverable 409; a second real charge is not.
        expire = True
        if reservation.stripe_payment_intent_id is not None:
            try:
                await stripe_service.cancel_payment_intent(
                    account_id, reservation.stripe_payment_intent_id
                )
            except Exception:
                expire = False
                try:
                    stale = await stripe_service.retrieve_payment_intent(
                        account_id, reservation.stripe_payment_intent_id
                    )
                    if stale.status == "canceled":
                        expire = True
                except Exception:
                    logger.warning(
                        "c234: could not determine state of aged intent %s "
                        "(reservation %s); keeping the reservation - erring "
                        "toward a 409 over a possible double charge",
                        reservation.stripe_payment_intent_id,
                        reservation.id,
                    )
        if expire:
            # 'expired' is not a value the status CHECK constraint allows
            # (migration 0010), so this reuses 'canceled' - the same retryable
            # bucket stripe_webhook already puts a genuinely failed/canceled
            # payment in.
            reservation.status = "canceled"
            reservation.updated_at = datetime.now(timezone.utc)
            await session.commit()
            reservation = None

    if reservation is not None:
        if reservation.status == "succeeded":
            # Effectively unreachable in normal operation (board c172): a reservation
            # only reaches 'succeeded' via the webhook's stripe_webhook, which
            # commits in the SAME transaction as stripe_webhook's ledger insert
            # — so whenever this is true, the existence check above has already
            # raised (with the honest already_paid/refunded_contact_treasurer split)
            # before this line runs. Left as a defensive backstop rather than removed,
            # matching uq_ledger_dues_payment_once's own "independent of the
            # reservation" backstop reasoning (migration 0010).
            raise conflict("already_paid")
        if reservation.rail != body.rail:
            # THE double-charge case: an ACH debit is still processing (days) and
            # the member is now trying to pay the same cycle by card. The per-rail
            # Stripe idempotency key would happily mint a second real intent.
            raise conflict("payment_already_in_progress")
        # Same rail — an ordinary client retry. Handled below: reused if we already
        # have a stored intent id, created if we do not.
    else:
        reservation = models.DuesPaymentIntent(
            chapter_id=chapter.id,
            dues_cycle_id=cycle.id,
            user_id=user.id,
            rail=body.rail,
            # c349: snapshot what this intent is being created FOR, in the same
            # transaction as the reservation. The webhook binds settlement to these
            # two values (migration 0033) rather than to the cycle or the event.
            amount_cents=cycle.amount_cents,
            currency=stripe_service.CURRENCY,
        )
        session.add(reservation)
        try:
            await session.commit()
        except IntegrityError:
            # Concurrent first attempt won the race. uq_dues_intent_live, not the
            # check above, is what actually makes this safe under concurrency.
            await session.rollback()
            raise conflict("payment_already_in_progress") from None
        except DBAPIError as exc:
            # c230: cross_table_dues_guard_intents (migration 0028) — a treasurer
            # committed an ACTIVE payment plan for this (cycle, member) between
            # the active_plan read-guard above and this INSERT actually landing.
            # Same cross-table TOCTOU that read-guard exists to close, now
            # backstopped at the database; raise the identical 409 the read-guard
            # would have given had it run a moment later. Any OTHER DBAPIError
            # (not this trigger) re-raises untouched — this is a backstop for one
            # specific conflict, not a blanket swallow.
            await session.rollback()
            if not is_cross_table_dues_guard_conflict(exc):
                raise
            raise conflict("on_payment_plan") from None

    payment_intent_status = "awaiting_payment"
    if reservation.stripe_payment_intent_id is not None:
        # A same-rail retry against an intent we already created (board c193). Stripe
        # only retains an idempotency key for 24h, while ACH can sit in 'processing'
        # for DAYS with no payment_intent.processing webhook to move the reservation
        # out of 'open' in the meantime — a create() call here past that window would
        # mint a genuinely NEW real intent (a second bank debit) instead of resolving
        # to the original. Retrieve, never create, once an intent id is on file.
        intent = await stripe_service.retrieve_payment_intent(
            account_id, reservation.stripe_payment_intent_id
        )
        if intent.status in _INTENT_NOT_AWAITING_PAYMENT:
            # c234: Stripe moved past "waiting on the member" while the client was
            # away (settled, or an ACH debit mid-flight) and no webhook has resolved
            # this reservation yet. payment_intent_client_secret below is still the
            # real one Stripe issued — harmless to hand back — but it must not be
            # presented as a fresh checkout; the client is expected to read this
            # field instead of blindly reopening PaymentSheet.
            payment_intent_status = intent.status
    else:
        # Either a freshly-inserted reservation (the else branch above), or one that
        # exists but has not gotten a Stripe answer yet — either way there is no
        # intent to retrieve, so this is the only branch allowed to create one, and
        # therefore the only branch allowed to cancel the reservation if Stripe
        # rejects the call.
        try:
            intent = await stripe_service.create_dues_payment_intent(
                account_id=account_id,
                customer_id=customer_id,
                amount_cents=reservation.amount_cents,
                currency=reservation.currency,
                rail=body.rail,
                cycle_id=cycle.id,
                user_id=user.id,
                chapter_id=chapter.id,
                # c231: nonced per reservation row, not just (cycle, member, rail) —
                # see create_dues_payment_intent's docstring for why a bare
                # (cycle, member, rail) key let a declined-card retry's FRESH
                # reservation collide with the dead one it superseded.
                reservation_id=reservation.id,
            )
        except Exception:
            # Stripe never created an intent, so the reservation must not keep
            # blocking a legitimate retry. Safe ONLY here: stripe_payment_intent_id
            # was still None going in, so this reservation was never live at Stripe.
            # A reservation that already points at a live intent must NEVER be
            # canceled from an exception — that would release uq_dues_intent_live
            # and reopen the cross-rail double-charge this guard exists to close.
            reservation.status = "canceled"
            await session.commit()
            raise

        reservation_id = reservation.id
        try:
            reservation.stripe_payment_intent_id = intent.id
            await session.commit()
        except IntegrityError:
            # Belt, not the primary fix (c231): uq_dues_intent_stripe_id spans every
            # status, so if the id Stripe just handed back is already claimed by a
            # DIFFERENT reservation row, this commit loses the race instead of
            # silently stealing it. Expected to be unreachable once c231's per-
            # reservation nonce above is fully rolled out; kept as a belt for the
            # deploy window where an old-format (cycle, member, rail) key can still
            # be live in Stripe's 24h idempotency cache and get replayed by an
            # instance still running the old code. This reservation was never live
            # at Stripe under that id — still the create branch, so canceling it is
            # safe per the invariant above — and the client's next retry reserves a
            # fresh row with a fresh idempotency key rather than hitting a 500.
            await session.rollback()
            stale = await session.get(models.DuesPaymentIntent, reservation_id)
            if stale is not None:
                stale.status = "canceled"
                stale.updated_at = datetime.now(timezone.utc)
                await session.commit()
            raise conflict("payment_intent_conflict") from None

        # c227 (skeptic catch): emitted HERE, inside the create branch after the
        # intent id committed, never after the branches converge - the retrieve
        # path re-serves an existing intent on every same-rail retry/poll (for
        # ACH, across days), and an emit there overcounts "intents created" by
        # however many times the member reopens the screen.
        emit(
            "payment_intent_created",
            chapter_id=chapter.id,
            cycle_id=cycle.id,
            user_id=user.id,
            rail=body.rail,
        )

    customer_session_secret = await stripe_service.create_customer_session(
        account_id, customer_id
    )
    return DuesIntentOut(
        payment_intent_client_secret=intent.client_secret,
        customer_session_client_secret=customer_session_secret,
        customer_id=customer_id,
        publishable_key=stripe_service.publishable_key(),
        stripe_account_id=account_id,
        amount_cents=reservation.amount_cents,
        application_fee_cents=stripe_service.platform_fee_cents(
            reservation.amount_cents, body.rail
        ),
        rail=body.rail,
        payment_intent_status=payment_intent_status,
    )


def _emit_stripe_webhook_event(event_type: str, bound: BoundSettlement) -> None:
    """Emit only committed, first-time transitions, using database-owned identity."""
    if event_type not in ("payment_intent.succeeded", "payment_intent.payment_failed"):
        return
    reservation = bound.reservation
    emit(
        "payment_succeeded" if event_type == "payment_intent.succeeded" else "payment_failed",
        event_type=event_type,
        rail=reservation.rail,
        cycle_id=str(reservation.dues_cycle_id),
        user_id=str(reservation.user_id),
    )


@router.post("/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Authenticate then bind settlement to its stored reservation (c349).

    The event receipt, quarantine or ledger change commit together. Only expected
    uniqueness conflicts are acknowledged; failed database writes remain retryable.
    No raw payload, customer details, or secret enters logs or quarantine.
    """
    if not stripe_signature:
        raise HTTPException(status_code=400, detail="missing_stripe_signature")
    event = stripe_service.verify_webhook_event(await request.body(), stripe_signature)

    receipt = await session.scalar(
        pg_insert(models.ProcessedStripeEvent)
        .values(event_id=event["id"], event_type=event["type"])
        .on_conflict_do_nothing(index_elements=["event_id"])
        .returning(models.ProcessedStripeEvent.event_id)
    )
    if receipt is None:
        await session.rollback()
        return {"received": True}

    statuses = {
        "payment_intent.succeeded": "succeeded",
        "payment_intent.payment_failed": "failed",
        "payment_intent.canceled": "canceled",
    }
    event_type = event["type"]
    bound = await bind_settlement(session, event) if event_type in statuses else None
    changed = False
    reconciliation = None
    if bound is not None:
        reservation, cycle = bound.reservation, bound.cycle
        if event_type == "payment_intent.succeeded":
            # Both unique payment indexes arbitrate concurrent settlement. All
            # non-unique failures propagate and roll back the event receipt too.
            recorded = await session.scalar(
                pg_insert(models.LedgerEntry).values(
                    chapter_id=reservation.chapter_id, entry_type="dues_payment",
                    amount_cents=reservation.amount_cents, category="dues",
                    description=cycle.name, related_user_id=reservation.user_id,
                    dues_cycle_id=reservation.dues_cycle_id,
                    stripe_payment_intent_id=reservation.stripe_payment_intent_id,
                    created_by=reservation.user_id,
                ).on_conflict_do_nothing().returning(models.LedgerEntry.id)
            )
            if recorded is not None:
                reservation.status = "succeeded"
                reservation.updated_at = datetime.now(timezone.utc)
                changed = True
            else:
                recorded_intent = await session.scalar(
                    select(models.LedgerEntry.stripe_payment_intent_id).where(
                        models.LedgerEntry.dues_cycle_id == reservation.dues_cycle_id,
                        models.LedgerEntry.related_user_id == reservation.user_id,
                        models.LedgerEntry.entry_type == "dues_payment",
                    )
                )
                if recorded_intent != reservation.stripe_payment_intent_id:
                    reconciliation = (
                        str(reservation.dues_cycle_id), str(reservation.user_id),
                        reservation.stripe_payment_intent_id, recorded_intent,
                    )
        elif reservation.status != "succeeded":
            # An out-of-order failure must never release a successfully paid cycle.
            changed = reservation.status != statuses[event_type]
            reservation.status = statuses[event_type]
            reservation.updated_at = datetime.now(timezone.utc)

    await session.commit()
    if reconciliation is not None:
        logger.error(
            "dues payment reconciliation: cycle=%s user=%s intent=%s captured but "
            "NOT recorded on the ledger; recorded intent=%s. Verify both against "
            "Stripe and reconcile manually.", *reconciliation,
        )
    if changed and bound is not None:
        _emit_stripe_webhook_event(event_type, bound)
    return {"received": True}
