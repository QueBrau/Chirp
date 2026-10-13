"""Account-hold admission for the enrolled device/key write endpoints."""

from __future__ import annotations

import asyncio
import inspect
import os
import uuid

import pytest
from sqlalchemy import select, text

from app import models
from app.config import get_settings
from app.db import get_session_factory
from app.routers import keys, keys_v2
from app.services.account_deletion_hold import activate_deletion_hold
from tests.conftest import MakeUser, b64
from tests.e2ee_v2_helpers import Keyring


def _set_admission(monkeypatch: pytest.MonkeyPatch, enabled: bool, *, legal: bool = False) -> None:
    monkeypatch.setenv("ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED", str(enabled).lower())
    monkeypatch.setenv("LEGAL_ENFORCEMENT_ENABLED", str(legal).lower())
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clear_settings() -> None:
    yield
    get_settings.cache_clear()


async def _hold(user_id: str) -> None:
    async with get_session_factory()() as session:
        request = models.AccountDataRequest(
            user_id=uuid.UUID(user_id), kind="deletion", open_key="key"
        )
        session.add(request)
        await session.flush()
        await session.commit()
        request_id = request.id
    async with get_session_factory()() as session:
        await activate_deletion_hold(
            session, user_id=uuid.UUID(user_id), request_id=request_id
        )
        await session.commit()


def _v1_body() -> dict[str, object]:
    def key(tag: str) -> str:
        return b64((tag.encode() + os.urandom(32))[:32])

    return {
        "device_label": "admission-test",
        "registration_id": 901,
        "identity_key_b64": key("identity"),
        "signed_prekey": {"key_id": 1, "public_key_b64": key("signed"), "signature_b64": b64(os.urandom(64))},
        "one_time_prekeys": [{"key_id": 1, "public_key_b64": key("otk")}],
    }


def test_only_claimed_key_writes_use_guarded_identity_dependency() -> None:
    v1 = (keys.register_device, keys.replenish_prekeys)
    v2 = (
        keys_v2.register_device_v2,
        keys_v2.approve_device_v2,
        keys_v2.upload_keys_v2,
        keys_v2.revoke_device_v2,
    )
    for endpoint in (*v1, *v2):
        dependency = inspect.signature(endpoint).parameters["user"].default.dependency
        assert dependency.__name__ == "get_current_user_for_write"
    for endpoint in (keys.fetch_prekey_bundle, keys_v2.claim_keys, keys_v2.get_user_directory):
        dependency = inspect.signature(endpoint).parameters["user"].default.dependency
        assert dependency.__name__ != "get_current_user_for_write"


@pytest.mark.asyncio
async def test_active_hold_blocks_v1_and_v2_registration_before_device_rows(
    client, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission(monkeypatch, True)
    owner = await make_user("held key owner")
    await _hold(owner.id)

    v1 = await client.post("/devices", json=_v1_body(), headers=owner.headers)
    assert v1.status_code == 423, v1.text

    ring = Keyring(owner, 1)
    v2 = await client.post("/v2/devices", json=ring.registration_body(), headers=owner.headers)
    assert v2.status_code == 423, v2.text

    async with get_session_factory()() as session:
        assert await session.scalar(
            select(models.Device.id).where(models.Device.user_id == uuid.UUID(owner.id))
        ) is None


@pytest.mark.asyncio
async def test_unrelated_hold_does_not_block_registration_and_flag_off_skips_hold_sql(
    client, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission(monkeypatch, True)
    held = await make_user("held account")
    other = await make_user("unheld account")
    await _hold(held.id)
    ring = Keyring(other, 1)
    response = await client.post("/v2/devices", json=ring.registration_body(), headers=other.headers)
    assert response.status_code == 201, response.text

    _set_admission(monkeypatch, False)
    statements: list[str] = []
    from app.db import get_engine
    from sqlalchemy import event

    def record(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    event.listen(get_engine().sync_engine, "before_cursor_execute", record)
    try:
        response = await client.post("/devices", json=_v1_body(), headers=other.headers)
    finally:
        event.remove(get_engine().sync_engine, "before_cursor_execute", record)
    assert response.status_code == 201, response.text
    assert not any("account_deletion_holds" in statement for statement in statements)


@pytest.mark.asyncio
async def test_foreign_device_writes_remain_rejected_after_guard_admission(
    client, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission(monkeypatch, True)
    owner = await make_user("device owner")
    stranger = await make_user("device stranger")
    ring = Keyring(owner, 1)
    created = await client.post("/v2/devices", json=ring.registration_body(), headers=owner.headers)
    assert created.status_code == 201, created.text
    ring.id = created.json()["id"]

    response = await client.delete(f"/v2/devices/{ring.id}", headers=stranger.headers)
    assert response.status_code == 403, response.text
    async with get_session_factory()() as session:
        device = await session.get(models.Device, uuid.UUID(ring.id))
        assert device is not None and device.revoked_at is None


@pytest.mark.asyncio
async def test_all_enrolled_key_writes_succeed_when_account_is_not_held(
    client, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission(monkeypatch, True)
    user = await make_user("key write success")

    legacy = await client.post("/devices", json=_v1_body(), headers=user.headers)
    assert legacy.status_code == 201, legacy.text
    legacy_id = legacy.json()["id"]
    replenish = await client.post(
        f"/devices/{legacy_id}/prekeys",
        json={
            "one_time_prekeys": [{"key_id": 77, "public_key_b64": b64(os.urandom(32))}],
        },
        headers=user.headers,
    )
    assert replenish.status_code == 200, replenish.text

    root = Keyring(user, 1)
    registered = await client.post("/v2/devices", json=root.registration_body(), headers=user.headers)
    assert registered.status_code == 201, registered.text
    root.id = registered.json()["id"]
    pending = Keyring(user, 2)
    registered = await client.post("/v2/devices", json=pending.registration_body(), headers=user.headers)
    assert registered.status_code == 201, registered.text
    pending.id = registered.json()["id"]

    approval = await client.post(
        f"/v2/devices/{pending.id}/approve",
        json={
            "approver_device_id": root.id,
            "approval_signature_b64": b64(root.approval_for(pending)),
        },
        headers=user.headers,
    )
    assert approval.status_code == 200, approval.text
    upload = await client.post(
        f"/v2/devices/{root.id}/keys",
        json={"one_time_keys": [root.signed_key()]},
        headers=user.headers,
    )
    assert upload.status_code == 200, upload.text
    revoke = await client.delete(f"/v2/devices/{pending.id}", headers=user.headers)
    assert revoke.status_code == 200, revoke.text


@pytest.mark.asyncio
async def test_active_hold_blocks_each_enrolled_device_key_write_before_target_sql(
    client, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission(monkeypatch, True)
    user = await make_user("held key writes")

    legacy = await client.post("/devices", json=_v1_body(), headers=user.headers)
    assert legacy.status_code == 201, legacy.text
    legacy_id = legacy.json()["id"]
    root = Keyring(user, 1)
    root_response = await client.post(
        "/v2/devices", json=root.registration_body(), headers=user.headers
    )
    assert root_response.status_code == 201, root_response.text
    root.id = root_response.json()["id"]
    pending = Keyring(user, 2)
    pending_response = await client.post(
        "/v2/devices", json=pending.registration_body(), headers=user.headers
    )
    assert pending_response.status_code == 201, pending_response.text
    pending.id = pending_response.json()["id"]
    pending2 = Keyring(user, 3)
    pending2_response = await client.post(
        "/v2/devices", json=pending2.registration_body(), headers=user.headers
    )
    assert pending2_response.status_code == 201, pending2_response.text
    pending2.id = pending2_response.json()["id"]
    approved = await client.post(
        f"/v2/devices/{pending.id}/approve",
        json={
            "approver_device_id": root.id,
            "approval_signature_b64": b64(root.approval_for(pending)),
        },
        headers=user.headers,
    )
    assert approved.status_code == 200, approved.text
    await _hold(user.id)

    requests = [
        client.post(
            f"/devices/{legacy_id}/prekeys",
            json={"one_time_prekeys": [{"key_id": 91, "public_key_b64": b64(os.urandom(32))}]},
            headers=user.headers,
        ),
        client.post(
            f"/v2/devices/{pending2.id}/approve",
            json={
                "approver_device_id": root.id,
                "approval_signature_b64": b64(root.approval_for(pending2)),
            },
            headers=user.headers,
        ),
        client.post(
            f"/v2/devices/{root.id}/keys",
            json={"one_time_keys": [root.signed_key()]},
            headers=user.headers,
        ),
        client.delete(f"/v2/devices/{pending2.id}", headers=user.headers),
    ]
    from app.db import get_engine
    from sqlalchemy import event

    statements: list[str] = []

    def record(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    event.listen(get_engine().sync_engine, "before_cursor_execute", record)
    try:
        responses = await asyncio.gather(*requests)
    finally:
        event.remove(get_engine().sync_engine, "before_cursor_execute", record)

    assert [response.status_code for response in responses] == [423, 423, 423, 423]
    target_sql = (
        " from devices", "insert into devices", "update devices", "delete from devices",
        " from signed_prekeys", "insert into signed_prekeys", "delete from signed_prekeys",
        " from one_time_prekeys", "insert into one_time_prekeys", "delete from one_time_prekeys",
        " from kyber_prekeys", "insert into kyber_prekeys", "delete from kyber_prekeys",
    )
    assert not any(any(fragment in statement for fragment in target_sql) for statement in statements)

    async with get_session_factory()() as session:
        assert await session.scalar(
            select(models.Device.id).where(models.Device.id == uuid.UUID(pending2.id))
        ) is not None
        assert await session.scalar(
            select(models.Device.revoked_at).where(models.Device.id == uuid.UUID(pending2.id))
        ) is None


@pytest.mark.asyncio
async def test_foreign_upload_and_approval_cannot_cross_account_boundaries(
    client, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission(monkeypatch, True)
    owner = await make_user("owner boundary")
    stranger = await make_user("stranger boundary")
    owner_root = Keyring(owner, 1)
    response = await client.post(
        "/v2/devices", json=owner_root.registration_body(), headers=owner.headers
    )
    assert response.status_code == 201, response.text
    owner_root.id = response.json()["id"]
    owner_pending = Keyring(owner, 2)
    response = await client.post(
        "/v2/devices", json=owner_pending.registration_body(), headers=owner.headers
    )
    assert response.status_code == 201, response.text
    owner_pending.id = response.json()["id"]
    stranger_root = Keyring(stranger, 1)
    response = await client.post(
        "/v2/devices", json=stranger_root.registration_body(), headers=stranger.headers
    )
    assert response.status_code == 201, response.text
    stranger_root.id = response.json()["id"]

    upload = await client.post(
        f"/v2/devices/{owner_root.id}/keys",
        json={"one_time_keys": [stranger_root.signed_key()]},
        headers=stranger.headers,
    )
    assert upload.status_code == 403, upload.text
    approval = await client.post(
        f"/v2/devices/{owner_pending.id}/approve",
        json={
            "approver_device_id": stranger_root.id,
            "approval_signature_b64": b64(stranger_root.approval_for(owner_pending)),
        },
        headers=stranger.headers,
    )
    assert approval.status_code == 403, approval.text


@pytest.mark.asyncio
async def test_suspension_and_legal_gate_still_apply_after_write_enrollment(
    client, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission(monkeypatch, True, legal=True)
    user = await make_user("key gate user")
    response = await client.post("/v2/devices", json=Keyring(user, 1).registration_body(), headers=user.headers)
    assert response.status_code == 428, response.text
    async with get_session_factory()() as session:
        await session.execute(
            text("UPDATE users SET suspended_at = now() WHERE id = :id"),
            {"id": uuid.UUID(user.id)},
        )
        await session.commit()
    _set_admission(monkeypatch, True, legal=False)
    response = await client.post("/v2/devices", json=Keyring(user, 1).registration_body(), headers=user.headers)
    assert response.status_code == 403, response.text
