"""Faults at the actual Stripe SDK boundary; no provider calls or real funds."""
from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
import stripe
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db import get_engine, get_session_factory
from app.routers import payments
from tests.conftest import MakeChapterWith
from tests.test_payments import (
    FakeStripeObject,
    _create_dues_cycle,
    _onboard,
    _seed_reservation,
    stripe_calls,
    stripe_env,
)


async def _rows(cycle_id: str) -> list[dict[str, Any]]:
    async with get_session_factory()() as session:
        result = await session.execute(
            text("SELECT id, status, stripe_payment_intent_id FROM dues_payment_intents "
                 "WHERE dues_cycle_id = :cycle ORDER BY created_at, id"),
            {"cycle": cycle_id},
        )
        return [dict(row) for row in result.mappings()]


async def test_stale_same_reservation_create_error_cannot_release_earlier_secret(
    client: AsyncClient, make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch, stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle = await _create_dues_cycle(client, setup)
    entered = asyncio.Event()
    release_success = asyncio.Event()
    keys: list[str] = []

    async def create(**params: Any) -> FakeStripeObject:
        keys.append(params["idempotency_key"])
        number = len(keys)
        if number == 1:
            entered.set()
            await release_success.wait()
            return FakeStripeObject(id="pi_earlier", client_secret="pi_earlier_secret")
        if number == 2:
            # Before c366, a concurrent second SDK invocation could fail after
            # the first returned its secret and cancel that successful row.
            raise stripe.APIConnectionError("fixture lost response")
        return FakeStripeObject(id="pi_second", client_secret="pi_second_secret")

    monkeypatch.setattr(stripe.PaymentIntent, "create_async", create)
    endpoint = f"/payments/dues/{cycle}/intent"
    first = asyncio.create_task(client.post(endpoint, json={"rail": "ach"}, headers=setup.member.headers))
    await asyncio.wait_for(entered.wait(), 5)
    second = asyncio.create_task(client.post(endpoint, json={"rail": "ach"}, headers=setup.member.headers))
    try:
        competing = await asyncio.wait_for(second, 5)
        assert competing.status_code == 409, competing.text
        assert competing.json()["detail"] == "payment_already_in_progress"
        assert len(keys) == 1, "the loser must never call Stripe create"
    finally:
        release_success.set()
        success = await asyncio.wait_for(first, 5)
    assert success.status_code == 200, success.text
    assert success.json()["payment_intent_client_secret"] == "pi_earlier_secret"
    retained = await _rows(cycle)
    cross_rail = await client.post(endpoint, json={"rail": "card"}, headers=setup.member.headers)
    # The fixture's first intent/secret remains usable. This is not a claim that
    # money was captured; it proves whether a second checkout is now permitted.
    assert (retained[0]["status"], cross_rail.status_code) == ("open", 409)
    assert retained[0]["stripe_payment_intent_id"] == "pi_earlier"


async def test_single_lost_create_response_retries_original_reservation_and_key(
    client: AsyncClient, make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch, stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle = await _create_dues_cycle(client, setup)
    created: dict[str, str] = {}
    keys: list[str] = []

    async def create(**params: Any) -> FakeStripeObject:
        key = params["idempotency_key"]
        keys.append(key)
        intent_id = created.setdefault(key, f"pi_created_{len(created) + 1}")
        if len(keys) == 1:
            # Provider executed successfully; only its HTTP response was lost.
            raise stripe.APIConnectionError("fixture response lost after execution")
        return FakeStripeObject(id=intent_id, client_secret=f"{intent_id}_secret")

    monkeypatch.setattr(stripe.PaymentIntent, "create_async", create)
    endpoint = f"/payments/dues/{cycle}/intent"
    lost = await client.post(endpoint, json={"rail": "ach"}, headers=setup.member.headers)
    assert lost.status_code == 503
    assert lost.json()["detail"] == "payment_outcome_unconfirmed"
    original = (await _rows(cycle))[0]
    retry = await client.post(endpoint, json={"rail": "ach"}, headers=setup.member.headers)
    assert retry.status_code == 200, retry.text
    rows = await _rows(cycle)
    assert (len(rows), len(created), keys[0] == keys[1]) == (1, 1, True)
    assert rows[0]["id"] == original["id"]
    assert rows[0]["stripe_payment_intent_id"] == "pi_created_1"


@pytest.mark.parametrize("age_hours", [23, 24, 25])
async def test_unknown_no_id_reservation_never_recreates_after_safe_key_window(
    client: AsyncClient, make_chapter_with: MakeChapterWith,
    stripe_env: None, stripe_calls: dict[str, list[dict[str, Any]]], age_hours: int,
) -> None:
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle = await _create_dues_cycle(client, setup)
    original = await _seed_reservation(setup.chapter_id, cycle, setup.member.id, "ach", "open", age_hours=age_hours)
    response = await client.post(f"/payments/dues/{cycle}/intent", json={"rail": "ach"}, headers=setup.member.headers)
    assert response.status_code == 409
    assert response.json()["detail"] == "payment_reconciliation_required"
    assert stripe_calls["payment_intent"] == []
    assert stripe_calls["payment_intent_cancel"] == []
    rows = await _rows(cycle)
    assert len(rows) == 1 and str(rows[0]["id"]) == original
    assert rows[0]["status"] == "open"


@pytest.mark.parametrize("error, detail", [
    (stripe.APIConnectionError("SECRET fixture body"), "payment_outcome_unconfirmed"),
    (stripe.APIError("SECRET fixture body", http_status=500), "payment_outcome_unconfirmed"),
    (stripe.IdempotencyError("SECRET fixture body", http_status=400), "payment_outcome_unconfirmed"),
    (stripe.InvalidRequestError("SECRET fixture body", "x", code="idempotency_key_in_use", http_status=400), "payment_outcome_unconfirmed"),
    (stripe.RateLimitError("SECRET fixture body", http_status=429), "payment_outcome_unconfirmed"),
    (stripe.InvalidRequestError("SECRET fixture body", "amount", http_status=400), "payment_provider_rejected"),
    (stripe.AuthenticationError("SECRET fixture body", http_status=401), "payment_provider_rejected"),
    (stripe.PermissionError("SECRET fixture body", http_status=403), "payment_provider_rejected"),
])
async def test_rejection_classification_never_releases_potential_earlier_outcome(
    client: AsyncClient, make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch, stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]], error: Exception, detail: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle = await _create_dues_cycle(client, setup)
    original = await _seed_reservation(setup.chapter_id, cycle, setup.member.id, "ach", "open")

    async def reject(**params: Any) -> FakeStripeObject:
        raise error

    monkeypatch.setattr(stripe.PaymentIntent, "create_async", reject)
    response = await client.post(f"/payments/dues/{cycle}/intent", json={"rail": "ach"}, headers=setup.member.headers)
    assert response.status_code == 503
    assert response.json()["detail"] == detail
    rows = await _rows(cycle)
    assert len(rows) == 1 and str(rows[0]["id"]) == original
    assert rows[0]["status"] == "open"
    assert "SECRET" not in response.text and "SECRET" not in caplog.text
    assert get_engine().pool.checkedout() == 0


@pytest.mark.parametrize("provider_step", ["create", "retrieve", "cancel"])
async def test_provider_timeout_releases_row_and_pool_without_releasing_reservation(
    client: AsyncClient, make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch, stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]], provider_step: str,
) -> None:
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle = await _create_dues_cycle(client, setup)
    original = await _seed_reservation(
        setup.chapter_id, cycle, setup.member.id, "ach", "open",
        stripe_payment_intent_id=None if provider_step == "create" else "pi_pending",
        age_hours=25 if provider_step == "cancel" else 0,
    )
    cancelled = asyncio.Event()
    started = asyncio.Event()

    async def hang(*args: Any, **params: Any) -> FakeStripeObject:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(stripe.PaymentIntent, f"{provider_step}_async", hang)
    normal_budget = payments.RESERVATION_PROVIDER_TIMEOUT_SECONDS
    monkeypatch.setattr(payments, "RESERVATION_PROVIDER_TIMEOUT_SECONDS", .2)
    before = time.monotonic()
    response = await client.post(f"/payments/dues/{cycle}/intent", json={"rail": "ach"}, headers=setup.member.headers)
    assert response.status_code == 503, response.text
    assert response.json()["detail"] == "payment_outcome_unconfirmed"
    assert started.is_set() and cancelled.is_set()
    assert time.monotonic() - before < 2
    rows = await _rows(cycle)
    assert len(rows) == 1 and str(rows[0]["id"]) == original
    assert rows[0]["status"] == "open"
    assert get_engine().pool.checkedout() == 0

    async def recover(*args: Any, **params: Any) -> FakeStripeObject:
        return FakeStripeObject(id="pi_pending", client_secret="pi_pending_secret", status="processing")

    monkeypatch.setattr(stripe.PaymentIntent, f"{provider_step}_async", recover)
    # A non-canceled provider response must not expire an aged reservation.
    monkeypatch.setattr(stripe.PaymentIntent, "retrieve_async", recover)
    # The shortened budget already proved actual timeout/cancellation above.
    # Recovery uses the real budget; laptop DB scheduling is not a 200ms contract.
    monkeypatch.setattr(payments, "RESERVATION_PROVIDER_TIMEOUT_SECONDS", normal_budget)
    retry = await client.post(f"/payments/dues/{cycle}/intent", json={"rail": "ach"}, headers=setup.member.headers)
    assert retry.status_code == 200, retry.text
    assert retry.json()["payment_intent_status"] == "processing"
    assert len(await _rows(cycle)) == 1


async def test_cancel_and_fallback_retrieve_share_one_budget(
    client: AsyncClient, make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch, stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle = await _create_dues_cycle(client, setup)
    await _seed_reservation(setup.chapter_id, cycle, setup.member.id, "ach", "open", stripe_payment_intent_id="pi_aged", age_hours=25)
    provider_started: float | None = None
    retrieved = asyncio.Event()

    async def refuse_cancel(*args: Any, **params: Any) -> FakeStripeObject:
        nonlocal provider_started
        provider_started = time.monotonic()
        await asyncio.sleep(.3)
        raise stripe.APIConnectionError("fixture cancellation response lost")

    async def hang_retrieve(*args: Any, **params: Any) -> FakeStripeObject:
        retrieved.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(payments, "RESERVATION_PROVIDER_TIMEOUT_SECONDS", .5)
    monkeypatch.setattr(stripe.PaymentIntent, "cancel_async", refuse_cancel)
    monkeypatch.setattr(stripe.PaymentIntent, "retrieve_async", hang_retrieve)
    response = await client.post(f"/payments/dues/{cycle}/intent", json={"rail": "ach"}, headers=setup.member.headers)
    assert response.status_code == 503, response.text
    assert provider_started is not None and retrieved.is_set()
    # A reset budget would take at least .8s. This is fixture timing, not a
    # claim of a hard CPU/whole-endpoint deadline on a loaded deployment.
    assert time.monotonic() - provider_started < .7
    assert (await _rows(cycle))[0]["status"] == "open"


async def test_customer_session_provider_call_holds_no_database_connection(
    client: AsyncClient, make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch, stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle = await _create_dues_cycle(client, setup)
    checked_out: list[int] = []

    async def customer_session(**params: Any) -> FakeStripeObject:
        checked_out.append(get_engine().pool.checkedout())
        return FakeStripeObject(client_secret="cuss_fixture")

    monkeypatch.setattr(stripe.CustomerSession, "create_async", customer_session)
    for _ in range(2):
        response = await client.post(f"/payments/dues/{cycle}/intent", json={"rail": "ach"}, headers=setup.member.headers)
        assert response.status_code == 200, response.text
    assert checked_out == [0, 0], "both create and retrieve must release the pool slot"
    assert len(stripe_calls["payment_intent"]) == 1
    assert len(stripe_calls["payment_intent_retrieve"]) == 1


@pytest.mark.parametrize("provider_step", ["cancel", "fallback_retrieve", "retrieve"])
async def test_wrong_provider_intent_id_cannot_release_or_replace_stored_intent(
    client: AsyncClient, make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch, stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]], provider_step: str,
) -> None:
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle = await _create_dues_cycle(client, setup)
    original = await _seed_reservation(
        setup.chapter_id, cycle, setup.member.id, "ach", "open",
        stripe_payment_intent_id="pi_stored", age_hours=0 if provider_step == "retrieve" else 25,
    )

    async def wrong_intent(*args: Any, **params: Any) -> FakeStripeObject:
        return FakeStripeObject(id="pi_someone_else", status="canceled", client_secret="SECRET_other_intent")

    async def refusal(*args: Any, **params: Any) -> FakeStripeObject:
        raise stripe.APIConnectionError("fixture cancellation response lost")

    if provider_step == "cancel":
        monkeypatch.setattr(stripe.PaymentIntent, "cancel_async", wrong_intent)
    else:
        monkeypatch.setattr(stripe.PaymentIntent, "retrieve_async", wrong_intent)
        if provider_step == "fallback_retrieve":
            monkeypatch.setattr(stripe.PaymentIntent, "cancel_async", refusal)
    rail = "ach" if provider_step == "retrieve" else "card"
    response = await client.post(f"/payments/dues/{cycle}/intent", json={"rail": rail}, headers=setup.member.headers)
    assert response.status_code == (503 if provider_step == "retrieve" else 409), response.text
    assert "SECRET" not in response.text
    rows = await _rows(cycle)
    assert len(rows) == 1 and str(rows[0]["id"]) == original
    assert rows[0]["status"] == "open" and rows[0]["stripe_payment_intent_id"] == "pi_stored"
    assert stripe_calls["payment_intent"] == []


@pytest.mark.parametrize("sqlstate", ["23514", "23505"])
async def test_unrelated_binding_constraint_failure_retains_reservation_and_original_key(
    client: AsyncClient, make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch, stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]], sqlstate: str,
) -> None:
    """Real PostgreSQL failures after provider success are not duplicate-ID proof."""
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle = await _create_dues_cycle(client, setup)
    created: dict[str, str] = {}
    keys: list[str] = []

    async def create(**params: Any) -> FakeStripeObject:
        key = params["idempotency_key"]
        keys.append(key)
        intent_id = created.setdefault(key, f"pi_created_{len(created) + 1}")
        return FakeStripeObject(id=intent_id, client_secret=f"{intent_id}_secret")

    monkeypatch.setattr(stripe.PaymentIntent, "create_async", create)
    async with get_session_factory()() as session:
        # Static test-only values: exercise actual asyncpg diagnostic wrapping,
        # including another unique violation with the same SQLSTATE as c231.
        await session.execute(text(
            "CREATE FUNCTION c366_reject_binding() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'test binding constraint failure' "
            f"USING ERRCODE='{sqlstate}', CONSTRAINT='c366_other_binding_constraint'; END $$"
        ))
        await session.execute(text(
            "CREATE TRIGGER c366_reject_binding BEFORE UPDATE OF stripe_payment_intent_id "
            "ON dues_payment_intents FOR EACH ROW EXECUTE FUNCTION c366_reject_binding()"
        ))
        await session.commit()

    endpoint = f"/payments/dues/{cycle}/intent"
    database_error: IntegrityError | None = None
    try:
        try:
            await client.post(endpoint, json={"rail": "ach"}, headers=setup.member.headers)
        except IntegrityError as exc:
            database_error = exc
        original = await _rows(cycle)
        assert len(original) == 1
        assert original[0]["status"] == "open", original
        assert original[0]["stripe_payment_intent_id"] is None
        assert database_error is not None, "unrelated database errors must propagate"
        assert len(created) == 1, "the provider succeeded before PostgreSQL rejected binding"
        assert get_engine().pool.checkedout() == 0
    finally:
        async with get_session_factory()() as session:
            await session.execute(text("DROP TRIGGER c366_reject_binding ON dues_payment_intents"))
            await session.execute(text("DROP FUNCTION c366_reject_binding()"))
            await session.commit()

    blocked = await client.post(endpoint, json={"rail": "card"}, headers=setup.member.headers)
    assert blocked.status_code == 409
    retry = await client.post(endpoint, json={"rail": "ach"}, headers=setup.member.headers)
    assert retry.status_code == 200, retry.text
    rows = await _rows(cycle)
    assert len(rows) == 1 and rows[0]["id"] == original[0]["id"]
    assert len(keys) == 2 and keys[0] == keys[1] and len(created) == 1
    assert rows[0]["stripe_payment_intent_id"] == "pi_created_1"
    assert retry.json()["payment_intent_client_secret"] == "pi_created_1_secret"
