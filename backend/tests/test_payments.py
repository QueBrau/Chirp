"""Stripe dues flow: fee/rail locking, onboarding gating, and webhook replay safety.

The Stripe SDK is monkeypatched at the `stripe.*` boundary rather than at our own
service functions, so these tests still cover the params we hand Stripe (rail,
application fee, connected account) — the part that moves real money.
"""
from __future__ import annotations

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
    }

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
        return FakeStripeObject(id=f"pi_{uuid.uuid4().hex[:12]}", client_secret="pi_secret")

    monkeypatch.setattr(stripe.Account, "create_async", fake_account_create)
    monkeypatch.setattr(stripe.Account, "retrieve_async", fake_account_retrieve)
    monkeypatch.setattr(stripe.AccountLink, "create_async", fake_account_link_create)
    monkeypatch.setattr(stripe.Customer, "create_async", fake_customer_create)
    monkeypatch.setattr(
        stripe.CustomerSession, "create_async", fake_customer_session_create
    )
    monkeypatch.setattr(stripe.PaymentIntent, "create_async", fake_payment_intent_create)
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
    """A client retry must resolve to the same intent, not create a second one."""
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle_id = await _create_dues_cycle(client, setup)

    for _ in range(2):
        await client.post(
            f"/payments/dues/{cycle_id}/intent",
            json={"rail": "card"},
            headers=setup.member.headers,
        )

    keys = [params["idempotency_key"] for params in stripe_calls["payment_intent"]]
    assert keys[0] == keys[1]
    assert cycle_id in keys[0]


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


# ---- c172: the guard must NET corrections, matching the President overview (c171) ----
#
# Before the fix, this endpoint's already_paid check read the mere EXISTENCE of a
# dues_payment row and never looked at corrections, while chapter_overview NETTED them
# per member. A member refunded in full showed as still owing on the President
# dashboard while their own pay button told them they had already paid and refused a
# second attempt. dues_contributions_subquery (app/core/dues_status.py) is now the one
# definition both read.


async def test_dues_intent_is_allowed_again_after_a_full_refund(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    """The exact board c172 scenario: paid, refunded in full, must be payable again."""
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=25_000)
    entry_id = await _pay_on_ledger(client, setup, cycle_id, setup.member.id, 25_000)

    # Before the refund: blocked, same as the plain already-paid case above.
    blocked = await client.post(
        f"/payments/dues/{cycle_id}/intent",
        json={"rail": "card"},
        headers=setup.member.headers,
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "already_paid"

    await _correct(client, setup, entry_id, -25_000)

    allowed = await client.post(
        f"/payments/dues/{cycle_id}/intent",
        json={"rail": "card"},
        headers=setup.member.headers,
    )
    assert allowed.status_code == 200, allowed.text


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
    A refund must never leave one saying chase them and the other already paid."""
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
    assert intent.status_code == 200, intent.text


async def test_a_reservation_settled_through_stripe_stays_blocked_after_a_refund(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    """DOCUMENTS A KNOWN REMAINING EDGE, deliberately — this is not the divergence
    c172 closes, but the boundary of what it safely can.

    Unlike a treasurer's manual ledger entry (the two tests above), a payment made
    through THIS endpoint's real flow leaves a DuesPaymentIntent reservation behind
    in 'succeeded' status (board c51 / migration 0010), and nothing transitions that
    row when a later correction refunds the payment it belongs to. So a member whose
    ORIGINAL payment went through Stripe stays blocked here even though the President
    overview now correctly shows them outstanding — the two surfaces still disagree
    for this one path.

    This is not a quiet gap: it is asserted here on purpose, so it fails loudly if
    someone "fixes" the reservation check without also fixing what it guards against.
    Lifting it safely needs more than netting — uq_ledger_dues_payment_once still
    allows at most one dues_payment row per (cycle, member) ever, so a second Stripe
    payment could settle with no ledger row to show for it. See the RESIDUAL EDGE
    comments in payments.py and chapters.py: closing this needs the cycle/member's
    dues status modeled explicitly (c83-shaped), not a same-day patch.
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
    assert retry.json()["detail"] == "already_paid"


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
