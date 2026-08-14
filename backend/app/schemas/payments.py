"""Payment schemas: Connect onboarding, account status, dues PaymentSheet params."""
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

# Which rail the member chose. application_fee_amount is immutable once a
# PaymentIntent exists, so the rail is picked BEFORE the intent is created —
# otherwise there is no moment where the fee and the rail are both known.
PaymentRail = Literal["card", "ach"]


class ConnectOnboardingRequest(BaseModel):
    """Body for POST /payments/connect/onboarding-link."""

    chapter_id: uuid.UUID


class ConnectOnboardingOut(BaseModel):
    """Stripe-hosted Express onboarding link; single-use and short-lived."""

    url: str
    expires_at: datetime


class ChapterPaymentsStatusOut(BaseModel):
    """Whether a chapter can actually accept dues yet."""

    model_config = ConfigDict(from_attributes=True)

    onboarded: bool
    charges_enabled: bool
    details_submitted: bool


class DuesIntentCreate(BaseModel):
    """Body for POST /payments/dues/{cycle_id}/intent."""

    rail: PaymentRail


class DuesIntentOut(BaseModel):
    """Everything the mobile PaymentSheet needs for one dues payment.

    amount_cents is echoed from the dues cycle (server-side) so the client can
    render the total it is about to charge — it is never an input.
    """

    payment_intent_client_secret: str
    customer_session_client_secret: str
    customer_id: str
    publishable_key: str
    stripe_account_id: str
    amount_cents: int
    application_fee_cents: int
    rail: PaymentRail
