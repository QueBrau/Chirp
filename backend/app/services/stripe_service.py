"""Stripe Connect service — stubbed until milestone 8."""
import uuid

from fastapi import HTTPException


def _not_implemented() -> HTTPException:
    """501 for every Stripe operation until milestone 8."""
    return HTTPException(status_code=501, detail="stripe_not_implemented")


async def create_onboarding_link(chapter_id: uuid.UUID) -> dict:
    """Create a Stripe Connect onboarding link for a chapter's connected account."""
    # TODO(milestone-8): create/lookup connected account, return account-link URL.
    raise _not_implemented()


async def create_dues_payment_intent(cycle_id: uuid.UUID, user_id: uuid.UUID) -> dict:
    """Create a PaymentIntent for a dues cycle on the chapter's connected account."""
    # TODO(milestone-8): PaymentIntent on connected account; card data never touches us.
    raise _not_implemented()


async def handle_webhook(payload: bytes, signature: str) -> dict:
    """Verify and process a Stripe webhook event."""
    # TODO(milestone-8): verify signature with settings.stripe_webhook_secret, then dispatch.
    raise _not_implemented()
