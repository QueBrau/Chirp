"""Prekey-bundle fetch is rate-limited per (caller, target) pair (SECURITY-REVIEW finding 9)."""
from __future__ import annotations

from httpx import AsyncClient

from app.routers.keys import (
    _PREKEY_BUNDLE_RATE_LIMIT_MAX_CALLS as MAX_CALLS,
)
from app.services import rate_limit
from tests.conftest import MakeUser, RegisterDevice


async def _fresh_limiter():
    """Clear the module-level sliding-window state before each test (in-process, shared)."""
    rate_limit._reset_all()


async def test_nplus1th_fetch_from_same_caller_and_target_is_429(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    """After MAX_CALLS allowed fetches, the next one from the same (caller, target) is 429."""
    await _fresh_limiter()
    owner = await make_user("Key Owner")
    fetcher = await make_user("Session Starter")
    # Register more OTKs than fetches so 200s aren't masked by unrelated behavior.
    await register_device(owner, one_time_prekey_count=MAX_CALLS + 5)

    for i in range(MAX_CALLS):
        response = await client.get(
            f"/users/{owner.id}/prekey-bundle", headers=fetcher.headers
        )
        assert response.status_code == 200, f"fetch {i + 1} should be allowed: {response.text}"

    limited = await client.get(
        f"/users/{owner.id}/prekey-bundle", headers=fetcher.headers
    )
    assert limited.status_code == 429, limited.text
    assert limited.json()["detail"] == "prekey_bundle_rate_limited"


async def test_different_caller_same_target_is_unaffected(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    """A caller who has not exhausted their own budget is unaffected by another caller's usage."""
    await _fresh_limiter()
    owner = await make_user("Key Owner")
    drainer = await make_user("Drainer")
    other_fetcher = await make_user("Other Fetcher")
    await register_device(owner, one_time_prekey_count=2 * MAX_CALLS + 5)

    for _ in range(MAX_CALLS):
        response = await client.get(
            f"/users/{owner.id}/prekey-bundle", headers=drainer.headers
        )
        assert response.status_code == 200, response.text

    exhausted = await client.get(
        f"/users/{owner.id}/prekey-bundle", headers=drainer.headers
    )
    assert exhausted.status_code == 429, exhausted.text

    # A different caller targeting the same owner still gets served.
    unaffected = await client.get(
        f"/users/{owner.id}/prekey-bundle", headers=other_fetcher.headers
    )
    assert unaffected.status_code == 200, unaffected.text


async def test_different_target_from_same_caller_is_unaffected(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    """A caller who has exhausted their budget against one target can still reach another."""
    await _fresh_limiter()
    owner_a = await make_user("Key Owner A")
    owner_b = await make_user("Key Owner B")
    fetcher = await make_user("Session Starter")
    await register_device(owner_a, one_time_prekey_count=MAX_CALLS + 5)
    await register_device(owner_b, one_time_prekey_count=2)

    for _ in range(MAX_CALLS):
        response = await client.get(
            f"/users/{owner_a.id}/prekey-bundle", headers=fetcher.headers
        )
        assert response.status_code == 200, response.text

    exhausted = await client.get(
        f"/users/{owner_a.id}/prekey-bundle", headers=fetcher.headers
    )
    assert exhausted.status_code == 429, exhausted.text

    # Same caller, a DIFFERENT target — separate rate-limit bucket, still allowed.
    unaffected = await client.get(
        f"/users/{owner_b.id}/prekey-bundle", headers=fetcher.headers
    )
    assert unaffected.status_code == 200, unaffected.text
