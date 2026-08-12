"""Payments: Stripe Connect onboarding + dues intents (501 stubs) and webhook stub."""
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.core.errors import forbidden, not_found
from app.core.permissions import Role
from app.db import get_session
from app.middleware.auth import get_current_user
from app.services import stripe_service

router = APIRouter(tags=["payments"])


class ConnectOnboardingRequest(BaseModel):
    """Body for POST /payments/connect/onboarding-link (local — Stripe is milestone 8)."""

    chapter_id: uuid.UUID


@router.post("/payments/connect/onboarding-link")
async def create_connect_onboarding_link(
    body: ConnectOnboardingRequest,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Start Stripe Connect onboarding for a chapter; treasurer/president only.

    Delegates to the stripe_service stub, which raises 501 until milestone 8.
    """
    result = await session.execute(
        select(models.Membership.id)
        .where(
            models.Membership.user_id == user.id,
            models.Membership.chapter_id == body.chapter_id,
            models.Membership.status == "active",
            models.Membership.role.in_([Role.treasurer.value, Role.president.value]),
        )
        .limit(1)
    )
    if result.scalar_one_or_none() is None:
        raise forbidden("insufficient_role")
    # TODO(milestone-8): returns {"url": <account-link>} once Stripe Connect lands.
    return await stripe_service.create_onboarding_link(body.chapter_id)


@router.post("/payments/dues/{cycle_id}/intent")
async def create_dues_payment_intent(
    cycle_id: uuid.UUID,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Create a PaymentIntent for a dues cycle; members of the cycle's chapter.

    Delegates to the stripe_service stub, which raises 501 until milestone 8.
    """
    cycle = await session.get(models.DuesCycle, cycle_id)
    if cycle is None:
        raise not_found("dues_cycle_not_found")
    result = await session.execute(
        select(models.Membership.id)
        .where(
            models.Membership.user_id == user.id,
            models.Membership.chapter_id == cycle.chapter_id,
            models.Membership.status == "active",
        )
        .limit(1)
    )
    if result.scalar_one_or_none() is None:
        raise forbidden("not_a_member")
    # TODO(milestone-8): returns PaymentSheet params (client_secret etc.).
    return await stripe_service.create_dues_payment_intent(cycle_id, user.id)


@router.post("/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
) -> dict:
    """Stripe webhook — signature-check STUB ONLY per SPEC §9; no business logic.

    Reads the Stripe-Signature header and the raw body, acknowledges, and does
    nothing else. No auth dependency: Stripe authenticates via the signature.
    """
    if not stripe_signature:
        raise HTTPException(status_code=400, detail="missing_stripe_signature")
    await request.body()  # read + discard; verification is milestone 8
    # TODO(milestone-8): verify via stripe_service.handle_webhook and dispatch events.
    return {"received": True}
