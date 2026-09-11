"""Mounted key-directory byte, stored-row, concurrency and write-budget boundaries."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update

from app import models
from app.db import get_session_factory
from app.routers import keys
from app.schemas.e2ee import (
    EXACT_KYBER_PUBLIC_KEY_BYTES,
    EXACT_PUBLIC_KEY_BYTES,
    EXACT_SIGNATURE_BYTES,
    MAX_KYBER_PUBLIC_KEY_BYTES,
    MAX_PUBLIC_KEY_BYTES,
    MAX_SIGNATURE_BYTES,
    DeviceCreate,
)
from tests.conftest import MakeUser, RegisterDevice, _padded, b64


def _key(key_id: int = 1, *, signed: bool = False, kyber: bool = False) -> dict[str, object]:
    """Build one prekey field at its EXACT accepted byte length (board c347).

    kyber=True is for kyber_last_resort/kyber_one_time entries (1568-byte public key);
    the default is the EC (Curve25519) shape shared by identity/signed/one-time keys
    (32 bytes), which also applies to signed_prekey even though it takes signed=True.
    """
    public_bytes = EXACT_KYBER_PUBLIC_KEY_BYTES if kyber else EXACT_PUBLIC_KEY_BYTES
    result: dict[str, object] = {
        "key_id": key_id,
        "public_key_b64": b64(_padded(f"public-{key_id}".encode(), public_bytes)),
    }
    if signed:
        result["signature_b64"] = b64(
            _padded(f"signature-{key_id}".encode(), EXACT_SIGNATURE_BYTES)
        )
    return result


def _device_body() -> dict[str, object]:
    return {
        "device_label": "resource-bound-test",
        "registration_id": 1,
        "identity_key_b64": b64(_padded(b"identity", EXACT_PUBLIC_KEY_BYTES)),
        "signed_prekey": _key(signed=True),
        "one_time_prekeys": [_key()],
        "kyber_last_resort": _key(signed=True, kyber=True),
        "kyber_one_time": [_key(signed=True, kyber=True)],
    }


def _set_path(body: dict[str, object], path: tuple[str | int, ...], value: object) -> None:
    current: Any = body
    for segment in path[:-1]:
        current = current[segment]
    current[path[-1]] = value


_BYTE_FIELDS = [
    (("identity_key_b64",), MAX_PUBLIC_KEY_BYTES),
    (("signed_prekey", "public_key_b64"), MAX_PUBLIC_KEY_BYTES),
    (("signed_prekey", "signature_b64"), MAX_SIGNATURE_BYTES),
    (("one_time_prekeys", 0, "public_key_b64"), MAX_PUBLIC_KEY_BYTES),
    (("kyber_last_resort", "public_key_b64"), MAX_KYBER_PUBLIC_KEY_BYTES),
    (("kyber_last_resort", "signature_b64"), MAX_SIGNATURE_BYTES),
    (("kyber_one_time", 0, "public_key_b64"), MAX_KYBER_PUBLIC_KEY_BYTES),
    (("kyber_one_time", 0, "signature_b64"), MAX_SIGNATURE_BYTES),
]


def _hold_quota_check_open(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Keep the last-slot transaction open long enough for a competing request to arrive."""
    original = getattr(keys, name)

    async def delayed_check(*args: Any, **kwargs: Any) -> None:
        await original(*args, **kwargs)
        # With the row lock, the competitor waits and then observes the commit.
        # Without it, both requests can pass their read before either inserts.
        await asyncio.sleep(0.05)

    monkeypatch.setattr(keys, name, delayed_check)


@pytest.mark.parametrize("path,maximum", _BYTE_FIELDS)
async def test_oversize_decoded_field_rejected_without_registration(
    client: AsyncClient, make_user: MakeUser, path: tuple[str | int, ...], maximum: int
) -> None:
    owner = await make_user("Oversize Key")
    body = _device_body()
    # For these bounds, maximum + 1 has the SAME base64 length as maximum.
    # An encoded-length-only guard would pass this payload.
    assert len(b64(b"x" * maximum)) == len(b64(b"x" * (maximum + 1)))
    _set_path(body, path, b64(b"x" * (maximum + 1)))
    response = await client.post("/devices", json=body, headers=owner.headers)
    assert response.status_code == 422, response.text
    async with get_session_factory()() as session:
        assert await session.scalar(
            select(func.count()).select_from(models.Device).where(
                models.Device.user_id == uuid.UUID(owner.id)
            )
        ) == 0


@pytest.mark.parametrize("path,maximum", _BYTE_FIELDS[1:])
async def test_replenishment_rejects_oversize_before_storing_any_keys(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice,
    path: tuple[str | int, ...], maximum: int,
) -> None:
    owner = await make_user("Oversize Replenish")
    device = await register_device(owner, one_time_prekey_count=0)
    body = _device_body()
    body = {name: value for name, value in body.items() if name in {
        "signed_prekey", "one_time_prekeys", "kyber_last_resort", "kyber_one_time"
    }}
    _set_path(body, path, b64(b"x" * (maximum + 1)))
    response = await client.post(
        f"/devices/{device['id']}/prekeys", json=body, headers=owner.headers
    )
    assert response.status_code == 422, response.text
    counts = await client.get(f"/devices/{device['id']}/prekeys/count", headers=owner.headers)
    assert counts.json()["one_time_prekeys_available"] == 0
    assert counts.json()["kyber_one_time_prekeys_available"] == 0
    assert counts.json()["kyber_last_resort_registered"] is False


def test_exact_byte_lengths_are_the_accepted_contract() -> None:
    """The old ceiling-only contract (1..max_bytes accepted) is no longer true (c347):
    a payload built entirely at the CEILING length must now be refused, while one built
    entirely at the EXACT length must be accepted."""
    exact_body = _device_body()
    assert DeviceCreate.model_validate(exact_body).identity_key_b64 == (
        exact_body["identity_key_b64"]
    )
    assert DeviceCreate.model_validate(_device_body()).registration_id == 1

    ceiling_body = _device_body()
    for path, maximum in _BYTE_FIELDS:
        _set_path(ceiling_body, path, b64(b"x" * maximum))
    with pytest.raises(ValueError):
        DeviceCreate.model_validate(ceiling_body)


_EXACT_BYTE_FIELDS = [
    (("identity_key_b64",), EXACT_PUBLIC_KEY_BYTES),
    (("signed_prekey", "public_key_b64"), EXACT_PUBLIC_KEY_BYTES),
    (("signed_prekey", "signature_b64"), EXACT_SIGNATURE_BYTES),
    (("one_time_prekeys", 0, "public_key_b64"), EXACT_PUBLIC_KEY_BYTES),
    (("kyber_last_resort", "public_key_b64"), EXACT_KYBER_PUBLIC_KEY_BYTES),
    (("kyber_last_resort", "signature_b64"), EXACT_SIGNATURE_BYTES),
    (("kyber_one_time", 0, "public_key_b64"), EXACT_KYBER_PUBLIC_KEY_BYTES),
    (("kyber_one_time", 0, "signature_b64"), EXACT_SIGNATURE_BYTES),
]


def _assert_boundary_rejected(response: Any, exact_size: int) -> None:
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail[0]["type"] == "value_error", detail
    assert str(exact_size) in detail[0]["msg"], detail


@pytest.mark.parametrize("path,exact_size", _EXACT_BYTE_FIELDS)
async def test_exact_length_boundary_at_registration(
    client: AsyncClient, make_user: MakeUser, path: tuple[str | int, ...], exact_size: int,
) -> None:
    """One byte under or over the exact size is refused; exactly on it is accepted."""
    owner = await make_user(f"Boundary Registration {path}")

    under = _device_body()
    _set_path(under, path, b64(b"x" * (exact_size - 1)))
    response = await client.post("/devices", json=under, headers=owner.headers)
    _assert_boundary_rejected(response, exact_size)

    over = _device_body()
    _set_path(over, path, b64(b"x" * (exact_size + 1)))
    response = await client.post("/devices", json=over, headers=owner.headers)
    _assert_boundary_rejected(response, exact_size)

    async with get_session_factory()() as session:
        assert await session.scalar(
            select(func.count()).select_from(models.Device).where(
                models.Device.user_id == uuid.UUID(owner.id)
            )
        ) == 0

    exact = _device_body()
    exact_value = b64(b"x" * exact_size)
    _set_path(exact, path, exact_value)
    response = await client.post("/devices", json=exact, headers=owner.headers)
    assert response.status_code == 201, response.text
    if path == ("identity_key_b64",):
        assert response.json()["identity_key_b64"] == exact_value


@pytest.mark.parametrize("path,exact_size", _EXACT_BYTE_FIELDS[1:])
async def test_exact_length_boundary_at_replenishment(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice,
    path: tuple[str | int, ...], exact_size: int,
) -> None:
    owner = await make_user(f"Boundary Replenish {path}")
    device = await register_device(owner, one_time_prekey_count=0)

    def _prekey_body(value: str) -> dict[str, object]:
        body = _device_body()
        body = {name: value for name, value in body.items() if name in {
            "signed_prekey", "one_time_prekeys", "kyber_last_resort", "kyber_one_time"
        }}
        _set_path(body, path, value)
        return body

    for delta in (-1, 1):
        response = await client.post(
            f"/devices/{device['id']}/prekeys",
            json=_prekey_body(b64(b"x" * (exact_size + delta))),
            headers=owner.headers,
        )
        _assert_boundary_rejected(response, exact_size)
        counts = await client.get(
            f"/devices/{device['id']}/prekeys/count", headers=owner.headers
        )
        assert counts.json()["one_time_prekeys_available"] == 0
        assert counts.json()["kyber_one_time_prekeys_available"] == 0
        assert counts.json()["kyber_last_resort_registered"] is False

    response = await client.post(
        f"/devices/{device['id']}/prekeys",
        json=_prekey_body(b64(b"x" * exact_size)),
        headers=owner.headers,
    )
    assert response.status_code == 200, response.text
    counts = await client.get(f"/devices/{device['id']}/prekeys/count", headers=owner.headers)
    if path[0] == "one_time_prekeys":
        assert counts.json()["one_time_prekeys_available"] == 1
    elif path[0] == "kyber_one_time":
        assert counts.json()["kyber_one_time_prekeys_available"] == 1
    elif path[0] == "kyber_last_resort":
        assert counts.json()["kyber_last_resort_registered"] is True


@pytest.mark.parametrize("value", ["", "====", "not base64!", "é", "AA==\n"])
def test_invalid_base64_fails_schema_validation(value: str) -> None:
    body = _device_body()
    body["identity_key_b64"] = value
    with pytest.raises(ValueError):
        DeviceCreate.model_validate(body)


@pytest.mark.parametrize("path,value", [
    (("device_label",), "x" * 101),
    (("registration_id",), 2**31),
    (("registration_id",), -1),
    (("signed_prekey", "key_id"), 2**31),
    (("one_time_prekeys", 0, "key_id"), -1),
    (("kyber_last_resort", "key_id"), 2**31),
    (("kyber_one_time", 0, "key_id"), 2**31),
])
def test_label_and_database_integer_bounds(path: tuple[str | int, ...], value: object) -> None:
    body = _device_body()
    _set_path(body, path, value)
    with pytest.raises(ValueError):
        DeviceCreate.model_validate(body)


async def test_concurrent_registration_cannot_exceed_active_device_quota(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = await make_user("Concurrent Device Owner")
    for _ in range(keys.MAX_ACTIVE_DEVICES - 1):
        await register_device(owner)
    _hold_quota_check_open(monkeypatch, "_check_device_quota")
    responses = await asyncio.gather(*(
        client.post("/devices", json=_device_body(), headers=owner.headers) for _ in range(3)
    ))
    assert sorted(response.status_code for response in responses) == [201, 409, 409]
    assert all(response.json()["detail"] == "active_device_limit_reached"
               for response in responses if response.status_code == 409)
    async with get_session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(models.Device).where(
            models.Device.user_id == uuid.UUID(owner.id)
        )) == keys.MAX_ACTIVE_DEVICES


async def test_revoked_devices_still_count_toward_account_storage_quota(
    client: AsyncClient, make_user: MakeUser,
) -> None:
    owner = await make_user("Revoked Device History")
    async with get_session_factory()() as session:
        session.add_all(models.Device(
            user_id=uuid.UUID(owner.id), registration_id=index, identity_key=b"old",
            revoked_at=datetime.now(timezone.utc),
        ) for index in range(keys.MAX_RETAINED_DEVICES))
        await session.commit()
    response = await client.post("/devices", json=_device_body(), headers=owner.headers)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "device_storage_limit_reached"


async def test_revoked_device_can_be_replaced_below_retained_ceiling(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice,
) -> None:
    owner = await make_user("Replacement Owner")
    devices = [await register_device(owner) for _ in range(keys.MAX_ACTIVE_DEVICES)]
    async with get_session_factory()() as session:
        await session.execute(update(models.Device).where(
            models.Device.id == uuid.UUID(devices[0]["id"])
        ).values(revoked_at=datetime.now(timezone.utc)))
        await session.commit()
    response = await client.post("/devices", json=_device_body(), headers=owner.headers)
    assert response.status_code == 201, response.text


async def test_repeated_full_batches_stop_at_retained_ceiling(
    client: AsyncClient, make_user: MakeUser,
) -> None:
    owner = await make_user("Repeated Upload Owner")
    body = _device_body()
    body["one_time_prekeys"] = [_key(index) for index in range(200)]
    body["kyber_one_time"] = [_key(index, signed=True, kyber=True) for index in range(200)]
    response = await client.post("/devices", json=body, headers=owner.headers)
    assert response.status_code == 201, response.text
    device_id = response.json()["id"]
    response = await client.post(f"/devices/{device_id}/prekeys", headers=owner.headers, json={
        "one_time_prekeys": [_key(index) for index in range(200, 400)],
        "kyber_one_time": [_key(index, signed=True, kyber=True) for index in range(200, 400)],
    })
    assert response.status_code == 200, response.text
    assert response.json()["one_time_prekeys_available"] == 400
    assert response.json()["kyber_one_time_prekeys_available"] == 400
    response = await client.post(f"/devices/{device_id}/prekeys", headers=owner.headers, json={
        "one_time_prekeys": [_key(400)],
    })
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "one_time_prekey_storage_limit_reached"


@pytest.mark.parametrize("pool", ["ec", "kyber", "signed", "last_resort"])
async def test_concurrent_replenishment_cannot_exceed_retained_quota(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice, pool: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = await make_user(f"Concurrent {pool} Owner")
    device = await register_device(owner, one_time_prekey_count=0)
    device_id = uuid.UUID(device["id"])
    if pool == "ec":
        model = models.OneTimePrekey
        limit = keys.MAX_RETAINED_ONE_TIME_PREKEYS
        payload = {"one_time_prekeys": [_key(999)]}
        extra = {}
        existing = 0
    elif pool == "kyber":
        model = models.KyberPrekey
        limit = keys.MAX_RETAINED_ONE_TIME_PREKEYS
        payload = {"kyber_one_time": [_key(999, signed=True, kyber=True)]}
        extra = {"signature": b"sig", "is_last_resort": False}
        existing = 0
    elif pool == "signed":
        model = models.SignedPrekey
        limit = keys.MAX_RETAINED_SIGNED_PREKEYS
        payload = {"signed_prekey": _key(999, signed=True)}
        extra = {"signature": b"sig"}
        existing = 1
    else:
        model = models.KyberPrekey
        limit = keys.MAX_RETAINED_LAST_RESORT_PREKEYS
        payload = {"kyber_last_resort": _key(999, signed=True, kyber=True)}
        extra = {"signature": b"sig", "is_last_resort": True}
        existing = 0
    async with get_session_factory()() as session:
        session.add_all(model(device_id=device_id, key_id=index + 10,
                              public_key=b"old", **extra)
                        for index in range(limit - existing - 1))
        await session.commit()
    _hold_quota_check_open(monkeypatch, "_check_prekey_quota")
    responses = await asyncio.gather(*(
        client.post(f"/devices/{device_id}/prekeys", json=payload, headers=owner.headers)
        for _ in range(2)
    ))
    assert sorted(response.status_code for response in responses) == [200, 409]
    async with get_session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(model).where(
            model.device_id == device_id
        )) == limit


@pytest.mark.parametrize("kyber", [False, True])
async def test_consumed_rows_remain_in_storage_quota_and_are_not_deleted(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice, kyber: bool,
) -> None:
    owner = await make_user("Retained Consumed Keys")
    device = await register_device(owner, one_time_prekey_count=0)
    device_id = uuid.UUID(device["id"])
    model = models.KyberPrekey if kyber else models.OneTimePrekey
    extra = {"signature": b"sig", "is_last_resort": False} if kyber else {}
    async with get_session_factory()() as session:
        session.add_all(model(
            device_id=device_id, key_id=index, public_key=b"old",
            consumed_at=datetime.now(timezone.utc), **extra,
        ) for index in range(keys.MAX_RETAINED_ONE_TIME_PREKEYS))
        await session.commit()
    payload = {"kyber_one_time": [_key(999, signed=True, kyber=True)]} if kyber else {
        "one_time_prekeys": [_key(999)]
    }
    # A rejected mixed upload must not rotate its otherwise-valid signed prekey.
    payload["signed_prekey"] = _key(999, signed=True)
    response = await client.post(
        f"/devices/{device_id}/prekeys", json=payload, headers=owner.headers
    )
    assert response.status_code == 409, response.text
    async with get_session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(model).where(
            model.device_id == device_id, model.consumed_at.is_not(None)
        )) == keys.MAX_RETAINED_ONE_TIME_PREKEYS
        assert await session.scalar(select(func.count()).select_from(models.SignedPrekey).where(
            models.SignedPrekey.device_id == device_id
        )) == 1


async def test_bundle_serves_every_device_at_maximum_supported_count(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice,
) -> None:
    owner = await make_user("Maximum Devices")
    fetcher = await make_user("Bundle Fetcher")
    devices = [await register_device(owner, one_time_prekey_count=1)
               for _ in range(keys.MAX_ACTIVE_DEVICES)]
    response = await client.get(f"/users/{owner.id}/prekey-bundle", headers=fetcher.headers)
    assert response.status_code == 200, response.text
    assert {item["device_id"] for item in response.json()["devices"]} == {
        item["id"] for item in devices
    }
    assert all(item["one_time_prekey"] for item in response.json()["devices"])


async def test_legacy_over_quota_bundle_refuses_before_consuming_any_key(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice,
) -> None:
    owner = await make_user("Legacy Oversize Account")
    fetcher = await make_user("Legacy Bundle Fetcher")
    device = await register_device(owner, one_time_prekey_count=1)
    async with get_session_factory()() as session:
        session.add_all(models.Device(user_id=uuid.UUID(owner.id), registration_id=index,
                                      identity_key=b"legacy")
                        for index in range(keys.MAX_ACTIVE_DEVICES))
        await session.commit()
    response = await client.get(f"/users/{owner.id}/prekey-bundle", headers=fetcher.headers)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "active_device_limit_reached"
    count = await client.get(f"/devices/{device['id']}/prekeys/count", headers=owner.headers)
    assert count.json()["one_time_prekeys_available"] == 1


async def test_revoked_device_refuses_replenishment_and_is_not_in_bundle(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice,
) -> None:
    owner = await make_user("Revoked Owner")
    other = await make_user("Other Owner")
    device = await register_device(owner)
    async with get_session_factory()() as session:
        await session.execute(update(models.Device).where(
            models.Device.id == uuid.UUID(device["id"])
        ).values(revoked_at=datetime.now(timezone.utc)))
        await session.commit()
    response = await client.post(f"/devices/{device['id']}/prekeys",
                                 json={"one_time_prekeys": [_key()]}, headers=owner.headers)
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "device_revoked"
    foreign = await client.post(f"/devices/{device['id']}/prekeys",
                                json={}, headers=other.headers)
    assert foreign.status_code == 403, foreign.text
    assert foreign.json()["detail"] == "not_device_owner"
    response = await client.get(f"/users/{owner.id}/prekey-bundle", headers=other.headers)
    assert response.json()["devices"] == []


async def test_registration_budget_is_scoped_per_account(
    client: AsyncClient, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(keys, "_DEVICE_REGISTER_RATE_MAX_CALLS", 1)
    owner = await make_user("Registration Budget")
    other = await make_user("Separate Registration Budget")
    first = await client.post("/devices", json=_device_body(), headers=owner.headers)
    assert first.status_code == 201, first.text
    limited = await client.post("/devices", json=_device_body(), headers=owner.headers)
    assert limited.status_code == 429, limited.text
    assert limited.json()["detail"] == "device_registration_rate_limited"
    separate = await client.post("/devices", json=_device_body(), headers=other.headers)
    assert separate.status_code == 201, separate.text


@pytest.mark.parametrize("scope", ["account", "device"])
async def test_replenishment_budgets_prevent_extra_writes(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice,
    monkeypatch: pytest.MonkeyPatch, scope: str,
) -> None:
    monkeypatch.setattr(keys, f"_PREKEY_WRITE_{scope.upper()}_MAX_CALLS", 1)
    owner = await make_user("Replenishment Budget")
    first = await register_device(owner, one_time_prekey_count=0)
    second = await register_device(owner, one_time_prekey_count=0)
    response = await client.post(f"/devices/{first['id']}/prekeys",
                                 json={"one_time_prekeys": [_key()]}, headers=owner.headers)
    assert response.status_code == 200, response.text
    target = second if scope == "account" else first
    limited = await client.post(f"/devices/{target['id']}/prekeys",
                                json={"one_time_prekeys": [_key(2)]}, headers=owner.headers)
    assert limited.status_code == 429, limited.text
    assert limited.json()["detail"] == "prekey_write_rate_limited"
    count = await client.get(f"/devices/{target['id']}/prekeys/count", headers=owner.headers)
    assert count.json()["one_time_prekeys_available"] == (0 if scope == "account" else 1)
    if scope == "device":
        response = await client.post(f"/devices/{second['id']}/prekeys",
                                     json={"one_time_prekeys": [_key()]}, headers=owner.headers)
        assert response.status_code == 200, response.text
