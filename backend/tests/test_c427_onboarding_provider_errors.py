"""c427: a Stripe failure while opening Connect onboarding must be a clean 503.

WHAT THIS CARD WAS. Jose reported, from a real phone, that "Set up payments"
showed an error and never opened Stripe. The exact message was never captured,
and the reason it could not be captured is the defect these tests pin: the
treasurer screen's catch falls back to its own title for anything it cannot map,
and this route produced exactly that — an exception that escaped the route, which
the deployed service turns into an opaque 500 whose body is not the app's usual
{"detail": ...} shape at all. No layer of the stack could say which layer failed.

`create_connect_onboarding_link` was the only provider I/O in payments.py with no
handler on it. The dues path has caught `stripe.StripeError` since c366, and
app/main.py registers no catch-all (its single handler is for SQLAlchemy's
TimeoutError), so a timeout, a rate limit, a key rotated mid-flight or a Connect
capability problem all escaped uncaught.

WHAT IS AND IS NOT CLAIMED HERE. These tests do not claim to reproduce Jose's
specific tap — the exact error was never supplied and the card says not to assume
the failure layer. They prove something narrower and checkable: every Stripe-side
failure in this route now produces a distinct, machine-readable 503 instead of an
unhandled exception, and the three failure modes no longer collapse into one
indistinguishable outcome. That is what makes the next report diagnosable.

PRECONDITIONS ARE ASSERTED, NOT ASSUMED. Each test states the condition it needs
(the chapter has no connected account yet, or already has one; the secret key is
absent) and asserts it is actually in effect BEFORE asserting the response. A case
that passes only because the fixture incidentally produced the right state proves
nothing about the code and stops being true the day the fixture changes.
"""
from __future__ import annotations

from typing import Any

import pytest
import stripe
from httpx import AsyncClient

from app.config import get_settings
from tests.conftest import ChapterSetup, MakeChapterWith, MakeUser

# Imported rather than redefined, the convention test_c355_provider_waits.py
# already follows: these fixtures patch the `stripe.*` SDK boundary, so a test
# using them still covers the params we hand Stripe.
from tests.test_payments import (  # noqa: F401
    _onboard,
    _stripe_account_id,
    stripe_calls,
    stripe_env,
)

ONBOARDING = "/payments/connect/onboarding-link"
"""The detail the route now returns for a Stripe-side failure."""
PROVIDER_DETAIL = "onboarding_unavailable"


def _raise_stripe_error(**_params: Any) -> None:
    """A Stripe SDK call that fails the way a real outage or timeout does."""
    raise stripe.APIConnectionError("simulated Stripe failure")


async def _post(client: AsyncClient, setup: ChapterSetup) -> Any:
    return await client.post(
        ONBOARDING,
        json={"chapter_id": setup.chapter_id},
        headers=setup.president.headers,
    )


async def test_create_account_stripe_error_returns_clean_503(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    """The first-onboarding branch: Account.create raising must not escape."""
    setup = await make_chapter_with(role="member")

    # PRECONDITION: no connected account yet, so the route takes the branch that
    # calls Account.create at all. Asserted, because if the chapter were already
    # onboarded this test would exercise the OTHER call and still pass.
    assert await _stripe_account_id(setup.chapter_id) is None

    monkeypatch.setattr(stripe.Account, "create_async", _raise_stripe_error)

    # The assertion that matters is that this returns AT ALL. Before this card the
    # exception propagated out of the request and there was no response object.
    response = await _post(client, setup)

    assert response.status_code == 503, response.text
    assert response.json()["detail"] == PROVIDER_DETAIL
    # A failed account creation must not leave a half-written association behind.
    assert await _stripe_account_id(setup.chapter_id) is None


async def test_account_link_stripe_error_returns_clean_503(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    """The resume branch: AccountLink.create raising must not escape either.

    This is the branch a treasurer tapping "Set up payments" a second time takes,
    which makes it the more likely one in the reported scenario — the chapter
    keeps its Express account once created.
    """
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)

    # PRECONDITION: the chapter now HAS an account, so the route skips
    # Account.create and goes straight to the link call under test.
    account_id = await _stripe_account_id(setup.chapter_id)
    assert account_id is not None

    monkeypatch.setattr(stripe.AccountLink, "create_async", _raise_stripe_error)

    response = await _post(client, setup)

    assert response.status_code == 503, response.text
    assert response.json()["detail"] == PROVIDER_DETAIL
    # The account association survives a failed link: the next attempt must
    # resume the same Express account rather than orphan it and make a new one.
    assert await _stripe_account_id(setup.chapter_id) == account_id


async def test_missing_key_keeps_its_own_detail_and_is_not_swallowed(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    """The risk the fix itself introduces, tested directly.

    `stripe_service._secret_key()` signals a missing key by raising HTTPException,
    and it is called INSIDE the two functions the new handler wraps. An `except`
    clause written one notch too wide — `Exception`, or a bare `except` — would
    catch that HTTPException and relabel a permanent misconfiguration as a
    transient provider outage, telling an operator to retry something that will
    never succeed. This test fails if that ever happens.
    """
    setup = await make_chapter_with(role="member")
    assert await _stripe_account_id(setup.chapter_id) is None

    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    get_settings.cache_clear()
    # PRECONDITION: the key really is gone from the settings the app will read.
    assert not get_settings().stripe_secret_key

    response = await _post(client, setup)

    assert response.status_code == 503, response.text
    assert response.json()["detail"] == "stripe_not_configured"
    assert response.json()["detail"] != PROVIDER_DETAIL


async def test_authorization_denials_survive_a_failing_provider(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    """Role gating still runs first, and Stripe is never reached for the unauthorized.

    The card requires existing authorization denials to be preserved. A handler
    placed too early, or one that turned any failure into a generic 503, could
    answer 503 to someone who should have been told 403 — which would hide a
    permission problem behind a retry prompt.
    """
    setup = await make_chapter_with(role="member")
    monkeypatch.setattr(stripe.Account, "create_async", _raise_stripe_error)
    monkeypatch.setattr(stripe.AccountLink, "create_async", _raise_stripe_error)

    as_member = await client.post(
        ONBOARDING,
        json={"chapter_id": setup.chapter_id},
        headers=setup.member.headers,
    )
    assert as_member.status_code == 403, as_member.text
    assert as_member.json()["detail"] == "insufficient_role"

    outsider = await make_user()
    as_outsider = await client.post(
        ONBOARDING,
        json={"chapter_id": setup.chapter_id},
        headers=outsider.headers,
    )
    assert as_outsider.status_code == 403, as_outsider.text
    assert as_outsider.json()["detail"] == "not_a_member"

    # Neither denial should have reached the provider at all.
    assert stripe_calls["account_create"] == []
    assert stripe_calls["account_link"] == []


async def test_the_three_failure_modes_stay_distinguishable(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    """The diagnosability property c427 actually needs, asserted as a set.

    All three of these answer 503, so status alone identifies nothing. What makes
    a phone report actionable is that the DETAILS differ: unset base URL, unset
    key, and a provider failure must never collapse into one string. Computed as a
    set over three real requests rather than compared pairwise by hand, so adding
    a fourth 503 that reuses an existing detail fails here.
    """
    details: list[str] = []

    # 1. No public base URL. Checked before either Stripe call, by design.
    setup_a = await make_chapter_with(role="member")
    monkeypatch.delenv("APP_PUBLIC_BASE_URL", raising=False)
    get_settings.cache_clear()
    assert not get_settings().app_public_base_url
    first = await _post(client, setup_a)
    assert first.status_code == 503, first.text
    details.append(first.json()["detail"])

    # 2. Base URL present, secret key absent.
    monkeypatch.setenv("APP_PUBLIC_BASE_URL", "https://app.chirp.test")
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    get_settings.cache_clear()
    assert get_settings().app_public_base_url
    assert not get_settings().stripe_secret_key
    setup_b = await make_chapter_with(role="member")
    second = await _post(client, setup_b)
    assert second.status_code == 503, second.text
    details.append(second.json()["detail"])

    # 3. Fully configured, provider raises.
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    get_settings.cache_clear()
    assert get_settings().stripe_secret_key
    setup_c = await make_chapter_with(role="member")
    monkeypatch.setattr(stripe.Account, "create_async", _raise_stripe_error)
    third = await _post(client, setup_c)
    assert third.status_code == 503, third.text
    details.append(third.json()["detail"])

    assert len(set(details)) == 3, f"503 details collapsed: {details}"
    assert PROVIDER_DETAIL in details


async def test_onboarding_still_succeeds_when_stripe_is_healthy(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    stripe_env: None,
    stripe_calls: dict[str, list[dict[str, Any]]],
) -> None:
    """The handler must not change the success path it wraps."""
    setup = await make_chapter_with(role="member")
    response = await _post(client, setup)

    assert response.status_code == 200, response.text
    assert response.json()["url"].startswith("https://")
    assert len(stripe_calls["account_link"]) == 1
    assert await _stripe_account_id(setup.chapter_id) is not None
