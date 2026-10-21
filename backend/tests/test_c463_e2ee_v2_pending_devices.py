"""GET /v2/devices/pending: an account's OWN devices that are waiting for approval (c463).

An approved phone has no other way to learn that a new phone signed in, because the directory
never lists a pending device. These tests pin what the read promises: it is the caller's own
pending devices and nobody else's, it carries exactly what an approval needs, approved,
revoked and legacy devices are never in it, and acting on a pending device takes it off the list.
"""
from __future__ import annotations

import inspect
import uuid
from typing import Any

import pytest
from httpx import AsyncClient

from app import models
from app.config import get_settings
from app.db import get_session_factory
from app.routers import keys_v2
from app.services.account_deletion_hold import activate_deletion_hold
from tests.conftest import MakeUser, RegisterDevice, b64
from tests.e2ee_v2_helpers import Keyring, approve, make_mate, register


@pytest.fixture(autouse=True)
def _clear_settings() -> Any:
    yield
    get_settings.cache_clear()


def _set_admission(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setenv("ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED", str(enabled).lower())
    monkeypatch.setenv("LEGAL_ENFORCEMENT_ENABLED", "false")
    get_settings.cache_clear()


async def _hold(user_id: str) -> None:
    """Put the account under an active deletion hold, the way test_c436_key_write_admission does."""
    async with get_session_factory()() as session:
        request = models.AccountDataRequest(
            user_id=uuid.UUID(user_id), kind="deletion", open_key="key"
        )
        session.add(request)
        await session.flush()
        await session.commit()
        request_id = request.id
    async with get_session_factory()() as session:
        await activate_deletion_hold(session, user_id=uuid.UUID(user_id), request_id=request_id)
        await session.commit()


async def _pending(client: AsyncClient, user: Any, *, expect: int = 200) -> list[dict[str, Any]]:
    response = await client.get("/v2/devices/pending", headers=user.headers)
    assert response.status_code == expect, response.text
    return response.json()["devices"] if expect == 200 else response.json()


async def test_the_owner_sees_own_pending_device_with_what_an_approval_needs(
    client: AsyncClient, make_user: MakeUser
) -> None:
    owner = await make_user("Owner")
    root = Keyring(owner, 1)
    await register(client, root)
    phone = Keyring(owner, 2)
    await register(client, phone)

    (entry,) = await _pending(client, owner)
    assert entry["id"] == phone.id
    assert entry["generation"] == 2
    assert entry["suite"] == "vodozemac-olm-v1"
    assert entry["identity_curve25519_b64"] == b64(phone.curve)
    assert entry["identity_ed25519_b64"] == b64(phone.ed)
    assert entry["binding_signature_b64"] == b64(phone.sign_binding())
    assert entry["device_label"] == "pytest"
    assert entry["created_at"]
    # Only the public binding: nothing about approval state, keys or the approver.
    assert set(entry) == {
        "id", "device_label", "suite", "generation", "identity_curve25519_b64",
        "identity_ed25519_b64", "binding_signature_b64", "created_at",
    }


async def test_it_lists_every_pending_device_oldest_first(
    client: AsyncClient, make_user: MakeUser
) -> None:
    owner = await make_user("Owner")
    root = Keyring(owner, 1)
    await register(client, root)
    first = Keyring(owner, 2)
    await register(client, first)
    second = Keyring(owner, 3)
    await register(client, second)
    assert [d["id"] for d in await _pending(client, owner)] == [first.id, second.id]


async def test_another_user_never_sees_it(client: AsyncClient, make_user: MakeUser) -> None:
    owner = await make_user("Owner")
    mate = await make_mate(client, make_user, owner)
    stranger = await make_user("Stranger")
    root = Keyring(owner, 1)
    await register(client, root)
    phone = Keyring(owner, 2)
    await register(client, phone)

    assert [d["id"] for d in await _pending(client, owner)] == [phone.id]
    # A current conversation mate (who may read the owner's DIRECTORY) and a stranger both get
    # their own, empty, list: there is no parameter that could point this at someone else.
    assert await _pending(client, mate) == []
    assert await _pending(client, stranger) == []
    # The route takes no user id: a query parameter is ignored, not honoured.
    response = await client.get(
        "/v2/devices/pending", params={"user_id": owner.id}, headers=stranger.headers
    )
    assert response.status_code == 200 and response.json() == {"devices": []}
    # Someone with their own pending device sees only that one.
    other_root = Keyring(stranger, 1)
    await register(client, other_root)
    other_phone = Keyring(stranger, 2)
    await register(client, other_phone)
    assert [d["id"] for d in await _pending(client, stranger)] == [other_phone.id]
    assert [d["id"] for d in await _pending(client, owner)] == [phone.id]


async def test_approved_revoked_and_legacy_devices_are_excluded(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    owner = await make_user("Owner")
    await register_device(owner, one_time_prekey_count=1)  # a legacy v1 row, suite NULL
    root = Keyring(owner, 1)
    await register(client, root)
    # The self-approved root and an approved second device are not pending.
    approved = Keyring(owner, 2)
    await register(client, approved)
    revoked = Keyring(owner, 3)
    await register(client, revoked)
    waiting = Keyring(owner, 4)
    await register(client, waiting)
    await approve(client, approver=root, new=approved)
    response = await client.delete(f"/v2/devices/{revoked.id}", headers=owner.headers)
    assert response.status_code == 200, response.text

    assert [d["id"] for d in await _pending(client, owner)] == [waiting.id]


async def test_approving_removes_it_from_the_list(client: AsyncClient, make_user: MakeUser) -> None:
    owner = await make_user("Owner")
    root = Keyring(owner, 1)
    await register(client, root)
    phone = Keyring(owner, 2)
    await register(client, phone)
    assert [d["id"] for d in await _pending(client, owner)] == [phone.id]

    await approve(client, approver=root, new=phone)
    assert await _pending(client, owner) == []


async def test_revoking_removes_it_from_the_list_and_a_new_root_has_none(
    client: AsyncClient, make_user: MakeUser
) -> None:
    owner = await make_user("Owner")
    root = Keyring(owner, 1)
    await register(client, root)
    phone = Keyring(owner, 2)
    await register(client, phone)
    assert [d["id"] for d in await _pending(client, owner)] == [phone.id]

    # "Not me": the owner revokes the pending device.
    response = await client.delete(f"/v2/devices/{phone.id}", headers=owner.headers)
    assert response.status_code == 200, response.text
    assert await _pending(client, owner) == []

    # A root device (the first or a reset one) is never pending.
    await client.delete(f"/v2/devices/{root.id}", headers=owner.headers)
    fresh = Keyring(owner, 3)
    body = await register(client, fresh)
    assert body["approved"] is True
    assert await _pending(client, owner) == []


async def test_it_needs_a_signed_in_account_and_is_not_a_write(
    client: AsyncClient, make_user: MakeUser
) -> None:
    response = await client.get("/v2/devices/pending")
    assert response.status_code == 401
    # Reads use the plain identity dependency, like the other v2 reads (c436): only the
    # approve and revoke WRITES go through the deletion-hold admission.
    dependency = inspect.signature(keys_v2.list_pending_devices_v2).parameters["user"].default.dependency
    assert dependency.__name__ == "get_current_user"
    assert dependency.__name__ != "get_current_user_for_write"
    for endpoint in (keys_v2.approve_device_v2, keys_v2.revoke_device_v2):
        guarded = inspect.signature(endpoint).parameters["user"].default.dependency
        assert guarded.__name__ == "get_current_user_for_write"


async def test_it_stays_readable_while_an_account_deletion_runs_but_the_writes_refuse(
    client: AsyncClient, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = await make_user("Owner")
    root = Keyring(owner, 1)
    await register(client, root)
    phone = Keyring(owner, 2)
    await register(client, phone)

    _set_admission(monkeypatch, True)
    await _hold(owner.id)
    # The read is the same as the directory read: it works under a hold.
    assert [d["id"] for d in await _pending(client, owner)] == [phone.id]
    # Approving and revoking are WRITES: the 423 the app already handles.
    approval = await client.post(
        f"/v2/devices/{phone.id}/approve",
        json={"approver_device_id": root.id, "approval_signature_b64": b64(root.approval_for(phone))},
        headers=owner.headers,
    )
    assert approval.status_code == 423, approval.text
    revoke = await client.delete(f"/v2/devices/{phone.id}", headers=owner.headers)
    assert revoke.status_code == 423, revoke.text
    # Nothing changed: still pending.
    assert [d["id"] for d in await _pending(client, owner)] == [phone.id]


async def test_it_is_rate_limited_per_account(client: AsyncClient, make_user: MakeUser) -> None:
    owner = await make_user("Owner")
    other = await make_user("Other")
    max_calls, _window = keys_v2.PENDING_LIST_LIMIT
    for _ in range(max_calls):
        response = await client.get("/v2/devices/pending", headers=owner.headers)
        assert response.status_code == 200, response.text
    limited = await client.get("/v2/devices/pending", headers=owner.headers)
    assert limited.status_code == 429, limited.text
    # The limit is per account: someone else is not slowed down by this caller.
    assert (await client.get("/v2/devices/pending", headers=other.headers)).status_code == 200

