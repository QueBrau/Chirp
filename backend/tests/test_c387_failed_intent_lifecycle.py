"""Signed failure events against a deterministic, retryable provider lifecycle.

The fixture models Stripe's documented declined-attempt -> requires_payment_method
transition. It never confirms an intent or moves funds; it records which issued
client secrets still authorize a future payment attempt.
"""
from __future__ import annotations

import copy
from typing import Any

import pytest
import stripe
from httpx import AsyncClient

from tests.conftest import MakeChapterWith
from tests.test_c349_settlement_binding import rows, signed_post
from tests.test_payments import (
    FakeStripeObject,
    _create_dues_cycle,
    _onboard,
    stripe_calls,
    stripe_env,
)


class RetryableProvider:
    def __init__(self) -> None:
        self.intents: dict[str, dict[str, Any]] = {}
        self.keys: dict[str, str] = {}
        self.accounts: dict[str, str] = {}
        self.create_calls = 0
        self.retrieve_calls = 0
        self.cancel_calls = 0

    async def create(self, **params: Any) -> FakeStripeObject:
        self.create_calls += 1
        key = params["idempotency_key"]
        if key not in self.keys:
            intent_id = f"pi_lifecycle_{len(self.intents) + 1}"
            self.keys[key] = intent_id
            self.accounts[intent_id] = params["stripe_account"]
            self.intents[intent_id] = {
                "id": intent_id, "client_secret": f"{intent_id}_secret",
                "object": "payment_intent", "status": "requires_payment_method",
                "amount": params["amount"], "amount_received": 0,
                "currency": params["currency"], "livemode": False,
                "metadata": params["metadata"],
            }
        return FakeStripeObject(**copy.deepcopy(self.intents[self.keys[key]]))

    async def retrieve(self, intent_id: str, **params: Any) -> FakeStripeObject:
        self.retrieve_calls += 1
        assert params["stripe_account"] == self.accounts[intent_id]
        return FakeStripeObject(**copy.deepcopy(self.intents[intent_id]))

    async def cancel(self, intent_id: str, **params: Any) -> FakeStripeObject:
        self.cancel_calls += 1
        assert params["stripe_account"] == self.accounts[intent_id]
        self.intents[intent_id]["status"] = "canceled"
        return FakeStripeObject(**copy.deepcopy(self.intents[intent_id]))

    def can_attempt_payment(self, secret: str) -> bool:
        return any(
            intent["client_secret"] == secret
            and intent["status"] in ("requires_payment_method", "requires_confirmation", "requires_action")
            for intent in self.intents.values()
        )

    def declined_event(self, intent_id: str) -> dict[str, Any]:
        intent = self.intents[intent_id]
        intent["status"] = "requires_payment_method"
        return {
            "id": "evt_lifecycle_declined", "object": "event",
            "type": "payment_intent.payment_failed", "livemode": False,
            "account": self.accounts[intent_id],
            "data": {"object": copy.deepcopy(intent)},
        }


@pytest.mark.parametrize("retry_rail", ["card", "ach"])
async def test_signed_decline_cannot_leave_two_confirmable_checkout_intents(
    client: AsyncClient, make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch, stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]], retry_rail: str,
) -> None:
    provider = RetryableProvider()
    monkeypatch.setattr(stripe.PaymentIntent, "create_async", provider.create)
    monkeypatch.setattr(stripe.PaymentIntent, "retrieve_async", provider.retrieve)
    monkeypatch.setattr(stripe.PaymentIntent, "cancel_async", provider.cancel)
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle = await _create_dues_cycle(client, setup)
    endpoint = f"/payments/dues/{cycle}/intent"

    first = await client.post(endpoint, json={"rail": "card"}, headers=setup.member.headers)
    assert first.status_code == 200, first.text
    earlier_secret = first.json()["payment_intent_client_secret"]
    assert provider.can_attempt_payment(earlier_secret)
    failed = provider.declined_event("pi_lifecycle_1")
    assert (await signed_post(client, failed)).status_code == 200
    # Replay cannot be blamed for the transition or the new checkout.
    assert (await signed_post(client, failed)).status_code == 200
    assert len(await rows("SELECT event_id FROM processed_stripe_events")) == 1
    assert await rows("SELECT id FROM stripe_settlement_quarantine") == []
    assert await rows("SELECT id FROM ledger_entries") == []
    local_after_failure = await rows("SELECT status FROM dues_payment_intents")

    retry = await client.post(endpoint, json={"rail": retry_rail}, headers=setup.member.headers)
    assert retry.status_code in (200, 409), retry.text
    confirmable = [intent["id"] for intent in provider.intents.values()
                   if provider.can_attempt_payment(intent["client_secret"])]
    evidence = {
        "local_after_failure": local_after_failure,
        "retry_status": retry.status_code,
        "provider_creates": provider.create_calls,
        "provider_cancels": provider.cancel_calls,
        "earlier_secret_still_usable": provider.can_attempt_payment(earlier_secret),
        "confirmable_intents": confirmable,
    }
    assert len(confirmable) == 1, evidence
    assert local_after_failure == [{"status": "failed"}]
    assert provider.create_calls == 1 and provider.cancel_calls == 0
    if retry_rail == "card":
        assert retry.status_code == 200, retry.text
        assert retry.json()["payment_intent_client_secret"] == earlier_secret
        assert provider.retrieve_calls == 1
    else:
        assert retry.status_code == 409, retry.text
        assert retry.json()["detail"] == "payment_already_in_progress"
        assert provider.retrieve_calls == 0


@pytest.fixture
async def declined_checkout(client, make_chapter_with, monkeypatch, stripe_env, stripe_calls):
    provider = RetryableProvider()
    monkeypatch.setattr(stripe.PaymentIntent, "create_async", provider.create)
    monkeypatch.setattr(stripe.PaymentIntent, "retrieve_async", provider.retrieve)
    monkeypatch.setattr(stripe.PaymentIntent, "cancel_async", provider.cancel)
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle = await _create_dues_cycle(client, setup)
    first = await client.post(f"/payments/dues/{cycle}/intent", json={"rail": "card"},
                              headers=setup.member.headers)
    assert first.status_code == 200, first.text
    failure = provider.declined_event("pi_lifecycle_1")
    assert (await signed_post(client, failure)).status_code == 200
    assert await rows("SELECT status FROM dues_payment_intents") == [{"status": "failed"}]
    return provider, setup, cycle, failure


@pytest.mark.parametrize("terminal", ["canceled", "succeeded"])
async def test_late_decline_does_not_revive_a_terminal_reservation(client, declined_checkout, terminal):
    provider, setup, cycle, failure = declined_checkout
    original = provider.intents["pi_lifecycle_1"]
    original["status"] = terminal
    if terminal == "succeeded":
        original["amount_received"] = original["amount"]
    event = copy.deepcopy(failure)
    event.update(id=f"evt_{terminal}", type=f"payment_intent.{terminal}")
    event["data"]["object"] = copy.deepcopy(original)
    assert (await signed_post(client, event)).status_code == 200

    retry = await client.post(f"/payments/dues/{cycle}/intent", json={"rail": "ach"},
                              headers=setup.member.headers)
    if terminal == "canceled":
        assert retry.status_code == 200, retry.text
        assert provider.create_calls == 2
        assert not provider.can_attempt_payment(original["client_secret"])
        assert provider.can_attempt_payment(retry.json()["payment_intent_client_secret"])
    else:
        assert retry.status_code == 409, retry.text
        assert retry.json()["detail"] == "already_paid"
        assert provider.create_calls == 1

    # Deliver a distinct late event, not just an already-recorded receipt replay.
    late_failure = copy.deepcopy(failure)
    late_failure["id"] = "evt_late_failure"
    assert (await signed_post(client, late_failure)).status_code == 200
    assert (await signed_post(client, late_failure)).status_code == 200
    stored = await rows("SELECT stripe_payment_intent_id, status FROM dues_payment_intents "
                        "ORDER BY stripe_payment_intent_id")
    expected = [{"stripe_payment_intent_id": "pi_lifecycle_1", "status": terminal}]
    if terminal == "canceled":
        expected.append({"stripe_payment_intent_id": "pi_lifecycle_2", "status": "open"})
    assert stored == expected
    assert len(await rows("SELECT id FROM ledger_entries")) == (1 if terminal == "succeeded" else 0)
    assert len(await rows("SELECT event_id FROM processed_stripe_events")) == 3
    assert await rows("SELECT id FROM stripe_settlement_quarantine") == []


@pytest.mark.parametrize("provider_outcome", ["canceled", "processing", "unknown"])
async def test_aged_failed_reservation_requires_confirmed_cancellation(
    client, declined_checkout, monkeypatch, provider_outcome,
):
    from app.db import get_session_factory
    from sqlalchemy import text

    provider, setup, cycle, _ = declined_checkout
    async with get_session_factory()() as session:
        await session.execute(text("UPDATE dues_payment_intents SET created_at=now()-interval '25 hours'"))
        await session.commit()
    if provider_outcome != "canceled":
        async def cannot_cancel(intent_id, **params):
            provider.cancel_calls += 1
            raise stripe.APIConnectionError("simulated unknown cancel outcome")
        monkeypatch.setattr(stripe.PaymentIntent, "cancel_async", cannot_cancel)
        if provider_outcome == "processing":
            provider.intents["pi_lifecycle_1"]["status"] = "processing"
        else:
            async def cannot_retrieve(intent_id, **params):
                provider.retrieve_calls += 1
                raise stripe.APIConnectionError("simulated unknown retrieval outcome")
            monkeypatch.setattr(stripe.PaymentIntent, "retrieve_async", cannot_retrieve)

    retry = await client.post(f"/payments/dues/{cycle}/intent", json={"rail": "ach"},
                              headers=setup.member.headers)
    assert provider.cancel_calls == 1
    if provider_outcome == "canceled":
        assert retry.status_code == 200, retry.text
        assert provider.create_calls == 2
        assert not provider.can_attempt_payment("pi_lifecycle_1_secret")
        assert await rows("SELECT status FROM dues_payment_intents ORDER BY created_at") == [
            {"status": "canceled"}, {"status": "open"},
        ]
    else:
        assert retry.status_code == 409, retry.text
        assert retry.json()["detail"] == "payment_already_in_progress"
        assert provider.create_calls == 1 and provider.retrieve_calls == 1
        assert await rows("SELECT status FROM dues_payment_intents") == [{"status": "failed"}]


async def test_failed_intent_retrieve_timeout_retains_row_and_releases_ownership(
    client, declined_checkout, monkeypatch,
):
    import asyncio
    from app.routers import payments

    provider, setup, cycle, _ = declined_checkout
    canceled = asyncio.Event()
    async def hung_retrieve(intent_id, **params):
        try:
            await asyncio.Event().wait()
        finally:
            canceled.set()
    monkeypatch.setattr(stripe.PaymentIntent, "retrieve_async", hung_retrieve)
    monkeypatch.setattr(payments, "RESERVATION_PROVIDER_TIMEOUT_SECONDS", 0.05)
    response = await client.post(f"/payments/dues/{cycle}/intent", json={"rail": "card"},
                                 headers=setup.member.headers)
    assert response.status_code == 503, response.text
    assert response.json()["detail"] == "payment_outcome_unconfirmed"
    assert canceled.is_set(), "the timed-out provider coroutine must not retain ownership"
    assert await rows("SELECT status FROM dues_payment_intents") == [{"status": "failed"}]
    assert provider.can_attempt_payment("pi_lifecycle_1_secret")
    monkeypatch.setattr(stripe.PaymentIntent, "retrieve_async", provider.retrieve)
    monkeypatch.setattr(payments, "RESERVATION_PROVIDER_TIMEOUT_SECONDS", 15)
    retry = await client.post(f"/payments/dues/{cycle}/intent", json={"rail": "card"},
                              headers=setup.member.headers)
    assert retry.status_code == 200, retry.text
    assert retry.json()["payment_intent_client_secret"] == "pi_lifecycle_1_secret"
    assert provider.create_calls == 1


async def test_failed_intent_blocks_installment_plan_read_guard(client, declined_checkout):
    from tests.test_dues_payment_plans import _create_plan, _three_installments

    provider, setup, cycle, _ = declined_checkout
    result = await _create_plan(client, setup, cycle, setup.member.id, _three_installments(25_000))
    assert result.status_code == 409, result.text
    assert result.json()["detail"] == "payment_in_progress"
    assert await rows("SELECT id FROM dues_payment_plans") == []
    assert provider.can_attempt_payment("pi_lifecycle_1_secret")
