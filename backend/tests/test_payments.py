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
