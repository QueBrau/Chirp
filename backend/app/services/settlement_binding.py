"""Validate signed Stripe settlement against stored reservations before changing money state."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.services import stripe_service


@dataclass(frozen=True)
class BoundSettlement:
    """The database records that authorize a settlement, independent of event metadata."""

    reservation: models.DuesPaymentIntent
    cycle: models.DuesCycle


def _reference(value: Any, prefix: str = "") -> str | None:
    """Keep bounded identifiers only, never arbitrary payloads or customer details."""
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 255:
        return "[invalid]"
    if prefix:
        return value if re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9_]+", value) else "[invalid]"
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        return "[invalid]"


def _integer(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 2_147_483_647 else None


def _currency(value: Any) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[a-z]{3}", value) else None


async def bind_settlement(
    session: AsyncSession, event: Mapping[str, Any],
) -> BoundSettlement | None:
    """Return trusted records, or append quarantine/ignore foreign events without side effects.

    Caller owns the transaction, including its processed-event receipt. Insertion errors
    must propagate: acknowledging a failed quarantine write would lose the incident.
    No provider calls or logging happen here.
    """
    intent = event["data"]["object"]
    metadata = intent.get("metadata") or {}
    metadata = metadata if isinstance(metadata, Mapping) else {}
    intent_id = intent.get("id")
    reservation = None
    if _reference(intent_id, "pi_") not in (None, "[invalid]"):
        reservation = await session.scalar(
            select(models.DuesPaymentIntent)
            .where(models.DuesPaymentIntent.stripe_payment_intent_id == intent_id)
            .with_for_update()
        )
    if reservation is None and not any(str(key).startswith("chirp_") for key in metadata):
        return None  # A foreign Stripe event, not a claimed Chirp payment.

    expected_mode = stripe_service.expected_livemode()
    expected: dict[str, Any] = {"livemode": expected_mode}
    observed: dict[str, Any] = {
        "livemode": event.get("livemode") if type(event.get("livemode")) is bool else None,
        "intent_livemode": intent.get("livemode") if type(intent.get("livemode")) is bool else None,
        "account": _reference(event.get("account"), "acct_"),
        "amount": _integer(intent.get("amount")),
        "amount_received": _integer(intent.get("amount_received")),
        "currency": _currency(intent.get("currency")),
        "chapter_id": _reference(metadata.get("chirp_chapter_id")),
        "cycle_id": _reference(metadata.get("chirp_dues_cycle_id")),
        "user_id": _reference(metadata.get("chirp_user_id")),
        "rail": metadata.get("chirp_rail") if metadata.get("chirp_rail") in ("card", "ach") else None,
    }
    reason: str | None = None
    cycle = chapter = None
    if reservation is None:
        reason = "no_reservation"
    else:
        cycle = await session.get(models.DuesCycle, reservation.dues_cycle_id)
        chapter = await session.get(models.Chapter, reservation.chapter_id)
        expected.update(
            amount_cents=reservation.amount_cents, currency=reservation.currency,
            chapter_id=str(reservation.chapter_id), cycle_id=str(reservation.dues_cycle_id),
            user_id=str(reservation.user_id), rail=reservation.rail,
            account=chapter.stripe_account_id if chapter is not None else None,
        )
        if expected_mode is None:
            reason = "livemode_unverifiable"
        elif (
            type(event.get("livemode")) is not bool
            or event.get("livemode") is not expected_mode
            or type(intent.get("livemode")) is not bool
            or intent.get("livemode") is not expected_mode
        ):
            reason = "livemode_mismatch"
        elif (
            chapter is None or not chapter.stripe_account_id
            or event.get("account") != chapter.stripe_account_id
        ):
            reason = "account_mismatch"
        elif cycle is None or cycle.chapter_id != reservation.chapter_id:
            reason = "metadata_mismatch"
        elif event["type"] == "payment_intent.succeeded":
            expected_metadata = {
                "chirp_chapter_id": str(reservation.chapter_id),
                "chirp_dues_cycle_id": str(reservation.dues_cycle_id),
                "chirp_user_id": str(reservation.user_id),
                "chirp_rail": reservation.rail,
            }
            if any(metadata.get(key) != value for key, value in expected_metadata.items()):
                reason = "metadata_mismatch"
            elif intent.get("currency") != reservation.currency:
                reason = "currency_mismatch"
            elif (
                type(intent.get("amount")) is not int
                or type(intent.get("amount_received")) is not int
                or intent.get("amount") != reservation.amount_cents
                or intent.get("amount_received") != reservation.amount_cents
            ):
                reason = "amount_mismatch"

    if reason is not None:
        session.add(models.StripeSettlementQuarantine(
            event_id=event["id"], event_type=event["type"],
            stripe_payment_intent_id=_reference(intent_id, "pi_"),
            reservation_id=reservation.id if reservation is not None else None,
            chapter_id=reservation.chapter_id if reservation is not None else None,
            reason=reason, expected=expected, observed=observed,
        ))
        # A failed quarantine must never become a 200 response. The caller's
        # uniqueness handling is limited to explicit receipt/ledger INSERTs.
        await session.flush()
        return None
    assert reservation is not None and cycle is not None
    return BoundSettlement(reservation, cycle)

