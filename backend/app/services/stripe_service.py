"""Stripe Connect: Express accounts, direct charges, dues PaymentSheet params, webhooks.

Money model (see HANDOFF.md): Express accounts + DIRECT charges. The chapter is
merchant of record, funds land in the chapter's balance, and the chapter carries
chargeback liability — Chirp never takes custody. Every call therefore targets the
connected account via the `stripe_account` request option, and the platform's cut
rides along as `application_fee_amount`.

Card data never transits this backend (SPEC §8.7).
"""
import uuid

import stripe
from fastapi import HTTPException

from app.config import get_settings

CURRENCY = "usd"

# Platform cut per rail, in basis points: 1% card, 2% ACH. ACH still lands cheaper
# for the chapter all-in (~2.8% vs ~3.9%) because Stripe's own ACH fee is lower.
PLATFORM_FEE_BPS: dict[str, int] = {"card": 100, "ach": 200}

# The rail is locked into the PaymentIntent because application_fee_amount is
# immutable after creation — letting PaymentSheet choose the rail afterwards would
# leave the fee mismatched with the rail actually used.
RAIL_PAYMENT_METHOD_TYPES: dict[str, list[str]] = {
    "card": ["card"],
    "ach": ["us_bank_account"],
}


def platform_fee_cents(amount_cents: int, rail: str) -> int:
    """Platform application fee for an amount on a given rail (floored to the cent)."""
    return amount_cents * PLATFORM_FEE_BPS[rail] // 10_000


def _secret_key() -> str:
    """Stripe secret key, or 503 if the deployment has not configured payments."""
    key = get_settings().stripe_secret_key
    if not key:
        raise HTTPException(status_code=503, detail="stripe_not_configured")
    return key


def publishable_key() -> str:
    """Publishable key handed to the mobile SDK, or 503 if unconfigured."""
    key = get_settings().stripe_publishable_key
    if not key:
        raise HTTPException(status_code=503, detail="stripe_not_configured")
    return key


async def create_express_account(chapter_id: uuid.UUID, org_name: str) -> str:
    """Create an Express connected account for a chapter; returns the account id."""
    account = await stripe.Account.create_async(
        api_key=_secret_key(),
        type="express",
        country="US",
        business_profile={"name": org_name},
        metadata={"chirp_chapter_id": str(chapter_id)},
    )
    return account.id


async def create_account_link(account_id: str, return_url: str, refresh_url: str) -> stripe.AccountLink:
    """Create a single-use Stripe-hosted onboarding link for a connected account."""
    return await stripe.AccountLink.create_async(
        api_key=_secret_key(),
        account=account_id,
        type="account_onboarding",
        return_url=return_url,
        refresh_url=refresh_url,
    )


async def retrieve_account(account_id: str) -> stripe.Account:
    """Fetch a connected account so status is read live rather than mirrored locally."""
    return await stripe.Account.retrieve_async(account_id, api_key=_secret_key())


async def create_customer(account_id: str, email: str, display_name: str) -> str:
    """Create a Customer ON the connected account (direct charges); returns its id."""
    customer = await stripe.Customer.create_async(
        api_key=_secret_key(),
        stripe_account=account_id,
        email=email,
        name=display_name,
    )
    return customer.id


async def create_customer_session(account_id: str, customer_id: str) -> str:
    """Create a CustomerSession client secret enabling saved cards in PaymentSheet."""
    session = await stripe.CustomerSession.create_async(
        api_key=_secret_key(),
        stripe_account=account_id,
        customer=customer_id,
        components={
            "mobile_payment_element": {
                "enabled": True,
                "features": {
                    "payment_method_save": "enabled",
                    "payment_method_redisplay": "enabled",
                    "payment_method_remove": "enabled",
                },
            }
        },
    )
    return session.client_secret


async def create_dues_payment_intent(
    *,
    account_id: str,
    customer_id: str,
    amount_cents: int,
    rail: str,
    cycle_id: uuid.UUID,
    user_id: uuid.UUID,
    chapter_id: uuid.UUID,
) -> stripe.PaymentIntent:
    """Create a dues PaymentIntent on the chapter's account with the platform fee.

    The idempotency key is derived from (cycle, member, rail) so a client retry
    resolves to the same intent instead of creating a second one.
    """
    return await stripe.PaymentIntent.create_async(
        api_key=_secret_key(),
        stripe_account=account_id,
        idempotency_key=f"dues:{cycle_id}:{user_id}:{rail}",
        amount=amount_cents,
        currency=CURRENCY,
        customer=customer_id,
        payment_method_types=RAIL_PAYMENT_METHOD_TYPES[rail],
        application_fee_amount=platform_fee_cents(amount_cents, rail),
        # Metadata is the only link back to Chirp rows when the webhook arrives —
        # the webhook has no session and cannot infer who paid from the intent alone.
        metadata={
            "chirp_dues_cycle_id": str(cycle_id),
            "chirp_user_id": str(user_id),
            "chirp_chapter_id": str(chapter_id),
            "chirp_rail": rail,
        },
    )


async def retrieve_payment_intent(account_id: str, intent_id: str) -> stripe.PaymentIntent:
    """Fetch a PaymentIntent live rather than creating a duplicate on retry.

    A same-rail retry against a reservation that already has a stored intent id
    must never call create_dues_payment_intent again (board c193): Stripe only
    retains an idempotency key for 24h, while ACH can sit in 'processing' for
    days, so a create() call past that window mints a genuinely NEW real intent
    (a second bank debit) instead of resolving to the original. Retrieving the
    intent we already created is the only safe way to hand the client its
    client_secret again.
    """
    return await stripe.PaymentIntent.retrieve_async(
        intent_id, api_key=_secret_key(), stripe_account=account_id
    )


def verify_webhook_event(payload: bytes, signature: str) -> stripe.Event:
    """Verify a webhook signature against the RAW body and return the parsed event.

    Verification happens before any parsing: an unverified payload is entirely
    attacker-controlled input. Raises 400 on a bad signature.
    """
    secret = get_settings().stripe_webhook_secret
    if not secret:
        raise HTTPException(status_code=503, detail="stripe_not_configured")
    try:
        return stripe.Webhook.construct_event(payload, signature, secret)
    except stripe.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="invalid_signature") from None
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_payload") from None
