"""Stripe dues flow: fee/rail locking, onboarding gating, and webhook replay safety.

The Stripe SDK is monkeypatched at the `stripe.*` boundary rather than at our own
service functions, so these tests still cover the params we hand Stripe (rail,
application fee, connected account) — the part that moves real money.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import stripe
from httpx import AsyncClient
from sqlalchemy import text

from app.config import get_settings
from app.services import stripe_service
from tests.conftest import ChapterSetup, MakeChapterWith, MakeUser


class FakeStripeObject:
    """Stand-in for a Stripe resource: attribute access over a plain dict."""

    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)


@pytest.fixture
def stripe_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Configure the Stripe keys + public base URL the payments routes require."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    monkeypatch.setenv("STRIPE_PUBLISHABLE_KEY", "pk_test_fake")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_fake")
    monkeypatch.setenv("APP_PUBLIC_BASE_URL", "https://app.chirp.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def stripe_calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict[str, Any]]]:
    """Record every Stripe SDK call and return canned resources."""
    calls: dict[str, list[dict[str, Any]]] = {
        "account_create": [],
        "account_link": [],
        "customer_create": [],
        "customer_session": [],
        "payment_intent": [],
        "payment_intent_retrieve": [],
        "payment_intent_cancel": [],
    }
    # intent id -> client_secret, so the retrieve fake can hand back the SAME
    # secret a prior create returned (real Stripe would); keyed rather than a
    # constant so tests can tell "the original intent" apart from "a new one".
    created_intents: dict[str, str] = {}

    async def fake_account_create(**params: Any) -> FakeStripeObject:
        calls["account_create"].append(params)
        return FakeStripeObject(id=f"acct_{uuid.uuid4().hex[:12]}")

    async def fake_account_link_create(**params: Any) -> FakeStripeObject:
        calls["account_link"].append(params)
        return FakeStripeObject(
            url="https://connect.stripe.test/setup/abc", expires_at=1_800_000_000
        )

    async def fake_account_retrieve(account_id: str, **params: Any) -> FakeStripeObject:
        return FakeStripeObject(
            id=account_id, charges_enabled=True, details_submitted=True
        )

    async def fake_customer_create(**params: Any) -> FakeStripeObject:
        calls["customer_create"].append(params)
        return FakeStripeObject(id=f"cus_{uuid.uuid4().hex[:12]}")

    async def fake_customer_session_create(**params: Any) -> FakeStripeObject:
        calls["customer_session"].append(params)
        return FakeStripeObject(client_secret="cuss_secret_fake")

    async def fake_payment_intent_create(**params: Any) -> FakeStripeObject:
        calls["payment_intent"].append(params)
        intent_id = f"pi_{uuid.uuid4().hex[:12]}"
        client_secret = f"{intent_id}_secret"
        created_intents[intent_id] = client_secret
        return FakeStripeObject(id=intent_id, client_secret=client_secret)

    async def fake_payment_intent_cancel(intent_id: str, **params: Any) -> FakeStripeObject:
        calls["payment_intent_cancel"].append({"id": intent_id, **params})
        return FakeStripeObject(id=intent_id, status="canceled")

    async def fake_payment_intent_retrieve(intent_id: str, **params: Any) -> FakeStripeObject:
        """Default: behaves like real Stripe GET — same id, same client_secret as
        whatever create_async originally returned for it, still awaiting payment
        (status="requires_payment_method", same as a fresh create — c234 reads this
        field on the retrieve path). Individual tests override this via monkeypatch
        to simulate a retrieve failure, or a status that has moved past "awaiting
        payment" (see the c234 retrieve-path tests below)."""
        calls["payment_intent_retrieve"].append({"id": intent_id, **params})
        return FakeStripeObject(
            id=intent_id,
            client_secret=created_intents.get(intent_id, "pi_secret"),
            status="requires_payment_method",
        )

    monkeypatch.setattr(stripe.Account, "create_async", fake_account_create)
    monkeypatch.setattr(stripe.Account, "retrieve_async", fake_account_retrieve)
    monkeypatch.setattr(stripe.AccountLink, "create_async", fake_account_link_create)
    monkeypatch.setattr(stripe.Customer, "create_async", fake_customer_create)
    monkeypatch.setattr(
        stripe.CustomerSession, "create_async", fake_customer_session_create
    )
    monkeypatch.setattr(stripe.PaymentIntent, "create_async", fake_payment_intent_create)
    monkeypatch.setattr(stripe.PaymentIntent, "retrieve_async", fake_payment_intent_retrieve)
    monkeypatch.setattr(stripe.PaymentIntent, "cancel_async", fake_payment_intent_cancel)
    return calls


async def _create_dues_cycle(
    client: AsyncClient, setup: ChapterSetup, amount_cents: int = 25_000
) -> str:
    """Create a dues cycle as the president and return its id."""
    response = await client.post(
        f"/chapters/{setup.chapter_id}/dues-cycles",
        json={
            "name": "Spring 2027 Dues",
            "amount_cents": amount_cents,
            "due_date": "2027-03-01",
        },
        headers=setup.president.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _pay_on_ledger(
    client: AsyncClient, setup: ChapterSetup, cycle_id: str, user_id: str, cents: int = 25_000
) -> str:
    """Record a dues payment the way a treasurer enters a cash payment: straight onto
    the ledger, no Stripe involved. Same helper shape as test_chapter_overview.py's
    _pay — used here so a board c172 test can put a member in the "paid" state
    without ever creating a DuesPaymentIntent reservation, isolating the netted
    already_paid guard (payments.py) from the separate reservation-table gate (c51)."""
    response = await client.post(
        f"/chapters/{setup.chapter_id}/ledger",
        json={
            "entry_type": "dues_payment",
            "amount_cents": cents,
            "related_user_id": user_id,
            "dues_cycle_id": cycle_id,
        },
        headers=setup.president.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _correct(
    client: AsyncClient, setup: ChapterSetup, entry_id: str, cents: int
) -> None:
    """Append a correction against a prior ledger entry (SPEC 8.2) — a refund is a
    NEW row, never an update to the original."""
    response = await client.post(
        f"/chapters/{setup.chapter_id}/ledger",
        json={
            "entry_type": "correction",
            "amount_cents": cents,
            "corrects_entry_id": entry_id,
        },
        headers=setup.president.headers,
    )
    assert response.status_code == 201, response.text


async def _onboard(client: AsyncClient, setup: ChapterSetup) -> str:
    """Run onboarding as the president so the chapter has a connected account."""
    response = await client.post(
        "/payments/connect/onboarding-link",
        json={"chapter_id": setup.chapter_id},
        headers=setup.president.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["url"]


async def _stripe_account_id(chapter_id: str) -> str | None:
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        result = await session.execute(
            text("SELECT stripe_account_id FROM chapters WHERE id = :id"),
            {"id": chapter_id},
        )
        return result.scalar_one()


# ---------------------------------------------------------------------------
# Fee math
# ---------------------------------------------------------------------------


def test_platform_fee_is_one_percent_card_two_percent_ach() -> None:
    assert stripe_service.platform_fee_cents(25_000, "card") == 250
    assert stripe_service.platform_fee_cents(25_000, "ach") == 500


def test_platform_fee_floors_to_whole_cents() -> None:
    """Stripe rejects fractional cents; 1% of $1.99 must floor, not round up."""
    assert stripe_service.platform_fee_cents(199, "card") == 1


# ---------------------------------------------------------------------------
# Connect onboarding
# ---------------------------------------------------------------------------


async def test_onboarding_link_rejects_plain_member(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    setup = await make_chapter_with(role="member")
    response = await client.post(
        "/payments/connect/onboarding-link",
        json={"chapter_id": setup.chapter_id},
        headers=setup.member.headers,
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "insufficient_role"
    assert stripe_calls["account_create"] == []


async def test_onboarding_link_rejects_non_member(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    make_user: MakeUser,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    setup = await make_chapter_with(role="treasurer")
    outsider = await make_user("Rival Chapter Member")
    response = await client.post(
        "/payments/connect/onboarding-link",
        json={"chapter_id": setup.chapter_id},
        headers=outsider.headers,
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "not_a_member"


async def test_onboarding_creates_express_account_once_then_reuses_it(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    """A member who abandons onboarding must resume the SAME account, not orphan it."""
    setup = await make_chapter_with(role="treasurer")

    await _onboard(client, setup)
    account_id = await _stripe_account_id(setup.chapter_id)
    assert account_id is not None

    await _onboard(client, setup)
    assert len(stripe_calls["account_create"]) == 1
    assert len(stripe_calls["account_link"]) == 2
    assert await _stripe_account_id(setup.chapter_id) == account_id
    assert stripe_calls["account_create"][0]["type"] == "express"


async def test_onboarding_link_uses_https_return_urls(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    """Stripe rejects custom schemes — a chirp:// deep link here would fail at Stripe."""
    setup = await make_chapter_with(role="treasurer")
    await _onboard(client, setup)
    link_params = stripe_calls["account_link"][0]
    assert link_params["return_url"].startswith("https://")
    assert link_params["refresh_url"].startswith("https://")


async def test_payments_status_false_before_onboarding(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    setup = await make_chapter_with(role="member")
    response = await client.get(
        f"/chapters/{setup.chapter_id}/payments/status", headers=setup.member.headers
    )
    assert response.status_code == 200
    assert response.json() == {
        "onboarded": False,
        "charges_enabled": False,
        "details_submitted": False,
    }


async def test_payments_status_is_org_scoped(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    make_user: MakeUser,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    setup = await make_chapter_with(role="member")
    outsider = await make_user("Outsider")
    response = await client.get(
        f"/chapters/{setup.chapter_id}/payments/status", headers=outsider.headers
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Dues intents
# ---------------------------------------------------------------------------


async def test_dues_intent_locks_card_rail_and_one_percent_fee(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=25_000)

    response = await client.post(
        f"/payments/dues/{cycle_id}/intent",
        json={"rail": "card"},
        headers=setup.member.headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["amount_cents"] == 25_000
    assert body["application_fee_cents"] == 250
    assert body["rail"] == "card"
    assert body["customer_session_client_secret"] == "cuss_secret_fake"

    params = stripe_calls["payment_intent"][0]
    assert params["payment_method_types"] == ["card"]
    assert params["application_fee_amount"] == 250
    assert params["amount"] == 25_000


async def test_dues_intent_locks_ach_rail_and_two_percent_fee(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    """The rail is chosen before creation because application_fee_amount is immutable."""
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=25_000)

    response = await client.post(
        f"/payments/dues/{cycle_id}/intent",
        json={"rail": "ach"},
        headers=setup.member.headers,
    )
    assert response.status_code == 200, response.text
    params = stripe_calls["payment_intent"][0]
    assert params["payment_method_types"] == ["us_bank_account"]
    assert params["application_fee_amount"] == 500


async def test_dues_intent_charges_on_the_connected_account(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    """Direct charges: omitting stripe_account would bill the PLATFORM account instead."""
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle_id = await _create_dues_cycle(client, setup)
    await client.post(
        f"/payments/dues/{cycle_id}/intent",
        json={"rail": "card"},
        headers=setup.member.headers,
    )
    account_id = await _stripe_account_id(setup.chapter_id)
    assert stripe_calls["payment_intent"][0]["stripe_account"] == account_id
    assert stripe_calls["customer_create"][0]["stripe_account"] == account_id


async def test_dues_intent_is_idempotent_per_cycle_member_and_rail(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    """A client retry must resolve to the same intent, not create a second one.

    Post-c193: the retry does not lean on Stripe's idempotency layer at all — it
    retrieves the intent this endpoint already stored, because that layer only
    retains a key for 24h while ACH can sit in 'processing' for days (see the
    c51/c193 tests below for the case that actually bites).
    """
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle_id = await _create_dues_cycle(client, setup)

    responses = []
    for _ in range(2):
        responses.append(
            await client.post(
                f"/payments/dues/{cycle_id}/intent",
                json={"rail": "card"},
                headers=setup.member.headers,
            )
        )

    assert [r.status_code for r in responses] == [200, 200]
    # Only ONE create call ever reaches Stripe.
    assert len(stripe_calls["payment_intent"]) == 1
    assert cycle_id in stripe_calls["payment_intent"][0]["idempotency_key"]
    # The retry retrieved the SAME intent instead of creating a second one.
    assert len(stripe_calls["payment_intent_retrieve"]) == 1
    assert (
        responses[0].json()["payment_intent_client_secret"]
        == responses[1].json()["payment_intent_client_secret"]
    )


async def test_dues_intent_409_when_chapter_not_onboarded(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup)
    response = await client.post(
        f"/payments/dues/{cycle_id}/intent",
        json={"rail": "card"},
        headers=setup.member.headers,
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "chapter_not_onboarded"
    assert stripe_calls["payment_intent"] == []


async def test_dues_intent_409_when_charges_disabled(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    """Onboarding started but Stripe has not enabled charges yet — still not payable."""
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle_id = await _create_dues_cycle(client, setup)

    async def not_ready(account_id: str, **params: Any) -> FakeStripeObject:
        return FakeStripeObject(
            id=account_id, charges_enabled=False, details_submitted=True
        )

    monkeypatch.setattr(stripe.Account, "retrieve_async", not_ready)
    response = await client.post(
        f"/payments/dues/{cycle_id}/intent",
        json={"rail": "card"},
        headers=setup.member.headers,
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "chapter_not_onboarded"


async def test_dues_intent_409_after_the_member_already_paid(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle_id = await _create_dues_cycle(client, setup)
    await _post_webhook(client, _succeeded_event(cycle_id, setup.member.id))

    response = await client.post(
        f"/payments/dues/{cycle_id}/intent",
        json={"rail": "card"},
        headers=setup.member.headers,
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "already_paid"


# ---- c172: an honest reason for the same block, not a reopened charge path ----
#
# Before the fix, this endpoint's already_paid check read the mere EXISTENCE of a
# dues_payment row and never looked at corrections, while chapter_overview NETTED them
# per member. A member refunded in full showed as still owing on the President
# dashboard while their own pay button told them they had already paid.
#
# THE FIRST FIX ATTEMPT let the charge path itself net corrections and reopen for a
# fully-refunded member — reviewed and reverted (board c172) because it is a
# money-loss bug, not a fix: a member whose ORIGINAL payment was hand-entered by the
# treasurer has no DuesPaymentIntent reservation to block a retry, so netting alone
# let them create a fresh intent, pay through Stripe for real, and then hit
# uq_ledger_dues_payment_once (at most one dues_payment row per (cycle, member) EVER)
# when the webhook tried to record the second payment — money captured at Stripe,
# no ledger row, and _record_dues_payment's IntegrityError handling swallows exactly
# that as if it were a harmless replay. The test below is the one that would have
# caught it, rewritten to assert the correct (refused) outcome instead of stopping at
# a green "200, intent created" that never exercised the webhook leg where the loss
# actually happens.
#
# THE LANDED FIX keeps the charge path closed on existence — re-payment for one dues
# cycle is structurally unrepresentable in this ledger until dues status is modeled
# explicitly (c83-shaped) — and uses dues_contributions_subquery only to pick an
# honest 409 reason: already_paid (net > 0) or refunded_contact_treasurer (net <= 0).


async def test_dues_intent_refuses_with_an_honest_reason_after_a_full_refund(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    """THE TEST THAT WOULD HAVE CAUGHT THE MONEY-LOSS REGRESSION.

    A hand-entered original payment (no DuesPaymentIntent reservation exists to
    block a retry on its own) refunded in full. The charge path must REFUSE the
    retry outright — asserting refusal here, rather than a 200 that goes on to
    settle a real Stripe payment, is what keeps the webhook-settlement leg (where
    uq_ledger_dues_payment_once would silently swallow the second payment)
    unreachable in the first place.
    """
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=25_000)
    entry_id = await _pay_on_ledger(client, setup, cycle_id, setup.member.id, 25_000)

    # Before the refund: blocked, same as the plain already-paid case below.
    blocked = await client.post(
        f"/payments/dues/{cycle_id}/intent",
        json={"rail": "card"},
        headers=setup.member.headers,
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "already_paid"

    await _correct(client, setup, entry_id, -25_000)

    # After the refund: STILL blocked (the charge path never reopens), but the 409
    # now tells the truth about why instead of repeating "already_paid".
    refused = await client.post(
        f"/payments/dues/{cycle_id}/intent",
        json={"rail": "card"},
        headers=setup.member.headers,
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"] == "refunded_contact_treasurer"


async def test_dues_intent_stays_blocked_after_a_partial_refund(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    """Netting, not "has any correction": refunded $10 of $250 is still paid, and
    must stay blocked here exactly as they stay off the overview's chase list."""
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=25_000)
    entry_id = await _pay_on_ledger(client, setup, cycle_id, setup.member.id, 25_000)
    await _correct(client, setup, entry_id, -1_000)

    response = await client.post(
        f"/payments/dues/{cycle_id}/intent",
        json={"rail": "card"},
        headers=setup.member.headers,
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "already_paid"


async def test_a_plain_paid_member_with_no_correction_is_unaffected(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    """Regression guard: netting must not change the ordinary paid-with-no-refund
    case, which is the overwhelming majority of real payments."""
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=25_000)
    await _pay_on_ledger(client, setup, cycle_id, setup.member.id, 25_000)

    response = await client.post(
        f"/payments/dues/{cycle_id}/intent",
        json={"rail": "card"},
        headers=setup.member.headers,
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "already_paid"


async def test_full_refund_agrees_on_both_the_president_overview_and_the_pay_button(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    """Both surfaces named in board c172, read through the exact endpoints they use:
    GET .../overview (the President dashboard) and POST .../intent (the pay button).

    THE HONEST RESOLUTION of c172's disagreement is this exact pair, not full
    alignment: the president sees OUTSTANDING (the member genuinely owes again),
    and the pay button still refuses them — but with a reason that agrees with the
    dashboard (refunded_contact_treasurer) instead of contradicting it
    (already_paid, which is what it said before this fix).
    """
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=25_000)
    entry_id = await _pay_on_ledger(client, setup, cycle_id, setup.member.id, 25_000)
    await _correct(client, setup, entry_id, -25_000)

    overview = await client.get(
        f"/chapters/{setup.chapter_id}/overview", headers=setup.president.headers
    )
    assert overview.status_code == 200, overview.text
    assert overview.json()["dues"]["paid_members"] == 0
    assert overview.json()["dues"]["outstanding_members"] == 2  # president + member

    intent = await client.post(
        f"/payments/dues/{cycle_id}/intent",
        json={"rail": "card"},
        headers=setup.member.headers,
    )
    assert intent.status_code == 409, intent.text
    assert intent.json()["detail"] == "refunded_contact_treasurer"


async def test_a_reservation_settled_through_stripe_stays_blocked_after_a_refund(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    """The SAME refusal for a payment that settled through THIS endpoint's real
    Stripe flow, not just a treasurer's manual ledger entry — proving the existence
    check (not the DuesPaymentIntent reservation) is what is actually doing the work.

    A real payment here leaves a reservation behind in 'succeeded' status (board c51
    / migration 0010), and nothing transitions that row when a later correction
    refunds the payment it belongs to. That reservation's own 'succeeded' branch
    would still unconditionally raise already_paid — but it never gets the chance:
    the existence check above it in the function runs first, finds the SAME
    dues_payment row this reservation is tied to, and raises refunded_contact_treasurer
    before the reservation check is ever reached. This backstops that invariant: if a
    future change ever let a dues_payment row and its reservation disagree, this is
    the test that would notice.
    """
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=25_000)

    created = await client.post(
        f"/payments/dues/{cycle_id}/intent",
        json={"rail": "card"},
        headers=setup.member.headers,
    )
    assert created.status_code == 200, created.text
    intent_id = await _reserved_intent_id(cycle_id, setup.member.id)
    assert intent_id is not None

    settled = await _post_webhook(
        client, _succeeded_event(cycle_id, setup.member.id, intent_id, "evt_settled")
    )
    assert settled.status_code == 200

    entries = await client.get(
        f"/chapters/{setup.chapter_id}/ledger", headers=setup.president.headers
    )
    paid_entry = next(
        e for e in entries.json()
        if e["entry_type"] == "dues_payment" and e["related_user_id"] == setup.member.id
    )
    await _correct(client, setup, paid_entry["id"], -25_000)

    overview = await client.get(
        f"/chapters/{setup.chapter_id}/overview", headers=setup.president.headers
    )
    assert overview.json()["dues"]["outstanding_members"] == 2  # correctly refunded

    retry = await client.post(
        f"/payments/dues/{cycle_id}/intent",
        json={"rail": "card"},
        headers=setup.member.headers,
    )
    assert retry.status_code == 409
    assert retry.json()["detail"] == "refunded_contact_treasurer"


async def test_dues_intent_rejects_non_member(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    make_user: MakeUser,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle_id = await _create_dues_cycle(client, setup)
    outsider = await make_user("Outsider")
    response = await client.post(
        f"/payments/dues/{cycle_id}/intent",
        json={"rail": "card"},
        headers=outsider.headers,
    )
    assert response.status_code == 403


async def test_customer_is_per_chapter_not_per_user(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    make_user: MakeUser,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    """Under direct charges a Customer belongs to one connected account.

    A single users.stripe_customer_id column would work for the first chapter and
    silently break the second, so this asserts a distinct Customer per chapter.
    """
    first = await make_chapter_with(role="member")
    second = await make_chapter_with(role="member")
    await _onboard(client, first)
    await _onboard(client, second)

    # Put the same person in both chapters.
    member = first.member
    invite = await client.post(
        f"/chapters/{second.chapter_id}/invites",
        json={"role": "member"},
        headers=second.president.headers,
    )
    joined = await client.post(
        "/chapters/join", json={"code": invite.json()["code"]}, headers=member.headers
    )
    assert joined.status_code == 201, joined.text

    for setup in (first, second):
        cycle_id = await _create_dues_cycle(client, setup)
        response = await client.post(
            f"/payments/dues/{cycle_id}/intent",
            json={"rail": "card"},
            headers=member.headers,
        )
        assert response.status_code == 200, response.text

    assert len(stripe_calls["customer_create"]) == 2
    accounts = {params["stripe_account"] for params in stripe_calls["customer_create"]}
    assert len(accounts) == 2


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------


def _succeeded_event(
    cycle_id: str, user_id: str, intent_id: str = "pi_test_1", event_id: str = "evt_1"
) -> dict[str, Any]:
    return {
        "id": event_id,
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": intent_id,
                "metadata": {
                    "chirp_dues_cycle_id": cycle_id,
                    "chirp_user_id": user_id,
                },
            }
        },
    }


async def _post_webhook(client: AsyncClient, event: dict[str, Any]) -> Any:
    """POST a verified event, stubbing signature verification for this call only."""
    original = stripe.Webhook.construct_event
    stripe.Webhook.construct_event = staticmethod(lambda payload, sig, secret: event)
    try:
        return await client.post(
            "/webhooks/stripe",
            content=b"{}",
            headers={"Stripe-Signature": "t=1,v1=fake"},
        )
    finally:
        stripe.Webhook.construct_event = original


async def _ledger_count(chapter_id: str) -> int:
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        result = await session.execute(
            text(
                "SELECT count(*) FROM ledger_entries "
                "WHERE chapter_id = :id AND entry_type = 'dues_payment'"
            ),
            {"id": chapter_id},
        )
        return int(result.scalar_one())


async def test_webhook_without_signature_header_is_400(
    client: AsyncClient, stripe_env: None
) -> None:
    response = await client.post("/webhooks/stripe", content=b"{}")
    assert response.status_code == 400
    assert response.json()["detail"] == "missing_stripe_signature"


async def test_webhook_with_bad_signature_is_400(
    client: AsyncClient, stripe_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verification runs before parsing — an unverified body is attacker input."""

    def boom(payload: bytes, sig: str, secret: str) -> Any:
        raise stripe.SignatureVerificationError("bad sig", sig)

    monkeypatch.setattr(stripe.Webhook, "construct_event", staticmethod(boom))
    response = await client.post(
        "/webhooks/stripe", content=b"{}", headers={"Stripe-Signature": "t=1,v1=nope"}
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_signature"


async def test_webhook_appends_dues_payment_to_the_ledger(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=25_000)

    response = await _post_webhook(client, _succeeded_event(cycle_id, setup.member.id))
    assert response.status_code == 200

    entries = await client.get(
        f"/chapters/{setup.chapter_id}/ledger", headers=setup.president.headers
    )
    dues = [e for e in entries.json() if e["entry_type"] == "dues_payment"]
    assert len(dues) == 1
    # Amount comes from the dues cycle, never from the event body.
    assert dues[0]["amount_cents"] == 25_000
    assert dues[0]["related_user_id"] == setup.member.id


async def test_webhook_replay_of_the_same_event_does_not_double_charge(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    """Stripe retries for days; the ledger has no delete path, so replays are permanent."""
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle_id = await _create_dues_cycle(client, setup)
    event = _succeeded_event(cycle_id, setup.member.id)

    first = await _post_webhook(client, event)
    second = await _post_webhook(client, event)

    assert first.status_code == 200
    assert second.status_code == 200
    assert await _ledger_count(setup.chapter_id) == 1


async def test_two_events_for_one_intent_still_produce_one_entry(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    """Event-level dedup alone is not enough — distinct event ids can share an intent."""
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle_id = await _create_dues_cycle(client, setup)

    await _post_webhook(
        client,
        _succeeded_event(cycle_id, setup.member.id, intent_id="pi_x", event_id="evt_a"),
    )
    second = await _post_webhook(
        client,
        _succeeded_event(cycle_id, setup.member.id, intent_id="pi_x", event_id="evt_b"),
    )

    assert second.status_code == 200
    assert await _ledger_count(setup.chapter_id) == 1


async def test_unhandled_event_type_is_acknowledged(
    client: AsyncClient, stripe_env: None
) -> None:
    """A non-2xx on an unhandled type makes Stripe retry that event for days."""
    response = await _post_webhook(
        client, {"id": "evt_other", "type": "invoice.paid", "data": {"object": {}}}
    )
    assert response.status_code == 200
    assert response.json() == {"received": True}


async def test_event_without_chirp_metadata_is_ignored(
    client: AsyncClient, stripe_env: None
) -> None:
    """Intents created outside Chirp must not be guessed into someone's ledger."""
    response = await _post_webhook(
        client,
        {
            "id": "evt_foreign",
            "type": "payment_intent.succeeded",
            "data": {"object": {"id": "pi_foreign", "metadata": {}}},
        },
    )
    assert response.status_code == 200


async def _reserved_intent_id(cycle_id: str, user_id: str) -> str | None:
    """The Stripe intent id stored on the live dues reservation (c51)."""
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        result = await session.execute(
            text(
                "SELECT stripe_payment_intent_id FROM dues_payment_intents "
                "WHERE dues_cycle_id = :c AND user_id = :u "
                "AND status IN ('open', 'succeeded')"
            ),
            {"c": cycle_id, "u": user_id},
        )
        return result.scalar_one_or_none()


# ---- c51: one member cannot pay one dues cycle twice ----


async def test_switching_rail_while_a_payment_is_in_flight_is_rejected(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    """The double-charge path: ACH sits in 'processing' for days, so the ledger is
    still empty and the cycle still looks unpaid. Retrying on card used to mint a
    GENUINELY different PaymentIntent (the Stripe idempotency key is per-rail), and
    both would settle into an append-only ledger with no reversal path."""
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle_id = await _create_dues_cycle(client, setup)

    ach = await client.post(
        f"/payments/dues/{cycle_id}/intent",
        json={"rail": "ach"},
        headers=setup.member.headers,
    )
    assert ach.status_code == 200, ach.text

    card = await client.post(
        f"/payments/dues/{cycle_id}/intent",
        json={"rail": "card"},
        headers=setup.member.headers,
    )

    assert card.status_code == 409, card.text
    assert card.json() == {"detail": "payment_already_in_progress"}
    # The decisive assertion: Stripe was never asked for a second intent.
    assert len(stripe_calls["payment_intent"]) == 1


# ---- c193: same-rail ACH retry past Stripe's 24h idempotency window ----
#
# The bug: a same-rail 'open' reservation that already has a stripe_payment_intent_id
# fell through the reuse comment above into an UNCONDITIONAL create_dues_payment_intent
# call, relying solely on Stripe's idempotency key to dedupe it. That key is retained
# for only 24h; ACH can sit in Stripe's 'processing' state for DAYS, and there is no
# payment_intent.processing webhook handler to move the reservation out of 'open' in
# the meantime. A retry past 24h therefore mints a genuinely NEW real PaymentIntent —
# a second bank debit — whose eventual ledger row is then silently dropped by
# uq_ledger_dues_payment_once while the webhook still returns 200.


async def test_same_rail_ach_retry_past_the_idempotency_window_does_not_mint_a_second_intent(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    """The fake hands back a brand new id/secret on EVERY create_async call — exactly
    what real Stripe does once the idempotency key has expired. So if the endpoint
    calls create() a second time at all for a same-rail retry, this test catches it:
    a second create call here IS a second real charge attempt.
    """
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle_id = await _create_dues_cycle(client, setup)

    first = await client.post(
        f"/payments/dues/{cycle_id}/intent",
        json={"rail": "ach"},
        headers=setup.member.headers,
    )
    assert first.status_code == 200, first.text
    original_secret = first.json()["payment_intent_client_secret"]

    retry = await client.post(
        f"/payments/dues/{cycle_id}/intent",
        json={"rail": "ach"},
        headers=setup.member.headers,
    )
    assert retry.status_code == 200, retry.text

    assert len(stripe_calls["payment_intent"]) == 1
    assert retry.json()["payment_intent_client_secret"] == original_secret


async def _reservation_status(cycle_id: str, user_id: str) -> str | None:
    """Raw status of the (only) reservation row for this (cycle, member), whatever
    it is — unlike _reserved_intent_id this does NOT filter to open/succeeded, so it
    can see a wrongly-canceled row too."""
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        result = await session.execute(
            text(
                "SELECT status FROM dues_payment_intents "
                "WHERE dues_cycle_id = :c AND user_id = :u"
            ),
            {"c": cycle_id, "u": user_id},
        )
        return result.scalar_one_or_none()


async def test_a_failed_stripe_call_on_retry_does_not_cancel_a_reservation_holding_a_live_intent(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    """The blanket except around the Stripe call used to set
    reservation.status = 'canceled' on ANY exception — even when the reservation
    already held a live intent from an EARLIER successful call. Canceling it releases
    uq_dues_intent_live, the only thing stopping a second, cross-rail charge for the
    same cycle. A transient failure on a RETRY must leave that reservation exactly as
    it was: open, still pointing at the live intent.

    Both create_async and retrieve_async are made to fail here so this test is
    agnostic to which one the implementation actually calls on a same-rail retry.
    """
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle_id = await _create_dues_cycle(client, setup)

    first = await client.post(
        f"/payments/dues/{cycle_id}/intent",
        json={"rail": "ach"},
        headers=setup.member.headers,
    )
    assert first.status_code == 200, first.text
    intent_id_before = await _reserved_intent_id(cycle_id, setup.member.id)
    assert intent_id_before is not None

    async def boom_create(**params: Any) -> FakeStripeObject:
        raise stripe.APIConnectionError("network blip")

    async def boom_retrieve(intent_id: str, **params: Any) -> FakeStripeObject:
        raise stripe.APIConnectionError("network blip")

    monkeypatch.setattr(stripe.PaymentIntent, "create_async", boom_create)
    monkeypatch.setattr(stripe.PaymentIntent, "retrieve_async", boom_retrieve)

    with pytest.raises(stripe.APIConnectionError):
        await client.post(
            f"/payments/dues/{cycle_id}/intent",
            json={"rail": "ach"},
            headers=setup.member.headers,
        )

    assert await _reservation_status(cycle_id, setup.member.id) == "open"
    assert await _reserved_intent_id(cycle_id, setup.member.id) == intent_id_before


async def test_a_failed_payment_releases_the_reservation_so_the_member_can_retry(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    """The guard must not strand a member whose payment genuinely failed."""
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle_id = await _create_dues_cycle(client, setup)

    first = await client.post(
        f"/payments/dues/{cycle_id}/intent",
        json={"rail": "ach"},
        headers=setup.member.headers,
    )
    assert first.status_code == 200, first.text

    # The failure event must carry the intent id Stripe actually returned, which
    # is what _resolve_reservation matches on.
    intent_id = await _reserved_intent_id(cycle_id, setup.member.id)
    assert intent_id is not None
    failed = dict(_succeeded_event(cycle_id, setup.member.id, intent_id, "evt_failed_1"))
    failed["type"] = "payment_intent.payment_failed"
    assert (await _post_webhook(client, failed)).status_code == 200

    retry = await client.post(
        f"/payments/dues/{cycle_id}/intent",
        json={"rail": "card"},
        headers=setup.member.headers,
    )

    assert retry.status_code == 200, retry.text
    assert len(stripe_calls["payment_intent"]) == 2


async def test_two_distinct_intents_for_one_cycle_cannot_both_reach_the_ledger(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    """Backstop, independent of the reservation: even if two different intents somehow
    both settle (a pre-0010 intent, a manual Stripe dashboard charge), the ledger's
    partial unique index keeps exactly one dues_payment per (cycle, member). The older
    uq_ledger_stripe_payment_intent only dedups a REPLAY of one intent id."""
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle_id = await _create_dues_cycle(client, setup)

    first = await _post_webhook(
        client, _succeeded_event(cycle_id, setup.member.id, "pi_ach", "evt_ach")
    )
    second = await _post_webhook(
        client, _succeeded_event(cycle_id, setup.member.id, "pi_card", "evt_card")
    )

    assert first.status_code == 200
    # Stripe must still get a 2xx or it retries for days.
    assert second.status_code == 200
    assert await _ledger_count(setup.chapter_id) == 1


async def test_a_second_distinct_capture_that_cannot_be_recorded_logs_a_reconciliation_error(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Finding 5 (board c193): the scenario above — a real second capture on a
    DIFFERENT intent, dropped by uq_ledger_dues_payment_once — used to be entirely
    silent. The 200 to Stripe is correct (a non-2xx would just retry an event that
    can never succeed), but the drop itself must be visible somewhere, or nobody
    ever finds out the chapter was paid twice and only credited once.
    """
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle_id = await _create_dues_cycle(client, setup)

    first = await _post_webhook(
        client, _succeeded_event(cycle_id, setup.member.id, "pi_ach", "evt_ach")
    )
    assert first.status_code == 200

    with caplog.at_level("ERROR", logger="app.routers.payments"):
        second = await _post_webhook(
            client, _succeeded_event(cycle_id, setup.member.id, "pi_card", "evt_card")
        )

    assert second.status_code == 200  # still 2xx — Stripe must not retry this forever
    assert await _ledger_count(setup.chapter_id) == 1  # unchanged: one dues_payment, ever

    reconciliation_errors = [
        r for r in caplog.records
        if r.name == "app.routers.payments" and r.levelno >= logging.ERROR
    ]
    assert reconciliation_errors, "a dropped second capture must log at ERROR/CRITICAL"
    message = reconciliation_errors[0].getMessage()
    assert "pi_card" in message  # the capture that could NOT be recorded
    assert "pi_ach" in message  # what IS already on the ledger, for reconciliation
    assert "@" not in message  # no email/PII, only internal ids


async def test_a_replayed_event_for_the_same_intent_does_not_log_a_reconciliation_error(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression guard: an exact replay of the SAME intent under a different event id
    (test_two_events_for_one_intent_still_produce_one_entry above) also loses to
    uq_ledger_dues_payment_once, but nothing was actually dropped — the ledger already
    holds this exact payment. That benign case must stay quiet.
    """
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle_id = await _create_dues_cycle(client, setup)

    await _post_webhook(
        client, _succeeded_event(cycle_id, setup.member.id, "pi_x", "evt_a")
    )
    with caplog.at_level("ERROR", logger="app.routers.payments"):
        second = await _post_webhook(
            client, _succeeded_event(cycle_id, setup.member.id, "pi_x", "evt_b")
        )

    assert second.status_code == 200
    assert await _ledger_count(setup.chapter_id) == 1
    reconciliation_errors = [
        r for r in caplog.records
        if r.name == "app.routers.payments" and r.levelno >= logging.ERROR
    ]
    assert reconciliation_errors == []


# ---------------------------------------------------------------------------
# c231: a declined-card same-rail retry must not replay a dead reservation's
# cached Stripe intent id.
#
# The bug: _resolve_reservation marks a failed reservation 'failed' but leaves its
# stripe_payment_intent_id on the row (by design — see its docstring). A same-rail
# retry never reuses that dead row (the 'live' query only sees 'open'/'succeeded');
# it inserts a genuinely NEW reservation. Pre-fix, create_dues_payment_intent's
# idempotency key was derived only from (cycle, member, rail) — identical for both
# rows — so within Stripe's 24h idempotency window the new row's create() call
# replayed Stripe's cache and got back the DEAD row's old intent id. Writing that
# id onto the new row collides with uq_dues_intent_stripe_id, which is not scoped
# by status at all, and the commit at that collision sat outside any try/except:
# an unhandled 500, repeatable for up to 24h.
# ---------------------------------------------------------------------------


async def _seed_reservation(
    chapter_id: str,
    cycle_id: str,
    user_id: str,
    rail: str,
    status: str,
    *,
    stripe_payment_intent_id: str | None = None,
    age_hours: float = 0,
) -> str:
    """Insert a dues_payment_intents row directly with a specific status/age — the
    only way to put the table in states the endpoint itself cannot produce on
    demand (a 'failed' row with an intent id still attached, an 'open' row that is
    hours old). Returns the new row's id."""
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        result = await session.execute(
            text(
                "INSERT INTO dues_payment_intents "
                "(chapter_id, dues_cycle_id, user_id, rail, status, "
                "stripe_payment_intent_id, created_at, updated_at) "
                "VALUES (:chapter_id, :cycle_id, :user_id, :rail, :status, :pi_id, "
                "now() - (:age_hours * interval '1 hour'), now()) "
                "RETURNING id"
            ),
            {
                "chapter_id": chapter_id,
                "cycle_id": cycle_id,
                "user_id": user_id,
                "rail": rail,
                "status": status,
                "pi_id": stripe_payment_intent_id,
                "age_hours": age_hours,
            },
        )
        await session.commit()
        return str(result.scalar_one())


async def _open_reservation_count(cycle_id: str, user_id: str) -> int:
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        result = await session.execute(
            text(
                "SELECT count(*) FROM dues_payment_intents "
                "WHERE dues_cycle_id = :c AND user_id = :u AND status = 'open'"
            ),
            {"c": cycle_id, "u": user_id},
        )
        return int(result.scalar_one())


def _install_caching_payment_intent_create(
    monkeypatch: pytest.MonkeyPatch,
    calls: dict[str, list[dict[str, Any]]],
    *,
    force_id: str | None = None,
    seed: dict[str, str] | None = None,
) -> None:
    """Replace create_async with one that behaves like REAL Stripe idempotency: the
    same idempotency_key always gets back the same intent id. The shared
    stripe_calls fixture's default fake mints a fresh uuid on every call and
    ignores the key entirely — realistic for most tests, but it hides exactly the
    same-key collision c231 fixes.

    seed pre-populates the key->id cache (e.g. with what the OLD, pre-c231 key
    format would have resolved to) so a test can assert the CURRENT code no longer
    computes that key. force_id makes every call return that id regardless of key,
    emulating an old-format cached key still live in Stripe's window (or two
    requests racing on one key) to exercise the IntegrityError belt directly.
    """
    key_to_id: dict[str, str] = dict(seed or {})

    async def fake_create(**params: Any) -> FakeStripeObject:
        calls["payment_intent"].append(params)
        key = params["idempotency_key"]
        if force_id is not None:
            intent_id = force_id
        else:
            intent_id = key_to_id.setdefault(key, f"pi_{uuid.uuid4().hex[:12]}")
        return FakeStripeObject(id=intent_id, client_secret=f"{intent_id}_secret")

    monkeypatch.setattr(stripe.PaymentIntent, "create_async", fake_create)


async def test_a_declined_card_retry_gets_a_genuinely_fresh_intent(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    """The primary c231 fix: seed a FAILED reservation (R1) that still carries
    pi_123 (exactly what _resolve_reservation leaves behind), then retry. The old
    (cycle, member, rail) key is pre-seeded to resolve to pi_123 in the fake — if
    the retry's create() call still computed that key, it would get pi_123 back
    and collide with R1's row. It must not: the new reservation's key includes its
    OWN row id, so the retry gets a genuinely different intent and succeeds.
    """
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle_id = await _create_dues_cycle(client, setup)
    old_format_key = f"dues:{cycle_id}:{setup.member.id}:card"
    _install_caching_payment_intent_create(
        monkeypatch, stripe_calls, seed={old_format_key: "pi_123"}
    )

    await _seed_reservation(
        setup.chapter_id, cycle_id, setup.member.id, "card", "failed",
        stripe_payment_intent_id="pi_123",
    )

    retry = await client.post(
        f"/payments/dues/{cycle_id}/intent",
        json={"rail": "card"},
        headers=setup.member.headers,
    )

    assert retry.status_code == 200, retry.text
    new_intent_id = await _reserved_intent_id(cycle_id, setup.member.id)
    assert new_intent_id is not None
    assert new_intent_id != "pi_123"
    assert stripe_calls["payment_intent"][0]["idempotency_key"] != old_format_key


async def test_a_stale_cached_key_collision_is_a_409_not_a_500(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    """The c231 belt: even with the per-reservation nonce, force Stripe to hand
    back an id (pi_123) that's already claimed by a DIFFERENT row (R1, 'failed').
    uq_dues_intent_stripe_id then loses the assignment commit — this must resolve
    as an honest 409 the client can retry, not the unhandled 500 the bug report
    describes, and it must not leave the member stuck: the new reservation this
    attempt created is canceled, releasing uq_dues_intent_live.
    """
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle_id = await _create_dues_cycle(client, setup)
    _install_caching_payment_intent_create(monkeypatch, stripe_calls, force_id="pi_123")

    await _seed_reservation(
        setup.chapter_id, cycle_id, setup.member.id, "card", "failed",
        stripe_payment_intent_id="pi_123",
    )

    retry = await client.post(
        f"/payments/dues/{cycle_id}/intent",
        json={"rail": "card"},
        headers=setup.member.headers,
    )

    assert retry.status_code == 409, retry.text
    assert retry.json()["detail"] == "payment_intent_conflict"
    # Not stuck: no 'open' row is left behind holding uq_dues_intent_live.
    assert await _open_reservation_count(cycle_id, setup.member.id) == 0


# ---------------------------------------------------------------------------
# c234: abandoned reservation lifecycle.
# ---------------------------------------------------------------------------


async def test_an_abandoned_open_reservation_past_the_ttl_lets_the_member_switch_rails(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    """An 'open' reservation nobody ever resolved, well past the 24h TTL, must not
    permanently lock a member out of paying on ANY rail. The stale row is resolved
    (best-effort Stripe cancel, then marked 'canceled') and a fresh reservation on
    the NEW rail proceeds — the double-charge guard this would otherwise trip
    (payment_already_in_progress) must not fire for a genuinely dead attempt.
    """
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle_id = await _create_dues_cycle(client, setup)

    await _seed_reservation(
        setup.chapter_id, cycle_id, setup.member.id, "ach", "open",
        stripe_payment_intent_id="pi_abandoned", age_hours=25,
    )

    response = await client.post(
        f"/payments/dues/{cycle_id}/intent",
        json={"rail": "card"},
        headers=setup.member.headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["rail"] == "card"
    # Best-effort cancel was attempted against the abandoned intent.
    assert stripe_calls["payment_intent_cancel"][0]["id"] == "pi_abandoned"
    # A fresh reservation was created (a genuine create call reached Stripe) rather
    # than reusing or erroring on the stale row.
    assert len(stripe_calls["payment_intent"]) == 1
    new_intent_id = await _reserved_intent_id(cycle_id, setup.member.id)
    assert new_intent_id is not None
    assert new_intent_id != "pi_abandoned"


async def test_an_open_reservation_within_the_ttl_still_blocks_a_rail_switch(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    """Boundary regression: an 'open' reservation that is old but still WITHIN the
    24h TTL must keep blocking a cross-rail switch exactly as before c234 — the TTL
    must not have loosened the live double-charge guard itself."""
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle_id = await _create_dues_cycle(client, setup)

    await _seed_reservation(
        setup.chapter_id, cycle_id, setup.member.id, "ach", "open",
        stripe_payment_intent_id="pi_in_flight", age_hours=1,
    )

    response = await client.post(
        f"/payments/dues/{cycle_id}/intent",
        json={"rail": "card"},
        headers=setup.member.headers,
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "payment_already_in_progress"
    assert stripe_calls["payment_intent"] == []


async def test_same_rail_retrieve_of_a_settled_intent_returns_an_honest_status(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    """c234: a same-rail retry can retrieve an intent Stripe already settled (or is
    settling) while the webhook that would have moved the reservation out of 'open'
    has not arrived yet. The endpoint must not hand back what looks like a fresh
    checkout — payment_intent_status must say so instead of the default
    'awaiting_payment' a genuinely open intent reports.
    """
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle_id = await _create_dues_cycle(client, setup)

    first = await client.post(
        f"/payments/dues/{cycle_id}/intent",
        json={"rail": "card"},
        headers=setup.member.headers,
    )
    assert first.status_code == 200, first.text
    assert first.json()["payment_intent_status"] == "awaiting_payment"

    async def fake_retrieve_succeeded(intent_id: str, **params: Any) -> FakeStripeObject:
        calls_seen["id"] = intent_id
        return FakeStripeObject(
            id=intent_id, client_secret="stale_looking_secret", status="succeeded"
        )

    calls_seen: dict[str, str] = {}
    monkeypatch.setattr(stripe.PaymentIntent, "retrieve_async", fake_retrieve_succeeded)

    retry = await client.post(
        f"/payments/dues/{cycle_id}/intent",
        json={"rail": "card"},
        headers=setup.member.headers,
    )

    assert retry.status_code == 200, retry.text
    assert retry.json()["payment_intent_status"] == "succeeded"
    # No second create call — this is still the retrieve path, not a new charge.
    assert len(stripe_calls["payment_intent"]) == 1
