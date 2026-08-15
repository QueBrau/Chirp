"""Payments: Stripe Connect onboarding, dues PaymentIntents, and the webhook sink."""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.config import get_settings
from app.core.errors import conflict, forbidden, not_found
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

router = APIRouter(tags=["payments"])


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

    already_paid = await session.execute(
        select(models.LedgerEntry.id).where(
            models.LedgerEntry.dues_cycle_id == cycle_id,
            models.LedgerEntry.related_user_id == user.id,
            models.LedgerEntry.entry_type == "dues_payment",
        )
    )
    if already_paid.scalar_one_or_none() is not None:
        raise conflict("already_paid")

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
    if reservation is not None:
        if reservation.status == "succeeded":
            raise conflict("already_paid")
        if reservation.rail != body.rail:
            # THE double-charge case: an ACH debit is still processing (days) and
            # the member is now trying to pay the same cycle by card. The per-rail
            # Stripe idempotency key would happily mint a second real intent.
            raise conflict("payment_already_in_progress")
        # Same rail — an ordinary client retry. Reuse the reservation; the
        # idempotency key is identical, so Stripe resolves it to the same intent.
    else:
        reservation = models.DuesPaymentIntent(
            chapter_id=chapter.id, dues_cycle_id=cycle.id, user_id=user.id, rail=body.rail
        )
        session.add(reservation)
        try:
            await session.commit()
        except IntegrityError:
            # Concurrent first attempt won the race. uq_dues_intent_live, not the
            # check above, is what actually makes this safe under concurrency.
            await session.rollback()
            raise conflict("payment_already_in_progress") from None

    try:
        intent = await stripe_service.create_dues_payment_intent(
            account_id=account_id,
            customer_id=customer_id,
            amount_cents=cycle.amount_cents,
            rail=body.rail,
            cycle_id=cycle.id,
            user_id=user.id,
            chapter_id=chapter.id,
        )
    except Exception:
        # Stripe never created an intent, so the reservation must not keep
        # blocking a legitimate retry.
        reservation.status = "canceled"
        await session.commit()
        raise

    reservation.stripe_payment_intent_id = intent.id
    await session.commit()
    customer_session_secret = await stripe_service.create_customer_session(
        account_id, customer_id
    )
    return DuesIntentOut(
        payment_intent_client_secret=intent.client_secret,
        customer_session_client_secret=customer_session_secret,
        customer_id=customer_id,
        publishable_key=stripe_service.publishable_key(),
        stripe_account_id=account_id,
        amount_cents=cycle.amount_cents,
        application_fee_cents=stripe_service.platform_fee_cents(
            cycle.amount_cents, body.rail
        ),
        rail=body.rail,
    )


async def _resolve_reservation(
    session: AsyncSession, intent: dict, status: str
) -> None:
    """Move a dues reservation out of (or into) its terminal state (c51).

    'succeeded' keeps holding uq_dues_intent_live — the cycle is paid, so a second
    payment must stay blocked. 'failed'/'canceled' release it, because a member
    whose payment genuinely failed has to be able to try again.
    """
    result = await session.execute(
        select(models.DuesPaymentIntent).where(
            models.DuesPaymentIntent.stripe_payment_intent_id == intent["id"]
        )
    )
    reservation = result.scalar_one_or_none()
    if reservation is None:
        return
    reservation.status = status
    reservation.updated_at = datetime.now(timezone.utc)


async def _record_dues_payment(session: AsyncSession, intent: dict) -> None:
    """Append the dues_payment ledger entry for a succeeded PaymentIntent.

    Intents we did not create (no Chirp metadata) are ignored rather than guessed at.
    """
    metadata = intent.get("metadata") or {}
    cycle_id = metadata.get("chirp_dues_cycle_id")
    user_id = metadata.get("chirp_user_id")
    if not cycle_id or not user_id:
        return

    cycle = await session.get(models.DuesCycle, uuid.UUID(cycle_id))
    if cycle is None:
        return

    session.add(
        models.LedgerEntry(
            chapter_id=cycle.chapter_id,
            entry_type="dues_payment",
            # From the cycle, not the event: the amount of record is what the chapter
            # charges, and the event body is attacker-shaped input until proven otherwise.
            amount_cents=cycle.amount_cents,
            category="dues",
            description=cycle.name,
            related_user_id=uuid.UUID(user_id),
            dues_cycle_id=cycle.id,
            stripe_payment_intent_id=intent["id"],
            # No acting user in a webhook; the payer is the closest true answer.
            created_by=uuid.UUID(user_id),
        )
    )


@router.post("/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Stripe webhook sink. No auth dependency — Stripe authenticates via the signature.

    Returns 2xx for every verified event, including types we do not handle, because
    a non-2xx makes Stripe retry that event for days.

    Replay safety is layered: processed_stripe_events dedups at the event level, and
    the unique partial index on ledger_entries.stripe_payment_intent_id dedups at the
    payment level (two different events can describe the same intent). Both are DB
    constraints rather than check-then-insert, which would race under concurrent
    delivery. Event payloads are never logged — they carry customer PII.
    """
    if not stripe_signature:
        raise HTTPException(status_code=400, detail="missing_stripe_signature")
    payload = await request.body()
    event = stripe_service.verify_webhook_event(payload, stripe_signature)

    session.add(
        models.ProcessedStripeEvent(event_id=event["id"], event_type=event["type"])
    )
    if event["type"] == "payment_intent.succeeded":
        await _resolve_reservation(session, event["data"]["object"], "succeeded")
        await _record_dues_payment(session, event["data"]["object"])
    elif event["type"] == "payment_intent.payment_failed":
        # Releases uq_dues_intent_live so the member can genuinely retry (c51).
        await _resolve_reservation(session, event["data"]["object"], "failed")
    elif event["type"] == "payment_intent.canceled":
        await _resolve_reservation(session, event["data"]["object"], "canceled")

    try:
        await session.commit()
    except IntegrityError:
        # Replayed event id, or a second event for an intent already in the ledger.
        await session.rollback()
    return {"received": True}
