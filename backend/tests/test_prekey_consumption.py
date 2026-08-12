"""One-time prekey handout: concurrent fetches never double-consume; exhaustion degrades."""
from __future__ import annotations

import asyncio

from httpx import AsyncClient

from tests.conftest import MakeUser, RegisterDevice


async def test_concurrent_bundle_fetches_consume_different_otks(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    """Two concurrent prekey-bundle fetches get DIFFERENT one-time prekeys (SKIP LOCKED)."""
    owner = await make_user("Key Owner")
    fetcher = await make_user("Session Starter")
    device = await register_device(owner, one_time_prekey_count=2)

    first, second = await asyncio.gather(
        client.get(f"/users/{owner.id}/prekey-bundle", headers=fetcher.headers),
        client.get(f"/users/{owner.id}/prekey-bundle", headers=fetcher.headers),
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text

    first_devices = first.json()["devices"]
    second_devices = second.json()["devices"]
    assert len(first_devices) == 1 and len(second_devices) == 1

    otk_a = first_devices[0]["one_time_prekey"]
    otk_b = second_devices[0]["one_time_prekey"]
    assert otk_a is not None and otk_b is not None, "both fetches should get an OTK"
    assert otk_a["key_id"] != otk_b["key_id"], "concurrent fetches handed out the same OTK"

    # Pool of 2 is now fully consumed — the owner's count endpoint agrees.
    count = await client.get(
        f"/devices/{device['id']}/prekeys/count", headers=owner.headers
    )
    assert count.status_code == 200, count.text
    assert count.json()["one_time_prekeys_available"] == 0


async def test_exhausted_pool_still_returns_bundle_with_null_otk(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    """After exhaustion the bundle is still served; one_time_prekey is null (X3DH degrades)."""
    owner = await make_user("Key Owner")
    fetcher = await make_user("Session Starter")
    await register_device(owner, one_time_prekey_count=1)

    consumed = await client.get(
        f"/users/{owner.id}/prekey-bundle", headers=fetcher.headers
    )
    assert consumed.status_code == 200, consumed.text
    assert consumed.json()["devices"][0]["one_time_prekey"] is not None

    exhausted = await client.get(
        f"/users/{owner.id}/prekey-bundle", headers=fetcher.headers
    )
    assert exhausted.status_code == 200, exhausted.text
    bundle = exhausted.json()["devices"][0]
    assert bundle["one_time_prekey"] is None
    assert bundle["signed_prekey"] is not None
    assert bundle["identity_key_b64"]
