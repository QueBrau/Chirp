"""Payment routes enroll Stripe mutations before I/O when C436 admission is on."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import uuid

import pytest
import stripe
from sqlalchemy import event, select

from app import models
from app.config import get_settings
from app.db import get_engine, get_session_factory
from app.services.account_deletion_hold import activate_deletion_hold
from tests.test_payments import (
    _create_dues_cycle,
    _onboard,
    _post_webhook,
    _reserved_intent_id,
    _seed_reservation,
    _succeeded_event,
)


class _StripeObject:
    def __init__(self, **fields):
        self.__dict__.update(fields)


@pytest.fixture
def stripe_env(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    monkeypatch.setenv("STRIPE_PUBLISHABLE_KEY", "pk_test_fake")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_fake")
    monkeypatch.setenv("APP_PUBLIC_BASE_URL", "https://app.chirp.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def stripe_calls(monkeypatch):
    calls = {
        "account_create": [], "account_link": [], "customer_create": [],
        "customer_session": [], "payment_intent": [],
        "payment_intent_retrieve": [], "payment_intent_cancel": [],
    }
    intents = {}

    async def account_create(**params):
        calls["account_create"].append(params)
        return _StripeObject(object="account", id=f"acct_{uuid.uuid4().hex[:12]}")

    async def account_link(**params):
        calls["account_link"].append(params)
        return _StripeObject(
            object="account_link", url="https://connect.stripe.com/setup/abc",
            expires_at=int(datetime.now(timezone.utc).timestamp()) + 300,
        )

    async def account_retrieve(account_id, **params):
        return _StripeObject(id=account_id, charges_enabled=True, details_submitted=True)

    async def customer_create(**params):
        calls["customer_create"].append(params)
        return _StripeObject(object="customer", id=f"cus_{uuid.uuid4().hex[:12]}")

    async def customer_session(**params):
        calls["customer_session"].append(params)
        return _StripeObject(
            object="customer_session", customer=params["customer"],
            client_secret="cuss_secret_fake",
        )

    async def payment_intent(**params):
        calls["payment_intent"].append(params)
        intent_id = f"pi_{uuid.uuid4().hex[:12]}"
        secret = f"{intent_id}_secret"
        intents[intent_id] = secret
        return _StripeObject(
            id=intent_id, object="payment_intent", client_secret=secret,
            status="requires_payment_method",
        )

    async def payment_intent_retrieve(intent_id, **params):
        calls["payment_intent_retrieve"].append({"id": intent_id, **params})
        return _StripeObject(
            id=intent_id,
            client_secret=intents.get(intent_id, "pi_secret"),
            status="requires_payment_method",
        )

    async def payment_intent_cancel(intent_id, **params):
        calls["payment_intent_cancel"].append({"id": intent_id, **params})
        return _StripeObject(id=intent_id, status="canceled")

    monkeypatch.setattr(stripe.Account, "create_async", account_create)
    monkeypatch.setattr(stripe.Account, "retrieve_async", account_retrieve)
    monkeypatch.setattr(stripe.AccountLink, "create_async", account_link)
    monkeypatch.setattr(stripe.Customer, "create_async", customer_create)
    monkeypatch.setattr(stripe.CustomerSession, "create_async", customer_session)
    monkeypatch.setattr(stripe.PaymentIntent, "create_async", payment_intent)
    monkeypatch.setattr(stripe.PaymentIntent, "retrieve_async", payment_intent_retrieve)
    monkeypatch.setattr(stripe.PaymentIntent, "cancel_async", payment_intent_cancel)
    return calls


def _set_admission(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setenv("ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED", str(enabled).lower())
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    yield
    get_settings.cache_clear()


async def _activate_hold(user_id: str) -> None:
    async with get_session_factory()() as session:
        request = models.AccountDataRequest(
            user_id=uuid.UUID(user_id), kind="deletion", open_key=None
        )
        session.add(request)
        await session.flush()
        request_id = request.id
        await session.commit()
    async with get_session_factory()() as session:
        await activate_deletion_hold(
            session, user_id=uuid.UUID(user_id), request_id=request_id
        )
        await session.commit()


async def _rows_for_actor(user_id: str):
    async with get_session_factory()() as session:
        parents = list((await session.scalars(
            select(models.AccountExternalOperation).where(
                models.AccountExternalOperation.actor_user_id == uuid.UUID(user_id),
                models.AccountExternalOperation.kind == "payment_request",
            )
        )).all())
        attempts = list((await session.scalars(
            select(models.AccountPaymentAttempt).where(
                models.AccountPaymentAttempt.actor_user_id == uuid.UUID(user_id)
            )
        )).all())
        effects = list((await session.scalars(
            select(models.AccountPaymentEffect).where(
                models.AccountPaymentEffect.id.in_(
                    select(models.AccountPaymentAttempt.effect_id).where(
                        models.AccountPaymentAttempt.actor_user_id == uuid.UUID(user_id)
                    )
                )
            )
        )).all())
        return parents, attempts, effects


@pytest.mark.asyncio
async def test_onboarding_tracks_account_and_link_until_capability_handoff(
    client, make_chapter_with, stripe_env, stripe_calls, monkeypatch,
):
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with(role="treasurer")

    response = await client.post(
        "/payments/connect/onboarding-link",
        json={"chapter_id": setup.chapter_id},
        headers=setup.president.headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["url"].startswith("https://connect.stripe.com/")
    assert len(stripe_calls["account_create"]) == 1
    assert len(stripe_calls["account_link"]) == 1
    assert stripe_calls["account_create"][0]["idempotency_key"].startswith(
        "chirp:connect_account_create:"
    )
    parents, attempts, effects = await _rows_for_actor(setup.president.id)
    assert len(parents) == 1 and parents[0].state == "finished"
    assert len(attempts) == 2 and {row.state for row in attempts} == {"finished"}
    assert {row.kind for row in effects} == {"connect_account_create", "connect_account_link"}
    account_effect = next(row for row in effects if row.kind == "connect_account_create")
    assert account_effect.provider_object_id == await _stripe_account_id(setup.chapter_id)
    link_effect = next(row for row in effects if row.kind == "connect_account_link")
    assert link_effect.provider_object_id is None


@pytest.mark.asyncio
async def test_dues_request_tracks_customer_intent_and_session_with_reservation_key(
    client, make_chapter_with, stripe_env, stripe_calls, monkeypatch,
):
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle_id = await _create_dues_cycle(client, setup)

    response = await client.post(
        f"/payments/dues/{cycle_id}/intent",
        json={"rail": "card"},
        headers=setup.member.headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["payment_intent_client_secret"].startswith("pi_")
    assert response.json()["customer_session_client_secret"] == "cuss_secret_fake"
    assert len(stripe_calls["customer_create"]) == 1
    assert len(stripe_calls["payment_intent"]) == 1
    assert len(stripe_calls["customer_session"]) == 1
    async with get_session_factory()() as session:
        reservation = await session.scalar(select(models.DuesPaymentIntent).where(
            models.DuesPaymentIntent.user_id == uuid.UUID(setup.member.id),
            models.DuesPaymentIntent.dues_cycle_id == uuid.UUID(cycle_id),
        ))
        assert reservation is not None
        assert stripe_calls["payment_intent"][0]["idempotency_key"] == (
            f"dues:{cycle_id}:{setup.member.id}:card:{reservation.id}"
        )
    parents, attempts, effects = await _rows_for_actor(setup.member.id)
    assert len(parents) == 1 and parents[0].state == "finished"
    assert {row.state for row in attempts} == {"finished"}
    assert {row.kind for row in effects} == {
        "customer_create", "payment_intent_create", "customer_session_create"
    }
    intent_effect = next(row for row in effects if row.kind == "payment_intent_create")
    assert intent_effect.provider_object_id == reservation.stripe_payment_intent_id


@pytest.mark.asyncio
async def test_held_member_is_rejected_before_any_payment_provider_mutation(
    client, make_chapter_with, stripe_env, stripe_calls, monkeypatch,
):
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle_id = await _create_dues_cycle(client, setup)
    before = tuple(len(stripe_calls[key]) for key in (
        "customer_create", "payment_intent", "customer_session"
    ))
    await _activate_hold(setup.member.id)

    response = await client.post(
        f"/payments/dues/{cycle_id}/intent",
        json={"rail": "card"},
        headers=setup.member.headers,
    )

    assert response.status_code == 423, response.text
    assert response.json()["detail"] == "account_deletion_in_progress"
    assert tuple(len(stripe_calls[key]) for key in (
        "customer_create", "payment_intent", "customer_session"
    )) == before
    parents, attempts, _effects = await _rows_for_actor(setup.member.id)
    assert parents == [] and attempts == []


@pytest.mark.asyncio
async def test_hold_during_customer_session_withholds_capability_after_provider_returns(
    client, make_chapter_with, stripe_env, stripe_calls, monkeypatch,
):
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle_id = await _create_dues_cycle(client, setup)
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_customer_session(**params):
        stripe_calls["customer_session"].append(params)
        started.set()
        await release.wait()
        return type("Session", (), {
            "object": "customer_session", "customer": params["customer"],
            "client_secret": "cuss_secret_withheld",
        })()

    monkeypatch.setattr(stripe.CustomerSession, "create_async", blocked_customer_session)
    request = asyncio.create_task(client.post(
        f"/payments/dues/{cycle_id}/intent",
        json={"rail": "card"},
        headers=setup.member.headers,
    ))
    await asyncio.wait_for(started.wait(), timeout=5)
    await _activate_hold(setup.member.id)
    release.set()
    response = await asyncio.wait_for(request, timeout=5)

    assert response.status_code == 423, response.text
    assert response.json()["detail"] == "account_deletion_in_progress"
    assert "customer_session_client_secret" not in response.json()
    parents, attempts, _effects = await _rows_for_actor(setup.member.id)
    assert len(parents) == 1 and parents[0].state == "finished"
    assert attempts and {row.state for row in attempts} == {"finished"}


@pytest.mark.asyncio
async def test_flag_off_payment_path_does_not_query_admission_tables(
    client, make_chapter_with, stripe_env, stripe_calls, monkeypatch,
):
    _set_admission(monkeypatch, False)
    setup = await make_chapter_with(role="treasurer")
    statements = []

    def record_sql(_conn, _cursor, statement, _parameters, _context, _many):
        if "account_payment_" in statement or "account_external_operation" in statement:
            statements.append(statement)

    engine = get_engine()
    event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
    try:
        response = await client.post(
            "/payments/connect/onboarding-link",
            json={"chapter_id": setup.chapter_id},
            headers=setup.president.headers,
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_sql)
    assert response.status_code == 200, response.text
    assert statements == []
    assert stripe_calls["account_create"][0].get("idempotency_key") is None


@pytest.mark.asyncio
async def test_customer_create_candidate_losing_unique_binding_stays_uncertain(
    client, make_chapter_with, stripe_env, stripe_calls, monkeypatch,
):
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle_id = await _create_dues_cycle(client, setup)
    candidate = "cus_candidate"
    competitor = "cus_competitor"

    async def create_customer_with_competing_winner(**params):
        stripe_calls["customer_create"].append(params)
        async with get_session_factory()() as session:
            session.add(models.ChapterStripeCustomer(
                user_id=uuid.UUID(setup.member.id),
                chapter_id=uuid.UUID(setup.chapter_id),
                stripe_customer_id=competitor,
            ))
            await session.commit()
        return _StripeObject(object="customer", id=candidate)

    monkeypatch.setattr(stripe.Customer, "create_async", create_customer_with_competing_winner)
    response = await client.post(
        f"/payments/dues/{cycle_id}/intent",
        json={"rail": "card"},
        headers=setup.member.headers,
    )

    assert response.status_code == 503, response.text
    assert response.json()["detail"] == "payment_outcome_unconfirmed"
    assert stripe_calls["customer_create"]
    assert stripe_calls["payment_intent"] == []
    assert stripe_calls["customer_session"] == []
    async with get_session_factory()() as session:
        bound_customer = await session.scalar(select(models.ChapterStripeCustomer.stripe_customer_id).where(
            models.ChapterStripeCustomer.user_id == uuid.UUID(setup.member.id),
            models.ChapterStripeCustomer.chapter_id == uuid.UUID(setup.chapter_id),
        ))
    assert bound_customer == competitor
    parents, attempts, effects = await _rows_for_actor(setup.member.id)
    assert len(parents) == 1 and parents[0].state == "finished"
    assert len(attempts) == 1 and attempts[0].state == "uncertain"
    assert len(effects) == 1 and effects[0].provider_object_id is None


async def _prepare_enabled_member(client, setup, monkeypatch):
    _set_admission(monkeypatch, True)
    await _onboard(client, setup)
    return await _create_dues_cycle(client, setup)


@pytest.mark.asyncio
async def test_same_rail_nowait_loser_does_not_make_a_second_intent(
    client, make_chapter_with, stripe_env, stripe_calls, monkeypatch,
):
    setup = await make_chapter_with(role="member")
    cycle_id = await _prepare_enabled_member(client, setup, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_create(**params):
        stripe_calls["payment_intent"].append(params)
        entered.set()
        await release.wait()
        return _StripeObject(
            object="payment_intent", id="pi_race123", client_secret="pi_race123_secret",
            status="requires_payment_method",
        )

    monkeypatch.setattr(stripe.PaymentIntent, "create_async", blocked_create)
    first = asyncio.create_task(client.post(
        f"/payments/dues/{cycle_id}/intent", json={"rail": "card"},
        headers=setup.member.headers,
    ))
    await asyncio.wait_for(entered.wait(), 5)
    second = await asyncio.wait_for(client.post(
        f"/payments/dues/{cycle_id}/intent", json={"rail": "card"},
        headers=setup.member.headers,
    ), 5)
    release.set()
    first_response = await asyncio.wait_for(first, 5)

    assert second.status_code == 409 and second.json()["detail"] == "payment_already_in_progress"
    assert first_response.status_code == 200, first_response.text
    assert len(stripe_calls["payment_intent"]) == 1
    parents, attempts, _ = await _rows_for_actor(setup.member.id)
    assert len(parents) == 2 and all(parent.state == "finished" for parent in parents)
    assert len(attempts) == 3 and all(row.state == "finished" for row in attempts)


@pytest.mark.asyncio
async def test_lost_create_response_retries_with_same_reservation_key_and_object(
    client, make_chapter_with, stripe_env, stripe_calls, monkeypatch,
):
    setup = await make_chapter_with(role="member")
    cycle_id = await _prepare_enabled_member(client, setup, monkeypatch)
    response_object = _StripeObject(
        object="payment_intent", id="pi_lost123", client_secret="pi_lost123_secret",
        status="requires_payment_method",
    )

    async def accepted_but_response_lost(**params):
        stripe_calls["payment_intent"].append(params)
        raise stripe.APIConnectionError("response lost after provider acceptance")

    monkeypatch.setattr(stripe.PaymentIntent, "create_async", accepted_but_response_lost)
    first = await client.post(
        f"/payments/dues/{cycle_id}/intent", json={"rail": "ach"},
        headers=setup.member.headers,
    )
    assert first.status_code == 503, first.text
    parents, attempts, effects = await _rows_for_actor(setup.member.id)
    assert len(parents) == 1 and parents[0].state == "finished"
    pi_attempt = next(row for row in attempts if next(
        effect for effect in effects if effect.id == row.effect_id
    ).kind == "payment_intent_create")
    assert pi_attempt.state == "uncertain"

    async def replay_same_key(**params):
        stripe_calls["payment_intent"].append(params)
        return response_object

    monkeypatch.setattr(stripe.PaymentIntent, "create_async", replay_same_key)
    retry = await client.post(
        f"/payments/dues/{cycle_id}/intent", json={"rail": "ach"},
        headers=setup.member.headers,
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["payment_intent_client_secret"] == response_object.client_secret
    assert len(stripe_calls["payment_intent"]) == 2
    assert stripe_calls["payment_intent"][0]["idempotency_key"] == stripe_calls["payment_intent"][1]["idempotency_key"]
    assert stripe_calls["payment_intent"][0]["idempotency_key"].startswith(f"dues:{cycle_id}:")
    _parents, attempts, effects = await _rows_for_actor(setup.member.id)
    pi_effect = next(row for row in effects if row.kind == "payment_intent_create")
    assert pi_effect.provider_object_id == response_object.id
    assert sum(row.state == "reconciled" for row in attempts) == 1


@pytest.mark.asyncio
async def test_http_cancellation_marks_create_uncertain_then_same_key_retry_recovers(
    client, make_chapter_with, stripe_env, stripe_calls, monkeypatch,
):
    setup = await make_chapter_with(role="member")
    cycle_id = await _prepare_enabled_member(client, setup, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    async def canceled_after_acceptance(**params):
        stripe_calls["payment_intent"].append(params)
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            raise
        return _StripeObject(
            object="payment_intent", id="pi_cancel123", client_secret="pi_cancel123_secret",
            status="requires_payment_method",
        )

    monkeypatch.setattr(stripe.PaymentIntent, "create_async", canceled_after_acceptance)
    request = asyncio.create_task(client.post(
        f"/payments/dues/{cycle_id}/intent", json={"rail": "card"},
        headers=setup.member.headers,
    ))
    await asyncio.wait_for(entered.wait(), 5)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    parents, attempts, effects = await _rows_for_actor(setup.member.id)
    assert len(parents) == 1 and parents[0].state == "finished"
    pi_effect = next(row for row in effects if row.kind == "payment_intent_create")
    assert pi_effect.provider_object_id is None
    assert next(row for row in attempts if row.effect_id == pi_effect.id).state == "uncertain"

    async def recover(**params):
        stripe_calls["payment_intent"].append(params)
        return _StripeObject(
            object="payment_intent", id="pi_cancel123", client_secret="pi_cancel123_secret",
            status="requires_payment_method",
        )

    monkeypatch.setattr(stripe.PaymentIntent, "create_async", recover)
    retry = await client.post(
        f"/payments/dues/{cycle_id}/intent", json={"rail": "card"},
        headers=setup.member.headers,
    )
    assert retry.status_code == 200, retry.text
    assert len(stripe_calls["payment_intent"]) == 2
    assert stripe_calls["payment_intent"][0]["idempotency_key"] == stripe_calls["payment_intent"][1]["idempotency_key"]


@pytest.mark.asyncio
async def test_provider_success_then_binding_failure_retries_same_object(
    client, make_chapter_with, stripe_env, stripe_calls, monkeypatch,
):
    setup = await make_chapter_with(role="member")
    cycle_id = await _prepare_enabled_member(client, setup, monkeypatch)
    provider_object = _StripeObject(
        object="payment_intent", id="pi_bind123", client_secret="pi_bind123_secret",
        status="requires_payment_method",
    )

    async def same_object(**params):
        stripe_calls["payment_intent"].append(params)
        return provider_object

    monkeypatch.setattr(stripe.PaymentIntent, "create_async", same_object)
    from app.routers import payments
    original_finish = payments.finish_payment_attempt
    failed = False

    async def fail_first_binding(session, parent, effect, attempt, **kwargs):
        nonlocal failed
        if effect.kind == "payment_intent_create" and not failed:
            failed = True
            raise RuntimeError("simulated payment binding failure")
        return await original_finish(session, parent, effect, attempt, **kwargs)

    monkeypatch.setattr(payments, "finish_payment_attempt", fail_first_binding)
    with pytest.raises(RuntimeError, match="simulated payment binding failure"):
        await client.post(
            f"/payments/dues/{cycle_id}/intent", json={"rail": "ach"},
            headers=setup.member.headers,
        )
    parents, attempts, effects = await _rows_for_actor(setup.member.id)
    pi_effect = next(row for row in effects if row.kind == "payment_intent_create")
    assert pi_effect.provider_object_id is None
    assert any(row.effect_id == pi_effect.id and row.state == "uncertain" for row in attempts)

    monkeypatch.setattr(payments, "finish_payment_attempt", original_finish)
    retry = await client.post(
        f"/payments/dues/{cycle_id}/intent", json={"rail": "ach"},
        headers=setup.member.headers,
    )
    assert retry.status_code == 200, retry.text
    assert len(stripe_calls["payment_intent"]) == 2
    assert stripe_calls["payment_intent"][0]["idempotency_key"] == stripe_calls["payment_intent"][1]["idempotency_key"]
    assert await _reserved_intent_id(cycle_id, setup.member.id) == provider_object.id
    _parents, attempts, effects = await _rows_for_actor(setup.member.id)
    pi_effect = next(row for row in effects if row.kind == "payment_intent_create")
    assert pi_effect.provider_object_id == provider_object.id


@pytest.mark.asyncio
async def test_failed_known_intent_reuse_does_not_create_or_allow_cross_rail(
    client, make_chapter_with, stripe_env, stripe_calls, monkeypatch,
):
    setup = await make_chapter_with(role="member")
    cycle_id = await _prepare_enabled_member(client, setup, monkeypatch)
    first = await client.post(
        f"/payments/dues/{cycle_id}/intent", json={"rail": "ach"},
        headers=setup.member.headers,
    )
    assert first.status_code == 200, first.text
    intent_id = await _reserved_intent_id(cycle_id, setup.member.id)
    assert intent_id
    failed_event = dict(await _succeeded_event(cycle_id, setup.member.id, intent_id, "evt_c436_fail"))
    failed_event["type"] = "payment_intent.payment_failed"
    assert (await _post_webhook(client, failed_event)).status_code == 200

    cross_rail = await client.post(
        f"/payments/dues/{cycle_id}/intent", json={"rail": "card"},
        headers=setup.member.headers,
    )
    assert cross_rail.status_code == 409
    assert cross_rail.json()["detail"] == "payment_already_in_progress"
    same_rail = await client.post(
        f"/payments/dues/{cycle_id}/intent", json={"rail": "ach"},
        headers=setup.member.headers,
    )
    assert same_rail.status_code == 200, same_rail.text
    assert await _reserved_intent_id(cycle_id, setup.member.id) == intent_id
    assert len(stripe_calls["payment_intent"]) == 1
    assert len(stripe_calls["payment_intent_retrieve"]) == 1


@pytest.mark.asyncio
async def test_non_canceled_cancel_response_does_not_clear_prior_uncertainty(
    client, make_chapter_with, stripe_env, stripe_calls, monkeypatch,
):
    setup = await make_chapter_with(role="member")
    cycle_id = await _prepare_enabled_member(client, setup, monkeypatch)
    await _seed_reservation(
        setup.chapter_id, cycle_id, setup.member.id, "ach", "open",
        stripe_payment_intent_id="pi_old123", age_hours=25,
    )
    cancel_states = iter(("requires_payment_method", "canceled"))

    async def non_canceled_then_canceled(intent_id, **params):
        stripe_calls["payment_intent_cancel"].append({"id": intent_id, **params})
        return _StripeObject(id=intent_id, status=next(cancel_states))

    monkeypatch.setattr(stripe.PaymentIntent, "cancel_async", non_canceled_then_canceled)
    first = await client.post(
        f"/payments/dues/{cycle_id}/intent", json={"rail": "card"},
        headers=setup.member.headers,
    )
    assert first.status_code == 409
    assert first.json()["detail"] == "payment_already_in_progress"
    parents, attempts, effects = await _rows_for_actor(setup.member.id)
    cancel_effect = next(row for row in effects if row.kind == "payment_intent_cancel")
    cancel_attempt = next(row for row in attempts if row.effect_id == cancel_effect.id)
    assert cancel_attempt.state == "uncertain"
    assert cancel_effect.provider_object_id is None

    second = await client.post(
        f"/payments/dues/{cycle_id}/intent", json={"rail": "card"},
        headers=setup.member.headers,
    )
    assert second.status_code == 200, second.text
    _parents, attempts, effects = await _rows_for_actor(setup.member.id)
    cancel_effect = next(row for row in effects if row.kind == "payment_intent_cancel")
    cancel_attempts = [row for row in attempts if row.effect_id == cancel_effect.id]
    assert len(cancel_attempts) == 2
    assert {row.state for row in cancel_attempts} == {"finished", "reconciled"}
    assert cancel_effect.provider_object_id == "pi_old123"
    assert len(stripe_calls["payment_intent_cancel"]) == 2


@pytest.mark.asyncio
async def test_lost_cancel_response_with_noncanceled_readback_stays_uncertain(
    client, make_chapter_with, stripe_env, stripe_calls, monkeypatch,
):
    setup = await make_chapter_with(role="member")
    cycle_id = await _prepare_enabled_member(client, setup, monkeypatch)
    await _seed_reservation(
        setup.chapter_id, cycle_id, setup.member.id, "ach", "open",
        stripe_payment_intent_id="pi_readback123", age_hours=25,
    )

    async def lost_cancel(intent_id, **params):
        stripe_calls["payment_intent_cancel"].append({"id": intent_id, **params})
        raise stripe.APIConnectionError("cancel response lost")

    async def still_active(intent_id, **params):
        stripe_calls["payment_intent_retrieve"].append({"id": intent_id, **params})
        return _StripeObject(
            id=intent_id, client_secret="pi_readback123_secret", status="processing"
        )

    monkeypatch.setattr(stripe.PaymentIntent, "cancel_async", lost_cancel)
    monkeypatch.setattr(stripe.PaymentIntent, "retrieve_async", still_active)
    response = await client.post(
        f"/payments/dues/{cycle_id}/intent", json={"rail": "card"},
        headers=setup.member.headers,
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "payment_already_in_progress"
    _parents, attempts, effects = await _rows_for_actor(setup.member.id)
    cancel_effect = next(row for row in effects if row.kind == "payment_intent_cancel")
    cancel_attempt = next(row for row in attempts if row.effect_id == cancel_effect.id)
    assert cancel_attempt.state == "uncertain"
    assert cancel_effect.provider_object_id is None
    assert len(stripe_calls["payment_intent_cancel"]) == 1
    assert len(stripe_calls["payment_intent_retrieve"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url,expiry",
    [
        ("https://evil.example/setup/abc", int(datetime.now(timezone.utc).timestamp()) + 300),
        ("https://connect.stripe.com:8443/setup/abc", int(datetime.now(timezone.utc).timestamp()) + 300),
        ("https://connect.stripe.com/setup/abc", int(datetime.now(timezone.utc).timestamp()) - 1),
        ("https://connect.stripe.com/setup/abc", True),
    ],
)
async def test_invalid_connect_link_never_hands_out_capability_and_stays_uncertain(
    client, make_chapter_with, stripe_env, stripe_calls, monkeypatch, url, expiry,
):
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with(role="treasurer")

    async def invalid_link(**params):
        return _StripeObject(object="account_link", url=url, expires_at=expiry)

    monkeypatch.setattr(stripe.AccountLink, "create_async", invalid_link)
    response = await client.post(
        "/payments/connect/onboarding-link",
        json={"chapter_id": setup.chapter_id},
        headers=setup.president.headers,
    )

    assert response.status_code == 503, response.text
    assert "url" not in response.json()
    parents, attempts, effects = await _rows_for_actor(setup.president.id)
    assert len(parents) == 1 and parents[0].state == "finished"
    link_effect = next(row for row in effects if row.kind == "connect_account_link")
    assert link_effect.provider_object_id is None
    link_attempt = next(row for row in attempts if row.effect_id == link_effect.id)
    assert link_attempt.state == "uncertain"


async def _stripe_account_id(chapter_id: str) -> str | None:
    async with get_session_factory()() as session:
        return await session.scalar(select(models.Chapter.stripe_account_id).where(
            models.Chapter.id == uuid.UUID(chapter_id)
        ))
