"""Dormant Stripe Customer deletion adapter for c436 operator review.

This module is intentionally not imported by the deletion executor.  It handles
only caller-owned ``chapter_stripe_customers`` mappings and returns a provider
receipt after Stripe's documented deleted-customer readback.  Financial ledger,
payment-intent, plan, installment, and chapter mapping rows remain local audit
history.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Iterable
from uuid import UUID

import stripe
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.services.account_fulfillment import ProviderUnavailable

MAX_CUSTOMERS = 20
OPERATION_TIMEOUT_SECONDS = 30
TOTAL_TIMEOUT_SECONDS = 120
_ACCOUNT_PREFIX = "acct_"
_CUSTOMER_PREFIX = "cus_"


@dataclass(frozen=True)
class StripeCustomerReference:
    """A customer and its exact connected account, bound to one Chirp user."""

    connected_account_id: str
    customer_id: str


def _valid_provider_id(value: object, prefix: str) -> bool:
    suffix = value[len(prefix):] if isinstance(value, str) and value.startswith(prefix) else ""
    return (
        isinstance(value, str)
        and value.isascii()
        and value.startswith(prefix)
        and 1 <= len(suffix) <= 255
        and suffix.isalnum()
    )


def _invalid(reason: str) -> ProviderUnavailable:
    return ProviderUnavailable(reason)


async def load_customer_references(
    session: AsyncSession,
    user_id: UUID,
    *,
    max_customers: int = MAX_CUSTOMERS,
) -> tuple[StripeCustomerReference, ...]:
    """Load only mappings owned by ``user_id`` with their joined chapter account.

    A missing connected account, malformed provider ID, duplicate provider
    customer across accounts, or over-cap result is a manual-review failure.
    An empty owned mapping is valid and produces an explicit empty scope.
    """
    if isinstance(max_customers, bool) or not isinstance(max_customers, int) or not 0 < max_customers <= MAX_CUSTOMERS:
        raise ValueError("invalid_customer_cap")
    rows = (await session.execute(
        select(models.ChapterStripeCustomer, models.Chapter.stripe_account_id)
        .join(models.Chapter, models.Chapter.id == models.ChapterStripeCustomer.chapter_id)
        .where(models.ChapterStripeCustomer.user_id == user_id)
        .order_by(models.ChapterStripeCustomer.chapter_id, models.ChapterStripeCustomer.stripe_customer_id)
        .limit(max_customers + 1)
    )).all()
    if len(rows) > max_customers:
        raise _invalid("stripe_customer_mapping_limit")
    refs: list[StripeCustomerReference] = []
    seen_customers: set[str] = set()
    for mapping, account_id in rows:
        if not _valid_provider_id(account_id, _ACCOUNT_PREFIX) or not _valid_provider_id(mapping.stripe_customer_id, _CUSTOMER_PREFIX):
            raise _invalid("stripe_customer_mapping_invalid")
        if mapping.stripe_customer_id in seen_customers:
            raise _invalid("stripe_customer_mapping_ambiguous")
        seen_customers.add(mapping.stripe_customer_id)
        refs.append(StripeCustomerReference(account_id, mapping.stripe_customer_id))
    if refs:
        pair_predicates = [
            and_(
                models.Chapter.stripe_account_id == ref.connected_account_id,
                models.ChapterStripeCustomer.stripe_customer_id == ref.customer_id,
            )
            for ref in refs
        ]
        foreign_mapping = (await session.execute(
            select(models.ChapterStripeCustomer.user_id)
            .join(models.Chapter, models.Chapter.id == models.ChapterStripeCustomer.chapter_id)
            .where(
                models.ChapterStripeCustomer.user_id != user_id,
                or_(*pair_predicates),
            )
            .limit(1)
        )).scalar_one_or_none()
        if foreign_mapping is not None:
            raise _invalid("stripe_customer_mapping_foreign_owner")
        pending_plan = (await session.execute(
            select(models.DuesPaymentPlan.id)
            .where(models.DuesPaymentPlan.user_id == user_id, models.DuesPaymentPlan.status == "active")
            .limit(1)
        )).scalar_one_or_none()
        pending_intent = (await session.execute(
            select(models.DuesPaymentIntent.id)
            .where(
                models.DuesPaymentIntent.user_id == user_id,
                or_(
                    models.DuesPaymentIntent.status.not_in(("succeeded", "canceled")),
                    models.DuesPaymentIntent.status.is_(None),
                ),
            )
            .limit(1)
        )).scalar_one_or_none()
        if pending_plan is not None or pending_intent is not None:
            raise _invalid("stripe_pending_financial_review_required")
    return tuple(refs)


class StripeCustomerProvider:
    """Delete known Stripe Customers with strict connected-account readback.

    Stripe retains a deleted Customer's minimal object for history.  A 404 is
    therefore never treated as proof of deletion: it can indicate the wrong
    connected account or credentials.  Repeated calls accept only the exact
    ``id=customer, object=customer, deleted=true`` readback.
    """

    def __init__(self, references: Iterable[StripeCustomerReference], *, expected_user_id: UUID | str, stripe_module: Any = stripe, api_key: str | None = None) -> None:
        refs = tuple(references)
        if len(refs) > MAX_CUSTOMERS:
            raise _invalid("stripe_customer_mapping_limit")
        if len({ref.customer_id for ref in refs}) != len(refs):
            raise _invalid("stripe_customer_mapping_ambiguous")
        for ref in refs:
            if not _valid_provider_id(ref.connected_account_id, _ACCOUNT_PREFIX) or not _valid_provider_id(ref.customer_id, _CUSTOMER_PREFIX):
                raise _invalid("stripe_customer_mapping_invalid")
        self._references = refs
        self._expected_user_id = str(expected_user_id)
        self._stripe = stripe_module
        self._api_key = api_key

    def _key(self) -> str:
        try:
            if self._api_key:
                return self._api_key
            from app.services.stripe_service import _secret_key
            return _secret_key()
        except Exception:
            raise _invalid("stripe_customer_credentials_unavailable") from None

    async def _call(self, operation, *args: Any, **kwargs: Any) -> Any:
        try:
            return await asyncio.wait_for(operation(*args, **kwargs), timeout=OPERATION_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            raise _invalid("stripe_customer_operation_timeout") from None
        except ProviderUnavailable:
            raise
        except Exception:
            raise _invalid("stripe_customer_provider_error") from None

    async def _subscription_review(self, ref: StripeCustomerReference) -> None:
        list_async = getattr(getattr(self._stripe, "Subscription", None), "list_async", None)
        if list_async is None:
            raise _invalid("stripe_subscription_review_unavailable")
        subscriptions = await self._call(
            list_async,
            customer=ref.customer_id,
            status="all",
            limit=100,
            api_key=self._key(),
            stripe_account=ref.connected_account_id,
        )
        rows = getattr(subscriptions, "data", None)
        has_more = getattr(subscriptions, "has_more", None)
        if not isinstance(rows, list) or has_more is not False:
            raise _invalid("stripe_subscription_review_incomplete")
        inactive = {"canceled", "incomplete_expired"}
        known = inactive | {"active", "trialing", "past_due", "unpaid", "paused", "incomplete"}
        for row in rows:
            status = getattr(row, "status", row.get("status") if isinstance(row, dict) else None)
            if not isinstance(status, str) or status not in known:
                raise _invalid("stripe_subscription_status_unknown")
            if status not in inactive:
                raise _invalid("stripe_active_payment_review_required")

    async def delete_account_data(self, *, user: models.User, idempotency_key: str) -> dict[str, object]:
        if str(user.id) != self._expected_user_id:
            raise _invalid("stripe_customer_subject_mismatch")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise _invalid("stripe_customer_idempotency_key_invalid")
        try:
            async with asyncio.timeout(TOTAL_TIMEOUT_SECONDS):
                # Complete all ownership/readback/subscription checks before the
                # first delete. A later active subscription must not leave an
                # earlier customer partially removed.
                for ref in self._references:
                    current = await self._call(
                        self._stripe.Customer.retrieve_async,
                        ref.customer_id,
                        api_key=self._key(),
                        stripe_account=ref.connected_account_id,
                    )
                    current_id = getattr(current, "id", None)
                    current_object = getattr(current, "object", None)
                    deleted = getattr(current, "deleted", False)
                    if current_id != ref.customer_id or current_object != "customer":
                        raise _invalid("stripe_customer_readback_failed")
                    if deleted is False:
                        await self._subscription_review(ref)
                    elif deleted is not True:
                        raise _invalid("stripe_customer_readback_failed")
                for ref in self._references:
                    try:
                        current = await self._call(
                            self._stripe.Customer.retrieve_async,
                            ref.customer_id,
                            api_key=self._key(),
                            stripe_account=ref.connected_account_id,
                        )
                    except Exception as exc:
                        if isinstance(exc, ProviderUnavailable):
                            raise
                        raise _invalid("stripe_customer_readback_failed") from None
                    current_id = getattr(current, "id", None)
                    current_object = getattr(current, "object", None)
                    deleted = getattr(current, "deleted", False)
                    if current_id != ref.customer_id or current_object != "customer":
                        raise _invalid("stripe_customer_readback_failed")
                    if deleted is True:
                        continue
                    if deleted is not False:
                        raise _invalid("stripe_customer_readback_failed")
                    await self._subscription_review(ref)
                    deleted_customer = await self._call(
                        self._stripe.Customer.delete_async,
                        ref.customer_id,
                        api_key=self._key(),
                        stripe_account=ref.connected_account_id,
                    )
                    if (
                        getattr(deleted_customer, "id", None) != ref.customer_id
                        or getattr(deleted_customer, "object", None) != "customer"
                        or getattr(deleted_customer, "deleted", False) is not True
                    ):
                        raise _invalid("stripe_customer_delete_not_confirmed")
                    readback = await self._call(
                        self._stripe.Customer.retrieve_async,
                        ref.customer_id,
                        api_key=self._key(),
                        stripe_account=ref.connected_account_id,
                    )
                    if (
                        getattr(readback, "id", None) != ref.customer_id
                        or getattr(readback, "object", None) != "customer"
                        or getattr(readback, "deleted", False) is not True
                    ):
                        raise _invalid("stripe_customer_readback_failed")
        except asyncio.TimeoutError:
            raise _invalid("stripe_customer_total_timeout") from None
        return {
            "confirmed": True,
            "provider": "payment_provider",
            "subject": str(user.id),
            "scope": [
                "stripe_customer_records"
                if self._references
                else "stripe_customer_records:none_locally_mapped"
            ],
            "readback": True,
            "retained_with_reason": ["stripe_deleted_customer_history"] if self._references else [],
        }


async def provider_for_user(session: AsyncSession, user: models.User) -> StripeCustomerProvider:
    """Construct the dormant adapter from a caller-owned DB query."""
    return StripeCustomerProvider(await load_customer_references(session, user.id), expected_user_id=user.id)
