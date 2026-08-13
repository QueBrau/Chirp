"""Kyber (PQXDH) prekey directory: registration, one-time consumption, last-resort fallback.

Mirrors test_prekey_consumption.py's structure/patterns for the EC one-time prekeys —
see FINDINGS.md #1 in spikes/libsignal-node for the contract gap this closes.
"""
from __future__ import annotations

import asyncio
import uuid

from httpx import AsyncClient

from tests.conftest import ApiUser, MakeUser, b64


def _kyber_field(tag: str, key_id: int = 1) -> dict[str, object]:
    return {
        "key_id": key_id,
        "public_key_b64": b64(f"kyber-pub-{tag}-{uuid.uuid4().hex}".encode()),
        "signature_b64": b64(f"kyber-sig-{tag}-{uuid.uuid4().hex}".encode()),
    }


async def _register_device_with_kyber(
    client: AsyncClient,
    owner: ApiUser,
    *,
    kyber_one_time_count: int = 2,
    with_last_resort: bool = True,
    registration_id: int = 9001,
) -> dict[str, object]:
    """Register a device with EC prekeys plus a last-resort + one-time Kyber batch."""
    body: dict[str, object] = {
        "device_label": "pytest-kyber-device",
        "registration_id": registration_id,
        "identity_key_b64": b64(b"identity-" + uuid.uuid4().bytes),
        "signed_prekey": {
            "key_id": 1,
            "public_key_b64": b64(b"spk-" + uuid.uuid4().bytes),
            "signature_b64": b64(b"sig-" + uuid.uuid4().bytes),
        },
        "one_time_prekeys": [
            {"key_id": key_id, "public_key_b64": b64(f"otk-{key_id}".encode())}
            for key_id in range(1, 3)
        ],
        "kyber_one_time": [
            _kyber_field("otk", key_id) for key_id in range(1, kyber_one_time_count + 1)
        ],
    }
    if with_last_resort:
        body["kyber_last_resort"] = _kyber_field("last-resort")
    response = await client.post("/devices", json=body, headers=owner.headers)
    assert response.status_code == 201, response.text
    return response.json()


async def test_registration_with_kyber_fields_succeeds(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """POST /devices accepts kyber_last_resort + kyber_one_time and reports counts."""
    owner = await make_user("Kyber Owner")
    device = await _register_device_with_kyber(client, owner, kyber_one_time_count=3)

    count = await client.get(
        f"/devices/{device['id']}/prekeys/count", headers=owner.headers
    )
    assert count.status_code == 200, count.text
    body = count.json()
    assert body["kyber_one_time_prekeys_available"] == 3
    assert body["kyber_last_resort_registered"] is True
    # EC one-time count untouched by the kyber fields.
    assert body["one_time_prekeys_available"] == 2


async def test_registration_without_kyber_fields_still_works(
    client: AsyncClient, make_user: MakeUser, register_device
) -> None:
    """Backward compatibility: a registration with no kyber fields at all still succeeds,
    and the bundle it produces has kyber_prekey: null (nullable path)."""
    owner = await make_user("No Kyber Owner")
    fetcher = await make_user("No Kyber Fetcher")
    await register_device(owner, one_time_prekey_count=1)

    bundle = await client.get(
        f"/users/{owner.id}/prekey-bundle", headers=fetcher.headers
    )
    assert bundle.status_code == 200, bundle.text
    devices = bundle.json()["devices"]
    assert len(devices) >= 1
    assert devices[0]["kyber_prekey"] is None
    assert devices[0]["signed_prekey"] is not None


async def test_bundle_returns_one_time_kyber_then_falls_back_to_last_resort(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """Bundle hands out the one-time Kyber pool first; once exhausted it falls back to the
    last-resort Kyber prekey — and the last-resort key is NEVER marked consumed."""
    owner = await make_user("Kyber Pool Owner")
    fetcher = await make_user("Kyber Pool Fetcher")
    device = await _register_device_with_kyber(client, owner, kyber_one_time_count=2)

    seen_key_ids: list[int] = []
    last_resort_public_b64: str | None = None
    for _ in range(2):
        bundle = await client.get(
            f"/users/{owner.id}/prekey-bundle", headers=fetcher.headers
        )
        assert bundle.status_code == 200, bundle.text
        kyber = bundle.json()["devices"][0]["kyber_prekey"]
        assert kyber is not None
        assert kyber["is_last_resort"] is False, "one-time pool should serve first"
        seen_key_ids.append(kyber["key_id"])

    assert sorted(seen_key_ids) == [1, 2], "expected both distinct one-time kyber keys"

    # Pool of 2 is now exhausted -> next fetches fall back to the last-resort key.
    for _ in range(3):
        bundle = await client.get(
            f"/users/{owner.id}/prekey-bundle", headers=fetcher.headers
        )
        assert bundle.status_code == 200, bundle.text
        kyber = bundle.json()["devices"][0]["kyber_prekey"]
        assert kyber is not None
        assert kyber["is_last_resort"] is True
        if last_resort_public_b64 is None:
            last_resort_public_b64 = kyber["public_key_b64"]
        else:
            # Same last-resort row handed out every time -> never consumed.
            assert kyber["public_key_b64"] == last_resort_public_b64

    count = await client.get(
        f"/devices/{device['id']}/prekeys/count", headers=owner.headers
    )
    assert count.status_code == 200, count.text
    assert count.json()["kyber_one_time_prekeys_available"] == 0
    assert count.json()["kyber_last_resort_registered"] is True


async def test_device_with_no_kyber_prekeys_returns_null_kyber_bundle(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """A device registered with NO kyber_last_resort and NO kyber_one_time gets a bundle
    with kyber_prekey: null forever (never errors, degrades gracefully)."""
    owner = await make_user("Bare Owner")
    fetcher = await make_user("Bare Fetcher")
    await _register_device_with_kyber(
        client, owner, kyber_one_time_count=0, with_last_resort=False
    )

    bundle = await client.get(
        f"/users/{owner.id}/prekey-bundle", headers=fetcher.headers
    )
    assert bundle.status_code == 200, bundle.text
    assert bundle.json()["devices"][0]["kyber_prekey"] is None


async def test_concurrent_bundle_fetches_dont_double_consume_kyber(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """Two concurrent prekey-bundle fetches get DIFFERENT one-time Kyber prekeys."""
    owner = await make_user("Kyber Race Owner")
    fetcher = await make_user("Kyber Race Fetcher")
    await _register_device_with_kyber(client, owner, kyber_one_time_count=2)

    first, second = await asyncio.gather(
        client.get(f"/users/{owner.id}/prekey-bundle", headers=fetcher.headers),
        client.get(f"/users/{owner.id}/prekey-bundle", headers=fetcher.headers),
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text

    kyber_a = first.json()["devices"][0]["kyber_prekey"]
    kyber_b = second.json()["devices"][0]["kyber_prekey"]
    assert kyber_a is not None and kyber_b is not None
    assert kyber_a["is_last_resort"] is False and kyber_b["is_last_resort"] is False
    assert kyber_a["key_id"] != kyber_b["key_id"], (
        "concurrent fetches handed out the same one-time Kyber prekey"
    )


async def test_replenish_rotates_last_resort_and_tops_up_one_time_kyber(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """POST /devices/{id}/prekeys can rotate kyber_last_resort and add kyber_one_time keys."""
    owner = await make_user("Kyber Replenish Owner")
    device = await _register_device_with_kyber(
        client, owner, kyber_one_time_count=0, with_last_resort=True
    )

    replenish = await client.post(
        f"/devices/{device['id']}/prekeys",
        json={
            "kyber_last_resort": _kyber_field("rotated"),
            "kyber_one_time": [_kyber_field("topup", key_id=5)],
        },
        headers=owner.headers,
    )
    assert replenish.status_code == 200, replenish.text
    body = replenish.json()
    assert body["kyber_one_time_prekeys_available"] == 1
    assert body["kyber_last_resort_registered"] is True
