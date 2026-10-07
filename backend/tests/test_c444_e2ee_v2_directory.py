"""E2EE v2 device directory: registration, verification, approval, top-up, claim, revoke (c444).

The server VERIFIES Ed25519 signatures, so every request here is built by
tests/e2ee_v2_helpers.Keyring from real keys. A test that proves a field is bound by a
signature mutates the BODY after signing; a test that proves the wrong signer is refused
signs the right bytes with the wrong key.
"""
from __future__ import annotations

import asyncio
import base64
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import AsyncClient
from sqlalchemy import func, select

from app import models
from app.core import e2ee_v2
from app.db import get_session_factory
from tests.conftest import MakeUser, RegisterDevice, b64
from tests.e2ee_v2_helpers import (
    Keyring,
    approve,
    directory,
    register,
    register_approved_pair,
)


def _flip(value_b64: str) -> str:
    raw = bytearray(base64.b64decode(value_b64))
    raw[0] ^= 1
    return b64(bytes(raw))


async def _counts(client: AsyncClient, ring: Keyring) -> dict[str, Any]:
    response = await client.get(f"/v2/devices/{ring.id}/keys/count", headers=ring.user.headers)
    assert response.status_code == 200, response.text
    return response.json()


async def _claim(client: AsyncClient, caller: Any, *rings: Keyring, expect: int = 200) -> Any:
    response = await client.post(
        "/v2/keys/claim",
        json={"device_ids": [ring.id for ring in rings]},
        headers=caller.headers,
    )
    assert response.status_code == expect, response.text
    return response.json()


def _assert_claim_verifies(ring: Keyring, claimed: dict[str, Any]) -> None:
    """The server hands back material the owning device's identity key actually signed."""
    message = e2ee_v2.one_time_key_message(
        kind=claimed["kind"],
        key_id=claimed["key_id"],
        public_key=base64.b64decode(claimed["public_key_b64"]),
        identity_curve25519=ring.curve,
    )
    assert e2ee_v2.verify_ed25519(
        ring.ed, base64.b64decode(claimed["signature_b64"]), message
    )


# ---------------------------------------------------------------------------
# Registration, root device, pending devices
# ---------------------------------------------------------------------------


async def test_first_device_is_a_self_approved_root(
    client: AsyncClient, make_user: MakeUser
) -> None:
    owner = await make_user("Owner")
    viewer = await make_user("Viewer")
    root = Keyring(owner, 1)
    body = await register(client, root)

    assert body["approved"] is True and body["approved_at"] is not None
    assert body["approved_by_device_id"] is None, "NULL approver means ROOT"
    assert body["approval_signature_b64"] is None
    assert body["suite"] == "vodozemac-olm-v1" and body["generation"] == 1
    assert body["identity_curve25519_b64"] == b64(root.curve)
    assert body["identity_ed25519_b64"] == b64(root.ed)
    assert body["revoked_at"] is None

    (entry,) = await directory(client, viewer, owner.id)
    assert entry["device_id"] == root.id
    assert entry["approved_by_device_id"] is None
    assert entry["approver_identity_ed25519_b64"] is None
    counts = await _counts(client, root)
    assert counts["one_time_keys_available"] == 3 and counts["fallback_keys_available"] == 1


async def test_later_device_is_pending_and_invisible_until_approved(
    client: AsyncClient, make_user: MakeUser
) -> None:
    owner = await make_user("Owner")
    viewer = await make_user("Viewer")
    root = Keyring(owner, 1)
    await register(client, root)
    phone = Keyring(owner, 2)
    pending = await register(client, phone)

    assert pending["approved"] is False and pending["approved_at"] is None
    assert [d["device_id"] for d in await directory(client, viewer, owner.id)] == [root.id]
    # A pending device cannot be claimed.
    unavailable = await _claim(client, viewer, phone, expect=409)
    assert unavailable == {"detail": "device_unavailable", "device_ids": [phone.id]}

    approved = await approve(client, approver=root, new=phone)
    assert approved["approved"] is True and approved["approved_by_device_id"] == root.id
    assert approved["approval_signature_b64"] == b64(root.approval_for(phone))

    entries = {d["device_id"]: d for d in await directory(client, viewer, owner.id)}
    assert set(entries) == {root.id, phone.id}
    chain = entries[phone.id]
    assert chain["approved_by_device_id"] == root.id
    assert chain["approval_signature_b64"] == b64(root.approval_for(phone))
    assert chain["approver_identity_ed25519_b64"] == b64(root.ed)
    assert entries[root.id]["approved_by_device_id"] is None
    claimed = await _claim(client, viewer, phone)
    _assert_claim_verifies(phone, claimed["keys"][0])


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda r, b: b.update(generation=r.generation + 1), id="generation"),
        pytest.param(lambda r, b: b.update(identity_curve25519_b64=b64(os.urandom(32))), id="curve25519"),
        pytest.param(
            lambda r, b: b.update(identity_ed25519_b64=b64(Keyring(r.user, 1).ed)), id="ed25519"
        ),
        pytest.param(
            lambda r, b: b.update(binding_signature_b64=_flip(b["binding_signature_b64"])),
            id="signature-bit",
        ),
        pytest.param(
            lambda r, b: b.update(
                binding_signature_b64=b64(Ed25519PrivateKey.generate().sign(b"x" * 64))
            ),
            id="signed-by-other-key",
        ),
        pytest.param(
            lambda r, b: b.update(binding_signature_b64=b64(r.sign_binding(suite="vodozemac-olm-v2"))),
            id="signed-for-other-suite",
        ),
        pytest.param(
            lambda r, b: b.update(binding_signature_b64=b64(r.sign_binding(user_id=str(uuid.uuid4())))),
            id="signed-for-other-user",
        ),
    ],
)
async def test_binding_signature_covers_every_field(
    client: AsyncClient, make_user: MakeUser, mutate: Any
) -> None:
    owner = await make_user("Owner")
    ring = Keyring(owner, 1)
    body = ring.registration_body()
    mutate(ring, body)
    response = await client.post("/v2/devices", json=body, headers=owner.headers)
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "invalid_binding_signature"
    async with get_session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(models.Device)) == 0


async def test_a_registration_body_replayed_by_another_account_is_refused(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """The binding signs the user id, so a copied (valid) body is useless to anyone else."""
    victim = await make_user("Victim")
    attacker = await make_user("Attacker")
    body = Keyring(victim, 1).registration_body()
    response = await client.post("/v2/devices", json=body, headers=attacker.headers)
    assert response.status_code == 422 and response.json()["detail"] == "invalid_binding_signature"


async def test_unsupported_suite_has_its_own_fixed_code(
    client: AsyncClient, make_user: MakeUser
) -> None:
    owner = await make_user("Owner")
    ring = Keyring(owner, 1)
    body = ring.registration_body()
    body["suite"] = "vodozemac-olm-v2"
    body["binding_signature_b64"] = b64(ring.sign_binding(suite="vodozemac-olm-v2"))
    response = await client.post("/v2/devices", json=body, headers=owner.headers)
    assert response.status_code == 422 and response.json()["detail"] == "unsupported_suite"


@pytest.mark.parametrize(
    ("where", "mutate", "code"),
    [
        ("one_time", lambda r, k: k.update(key_id=k["key_id"] + 1000), "invalid_one_time_key_signature"),
        ("one_time", lambda r, k: k.update(public_key_b64=b64(os.urandom(32))), "invalid_one_time_key_signature"),
        ("one_time", lambda r, k: k.update(signature_b64=_flip(k["signature_b64"])), "invalid_one_time_key_signature"),
        # A fallback-signed key submitted as a one-time key (and the reverse): the signed
        # kind is part of the bytes.
        ("one_time", lambda r, k: k.update(**r.signed_key("fallback")), "invalid_one_time_key_signature"),
        # Signed for ANOTHER device's Curve25519 identity: not re-publishable here.
        (
            "one_time",
            lambda r, k: k.update(**Keyring(r.user, 9).signed_key("one_time")),
            "invalid_one_time_key_signature",
        ),
        ("fallback", lambda r, k: k.update(key_id=k["key_id"] + 1000), "invalid_fallback_key_signature"),
        ("fallback", lambda r, k: k.update(public_key_b64=b64(os.urandom(32))), "invalid_fallback_key_signature"),
        ("fallback", lambda r, k: k.update(signature_b64=_flip(k["signature_b64"])), "invalid_fallback_key_signature"),
        ("fallback", lambda r, k: k.update(**r.signed_key("one_time")), "invalid_fallback_key_signature"),
    ],
)
async def test_key_signatures_are_verified_at_registration(
    client: AsyncClient, make_user: MakeUser, where: str, mutate: Any, code: str
) -> None:
    owner = await make_user("Owner")
    ring = Keyring(owner, 1)
    body = ring.registration_body()
    mutate(ring, body["one_time_keys"][1] if where == "one_time" else body["fallback_key"])
    response = await client.post("/v2/devices", json=body, headers=owner.headers)
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == code
    async with get_session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(models.Device)) == 0
        assert await session.scalar(select(func.count()).select_from(models.OneTimePrekey)) == 0


async def test_registration_input_bounds(client: AsyncClient, make_user: MakeUser) -> None:
    owner = await make_user("Owner")
    ring = Keyring(owner, 1)

    async def post(body: dict[str, Any]) -> Any:
        return await client.post("/v2/devices", json=body, headers=owner.headers)

    too_many = ring.registration_body(one_time_keys=201)
    assert (await post(too_many)).status_code == 422

    huge_id = ring.registration_body(one_time_keys=1)
    huge_id["one_time_keys"][0] = ring.signed_key("one_time", key_id=2**63)
    assert (await post(huge_id)).status_code == 422, "key ids must fit BIGINT"

    negative_id = ring.registration_body(one_time_keys=1)
    negative_id["one_time_keys"][0]["key_id"] = -1
    assert (await post(negative_id)).status_code == 422

    short_key = ring.registration_body(one_time_keys=1)
    short_key["identity_ed25519_b64"] = b64(os.urandom(31))
    assert (await post(short_key)).status_code == 422

    no_fallback = ring.registration_body()
    del no_fallback["fallback_key"]
    assert (await post(no_fallback)).status_code == 422, "the fallback key is mandatory"

    long_label = ring.registration_body()
    long_label["device_label"] = "x" * 101
    assert (await post(long_label)).status_code == 422

    duplicate = ring.registration_body(one_time_keys=2)
    duplicate["one_time_keys"][1] = duplicate["one_time_keys"][0]
    response = await post(duplicate)
    assert response.status_code == 422 and response.json()["detail"] == "duplicate_key_id"

    clash_with_fallback = ring.registration_body(one_time_keys=1)
    clash_with_fallback["fallback_key"] = ring.signed_key(
        "fallback", key_id=clash_with_fallback["one_time_keys"][0]["key_id"]
    )
    response = await post(clash_with_fallback)
    assert response.status_code == 422 and response.json()["detail"] == "duplicate_key_id"

    # Largest accepted key id works end to end.
    edge = ring.registration_body(one_time_keys=1)
    edge["one_time_keys"][0] = ring.signed_key("one_time", key_id=2**63 - 1)
    assert (await post(edge)).status_code == 201


async def test_generation_must_strictly_increase_across_every_device_row(
    client: AsyncClient, make_user: MakeUser
) -> None:
    owner = await make_user("Owner")
    other = await make_user("Other")

    async def attempt(user: Any, generation: int) -> Any:
        return await client.post(
            "/v2/devices",
            json=Keyring(user, generation).registration_body(one_time_keys=0),
            headers=user.headers,
        )

    # First device must be >= 1: 0 is stale against "no device yet" (max 0).
    zero = await attempt(owner, 0)
    assert zero.status_code == 409
    assert zero.json() == {"detail": "stale_generation", "max_generation": 0}

    first = await attempt(owner, 5)
    assert first.status_code == 201
    first_id = first.json()["id"]
    for stale in (5, 4, 1):
        response = await attempt(owner, stale)
        assert response.status_code == 409, response.text
        assert response.json() == {"detail": "stale_generation", "max_generation": 5}

    # A PENDING device's generation counts too.
    pending = await attempt(owner, 9)
    assert pending.status_code == 201 and pending.json()["approved"] is False
    assert (await attempt(owner, 8)).json() == {"detail": "stale_generation", "max_generation": 9}

    # So does a REVOKED device's: revoking never lets a number be reused.
    revoked = await client.delete(f"/v2/devices/{pending.json()['id']}", headers=owner.headers)
    assert revoked.status_code == 200
    assert (await attempt(owner, 9)).json() == {"detail": "stale_generation", "max_generation": 9}
    assert (await attempt(owner, 10)).status_code == 201

    # Generations are per account: another user starts from 1 again, and the root revoke
    # of first_id did not matter above.
    assert first_id
    assert (await attempt(other, 1)).status_code == 201

    # u32 ceiling: the signed encoding itself cannot represent 2**32, so the body is
    # edited after signing to prove the API refuses it too.
    overflow = Keyring(other, 2**32 - 1).registration_body(one_time_keys=0)
    overflow["generation"] = 2**32
    refused = await client.post("/v2/devices", json=overflow, headers=other.headers)
    assert refused.status_code == 422
    assert (await attempt(other, 2**32 - 1)).status_code == 201


async def test_simultaneous_registrations_serialize_per_account(
    client: AsyncClient, make_user: MakeUser
) -> None:
    owner = await make_user("Owner")

    # Same generation at once: exactly one wins, the other sees it as stale.
    rings = [Keyring(owner, 1), Keyring(owner, 1)]
    responses = await asyncio.gather(*(
        client.post("/v2/devices", json=r.registration_body(one_time_keys=0), headers=owner.headers)
        for r in rings
    ))
    assert sorted(r.status_code for r in responses) == [201, 409]
    loser = next(r for r in responses if r.status_code == 409)
    assert loser.json() == {"detail": "stale_generation", "max_generation": 1}

    # Two DIFFERENT generations at once on a fresh account. Which request takes the account
    # lock first is up to the scheduler: gen 1 then gen 2 gives two registrations, gen 2
    # then gen 1 gives one registration and one stale_generation. Both are correct, so the
    # test pins only what must hold in EVERY order: at least one succeeds, a refusal is
    # always stale_generation, and the account never ends up with two roots - the later
    # registration must see the committed root and come out pending.
    other = await make_user("Other")
    responses = await asyncio.gather(*(
        client.post(
            "/v2/devices",
            json=Keyring(other, g).registration_body(one_time_keys=0),
            headers=other.headers,
        )
        for g in (1, 2)
    ))
    created = [r for r in responses if r.status_code == 201]
    refused = [r for r in responses if r.status_code != 201]
    assert created, [r.text for r in responses]
    assert all(
        r.status_code == 409 and r.json()["detail"] == "stale_generation" for r in refused
    ), [r.text for r in refused]
    # The first registration to commit is the root (approved); a second one is pending.
    expected_approved = [True] if len(created) == 1 else [False, True]
    assert sorted(r.json()["approved"] for r in created) == expected_approved
    async with get_session_factory()() as session:
        roots = await session.scalar(
            select(func.count()).select_from(models.Device).where(
                models.Device.user_id == uuid.UUID(other.id),
                models.Device.approved_at.is_not(None),
                models.Device.approved_by_device_id.is_(None),
            )
        )
    assert roots == 1, "exactly one root device per account"


async def test_the_same_identity_cannot_be_registered_twice(
    client: AsyncClient, make_user: MakeUser
) -> None:
    owner = await make_user("Owner")
    other = await make_user("Other")
    ring = Keyring(owner, 1)
    await register(client, ring)

    # Same account, new generation, same keys: "a new identity is a new device".
    same_account = Keyring(owner, 2, signing_key=ring.signing_key, curve=ring.curve)
    response = await client.post("/v2/devices", json=same_account.registration_body(), headers=owner.headers)
    assert response.status_code == 409 and response.json()["detail"] == "identity_already_registered"

    # Another account re-using the Ed25519 key (it can sign a binding for its own id).
    cross_account = Keyring(other, 1, signing_key=ring.signing_key)
    response = await client.post("/v2/devices", json=cross_account.registration_body(), headers=other.headers)
    assert response.status_code == 409 and response.json()["detail"] == "identity_already_registered"


async def test_device_caps_are_respected(client: AsyncClient, make_user: MakeUser) -> None:
    owner = await make_user("Owner")
    rings = [Keyring(owner, n) for n in range(1, 7)]
    for ring in rings[:5]:
        await register(client, ring, one_time_keys=0)
    sixth = await client.post("/v2/devices", json=rings[5].registration_body(one_time_keys=0), headers=owner.headers)
    assert sixth.status_code == 409 and sixth.json()["detail"] == "active_device_limit_reached"

    # Revoking frees an active slot (but the row still counts toward the retained cap).
    assert (await client.delete(f"/v2/devices/{rings[4].id}", headers=owner.headers)).status_code == 200
    await register(client, rings[5], one_time_keys=0)

    # Retained cap: 20 rows including revoked history.
    other = await make_user("Other")
    async with get_session_factory()() as session:
        for n in range(1, 21):
            ring = Keyring(other, n)
            session.add(
                models.Device(
                    user_id=uuid.UUID(other.id), identity_key=ring.curve, crypto_suite="vodozemac-olm-v1",
                    identity_ed25519=ring.ed, generation=n, binding_signature=os.urandom(64),
                    revoked_at=datetime.now(timezone.utc),
                )
            )
        await session.commit()
    response = await client.post("/v2/devices", json=Keyring(other, 21).registration_body(), headers=other.headers)
    assert response.status_code == 409 and response.json()["detail"] == "device_storage_limit_reached"


async def test_registration_is_rate_limited(client: AsyncClient, make_user: MakeUser) -> None:
    owner = await make_user("Owner")
    statuses = []
    for generation in range(1, 12):
        response = await client.post(
            "/v2/devices",
            json=Keyring(owner, generation).registration_body(one_time_keys=0),
            headers=owner.headers,
        )
        statuses.append(response.status_code)
    assert statuses[:5] == [201] * 5
    assert statuses[5:10] == [409] * 5
    assert statuses[10] == 429
    assert response.json()["detail"] == "device_registration_rate_limited"


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------


async def _pending_pair(client: AsyncClient, user: Any) -> tuple[Keyring, Keyring]:
    root = Keyring(user, 1)
    await register(client, root)
    phone = Keyring(user, 2)
    await register(client, phone)
    return root, phone


async def _approve_raw(
    client: AsyncClient, caller: Any, target: Keyring, approver_id: str | None, signature: bytes
) -> Any:
    return await client.post(
        f"/v2/devices/{target.id}/approve",
        json={"approver_device_id": approver_id, "approval_signature_b64": b64(signature)},
        headers=caller.headers,
    )


async def test_another_users_device_cannot_approve(
    client: AsyncClient, make_user: MakeUser
) -> None:
    owner = await make_user("Owner")
    stranger = await make_user("Stranger")
    _, phone = await _pending_pair(client, owner)
    strangers_root = Keyring(stranger, 1)
    await register(client, strangers_root)

    # The stranger's device signs a perfectly valid approval of the owner's pending phone.
    signature = strangers_root.approval_for(phone)
    # As the owner: the approver is not theirs.
    response = await _approve_raw(client, owner, phone, strangers_root.id, signature)
    assert response.status_code == 403 and response.json()["detail"] == "invalid_approver"
    # As the stranger: the target is not theirs.
    response = await _approve_raw(client, stranger, phone, strangers_root.id, signature)
    assert response.status_code == 403 and response.json()["detail"] == "not_device_owner"
    assert (await directory(client, owner, owner.id))[0]["device_id"] != phone.id
    assert len(await directory(client, owner, owner.id)) == 1


async def test_only_an_approved_unrevoked_v2_device_may_approve(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    owner = await make_user("Owner")
    root, phone = await _pending_pair(client, owner)
    third = Keyring(owner, 3)
    await register(client, third)

    # A pending device cannot approve another (or itself).
    response = await _approve_raw(client, owner, third, phone.id, phone.approval_for(third))
    assert response.status_code == 403 and response.json()["detail"] == "invalid_approver"
    response = await _approve_raw(client, owner, phone, phone.id, phone.approval_for(phone))
    assert response.status_code == 403 and response.json()["detail"] == "invalid_approver"

    # A legacy device, an unknown id, and a revoked approver are all the same refusal.
    legacy = await register_device(owner)
    for approver_id in (legacy["id"], str(uuid.uuid4())):
        response = await _approve_raw(client, owner, phone, approver_id, root.approval_for(phone))
        assert response.status_code == 403 and response.json()["detail"] == "invalid_approver"
    await approve(client, approver=root, new=third)
    assert (await client.delete(f"/v2/devices/{third.id}", headers=owner.headers)).status_code == 200
    response = await _approve_raw(client, owner, phone, third.id, third.approval_for(phone))
    assert response.status_code == 403 and response.json()["detail"] == "invalid_approver"

    # Still pending after every refusal.
    assert [d["device_id"] for d in await directory(client, owner, owner.id)] == [root.id]


async def test_approval_signature_is_verified(client: AsyncClient, make_user: MakeUser) -> None:
    owner = await make_user("Owner")
    root, phone = await _pending_pair(client, owner)
    good = root.approval_for(phone)

    wrong_generation = Keyring(owner, phone.generation + 1, signing_key=phone.signing_key, curve=phone.curve)
    wrong_generation.id = phone.id
    wrong_curve = Keyring(owner, phone.generation, signing_key=phone.signing_key)
    wrong_curve.id = phone.id
    bad_signatures = {
        "signed by the new device itself": phone.signing_key.sign(b"approve"),
        "approval for a different generation": root.approval_for(wrong_generation),
        "approval for different identities": root.approval_for(wrong_curve),
        "bit flipped": bytes([good[0] ^ 1]) + good[1:],
        "signed by an unrelated key": Ed25519PrivateKey.generate().sign(b"anything"),
        "binding bytes signed instead of approval bytes": root.signing_key.sign(
            e2ee_v2.device_binding_message(
                suite="vodozemac-olm-v1", user_id=uuid.UUID(owner.id), generation=phone.generation,
                identity_curve25519=phone.curve, identity_ed25519=phone.ed,
            )
        ),
    }
    for why, signature in bad_signatures.items():
        response = await _approve_raw(client, owner, phone, root.id, signature)
        assert response.status_code == 422, why
        assert response.json()["detail"] == "invalid_approval_signature", why

    refused = await client.get(f"/v2/users/{owner.id}/devices", headers=owner.headers)
    assert [d["device_id"] for d in refused.json()["devices"]] == [root.id], "must stay pending"
    await approve(client, approver=root, new=phone)  # the genuine one still works


async def test_approval_edge_cases(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    owner = await make_user("Owner")
    root, phone = await _pending_pair(client, owner)

    missing = Keyring(owner, 3)
    missing.id = str(uuid.uuid4())
    response = await _approve_raw(client, owner, missing, root.id, root.approval_for(missing))
    assert response.status_code == 404 and response.json()["detail"] == "device_not_found"

    first = await _approve_raw(client, owner, phone, root.id, root.approval_for(phone))
    assert first.status_code == 200
    # Retrying the SAME approval is idempotent (a lost response); a different approver is not.
    again = await _approve_raw(client, owner, phone, root.id, root.approval_for(phone))
    assert again.status_code == 200 and again.json()["approved_by_device_id"] == root.id
    third = Keyring(owner, 3)
    await register(client, third)
    await approve(client, approver=root, new=third)
    conflicting = await _approve_raw(client, owner, phone, third.id, third.approval_for(phone))
    assert conflicting.status_code == 409
    assert conflicting.json()["detail"] == "device_already_approved"

    # A legacy target and a revoked target.
    legacy = await register_device(owner)
    response = await client.post(
        f"/v2/devices/{legacy['id']}/approve",
        json={"approver_device_id": root.id, "approval_signature_b64": b64(os.urandom(64))},
        headers=owner.headers,
    )
    assert response.status_code == 403 and response.json()["detail"] == "device_suite_unsupported"
    fourth = Keyring(owner, 4)
    await register(client, fourth)
    assert (await client.delete(f"/v2/devices/{fourth.id}", headers=owner.headers)).status_code == 200
    response = await _approve_raw(client, owner, fourth, root.id, root.approval_for(fourth))
    assert response.status_code == 403 and response.json()["detail"] == "device_revoked"


# ---------------------------------------------------------------------------
# Revocation and the root rule
# ---------------------------------------------------------------------------


async def test_revoke_removes_a_device_everywhere_and_is_idempotent(
    client: AsyncClient, make_user: MakeUser
) -> None:
    owner = await make_user("Owner")
    viewer = await make_user("Viewer")
    root, phone = await register_approved_pair(client, owner)

    first = await client.delete(f"/v2/devices/{phone.id}", headers=owner.headers)
    assert first.status_code == 200 and first.json()["revoked_at"] is not None
    second = await client.delete(f"/v2/devices/{phone.id}", headers=owner.headers)
    assert second.status_code == 200 and second.json()["revoked_at"] == first.json()["revoked_at"]

    assert [d["device_id"] for d in await directory(client, viewer, owner.id)] == [root.id]
    unavailable = await _claim(client, viewer, phone, expect=409)
    assert unavailable["device_ids"] == [phone.id]

    # Only the owner, and only v2 devices.
    stranger = await make_user("Stranger")
    response = await client.delete(f"/v2/devices/{root.id}", headers=stranger.headers)
    assert response.status_code == 403 and response.json()["detail"] == "not_device_owner"
    response = await client.delete(f"/v2/devices/{uuid.uuid4()}", headers=owner.headers)
    assert response.status_code == 404


async def test_revoking_a_legacy_device_through_v2_is_refused(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    owner = await make_user("Owner")
    legacy = await register_device(owner)
    response = await client.delete(f"/v2/devices/{legacy['id']}", headers=owner.headers)
    assert response.status_code == 403 and response.json()["detail"] == "device_suite_unsupported"
    async with get_session_factory()() as session:
        row = await session.get(models.Device, uuid.UUID(legacy["id"]))
        assert row is not None and row.revoked_at is None


async def test_a_device_approved_by_a_since_revoked_device_stays_in_the_directory_with_its_chain(
    client: AsyncClient, make_user: MakeUser
) -> None:
    owner = await make_user("Owner")
    viewer = await make_user("Viewer")
    root, phone = await register_approved_pair(client, owner)
    assert (await client.delete(f"/v2/devices/{root.id}", headers=owner.headers)).status_code == 200
    (entry,) = await directory(client, viewer, owner.id)
    assert entry["device_id"] == phone.id
    assert entry["approved_by_device_id"] == root.id
    # The revoked approver is no longer listed, so its identity travels with the entry.
    assert entry["approver_identity_ed25519_b64"] == b64(root.ed)


async def test_registration_becomes_a_new_root_only_when_no_approver_is_left(
    client: AsyncClient, make_user: MakeUser
) -> None:
    owner = await make_user("Owner")
    root = Keyring(owner, 1)
    await register(client, root)
    second = Keyring(owner, 2)
    still_pending = await register(client, second)
    assert still_pending["approved"] is False

    # Lose every approved device ("Reset security"): the next registration is a new root.
    assert (await client.delete(f"/v2/devices/{root.id}", headers=owner.headers)).status_code == 200
    reset = Keyring(owner, 3)
    new_root = await register(client, reset)
    assert new_root["approved"] is True and new_root["approved_by_device_id"] is None
    assert new_root["generation"] == 3, "generation keeps increasing across the reset"

    # The old pending device is NOT promoted by that.
    entries = await directory(client, owner, owner.id)
    assert [d["device_id"] for d in entries] == [reset.id]


# ---------------------------------------------------------------------------
# Directory read
# ---------------------------------------------------------------------------


async def test_directory_lists_only_approved_unrevoked_v2_devices_and_claims_nothing(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    owner = await make_user("Owner")
    viewer = await make_user("Viewer")
    root, phone = await register_approved_pair(client, owner)
    pending = Keyring(owner, 3)
    await register(client, pending)
    await register_device(owner)  # a LEGACY device on the same account

    for _ in range(5):
        entries = await directory(client, viewer, owner.id)
        assert [d["device_id"] for d in entries] == [root.id, phone.id]
    # Reading the directory consumed nothing: the first claim still gets key id 1.
    assert (await _counts(client, root))["one_time_keys_available"] == 3
    first_claim = await _claim(client, viewer, root)
    assert first_claim["keys"][0]["key_id"] == 1 and first_claim["keys"][0]["kind"] == "one_time"

    entry = entries[0]
    assert set(entry) == {
        "device_id", "suite", "generation", "identity_curve25519_b64", "identity_ed25519_b64",
        "binding_signature_b64", "created_at", "approved_by_device_id",
        "approval_signature_b64", "approver_identity_ed25519_b64",
    }
    # The directory carries exactly what a client needs to re-verify the binding itself.
    assert e2ee_v2.verify_ed25519(
        base64.b64decode(entry["identity_ed25519_b64"]),
        base64.b64decode(entry["binding_signature_b64"]),
        e2ee_v2.device_binding_message(
            suite=entry["suite"], user_id=uuid.UUID(owner.id), generation=entry["generation"],
            identity_curve25519=base64.b64decode(entry["identity_curve25519_b64"]),
            identity_ed25519=base64.b64decode(entry["identity_ed25519_b64"]),
        ),
    )


async def test_directory_visibility_mirrors_the_v1_bundle_rule(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """Authenticated callers only, target must exist, rate-limited per (caller, target)."""
    owner = await make_user("Owner")
    viewer = await make_user("Viewer")
    await register(client, Keyring(owner, 1))

    assert (await client.get(f"/v2/users/{owner.id}/devices")).status_code == 401
    missing = await client.get(f"/v2/users/{uuid.uuid4()}/devices", headers=viewer.headers)
    assert missing.status_code == 404 and missing.json()["detail"] == "user_not_found"
    empty = await make_user("No Devices")
    assert (await directory(client, viewer, empty.id)) == []

    for _ in range(60):
        assert (await client.get(f"/v2/users/{owner.id}/devices", headers=viewer.headers)).status_code == 200
    limited = await client.get(f"/v2/users/{owner.id}/devices", headers=viewer.headers)
    assert limited.status_code == 429 and limited.json()["detail"] == "e2ee_directory_rate_limited"
    # The budget is per (caller, target): another target is unaffected.
    assert (await client.get(f"/v2/users/{viewer.id}/devices", headers=viewer.headers)).status_code == 200


# ---------------------------------------------------------------------------
# Top-up, count, fallback replacement
# ---------------------------------------------------------------------------


async def test_top_up_and_count(client: AsyncClient, make_user: MakeUser) -> None:
    owner = await make_user("Owner")
    ring = Keyring(owner, 1)
    await register(client, ring, one_time_keys=3)

    response = await client.post(
        f"/v2/devices/{ring.id}/keys",
        json={"one_time_keys": [ring.signed_key("one_time") for _ in range(5)]},
        headers=owner.headers,
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "device_id": ring.id, "one_time_keys_available": 8, "fallback_keys_available": 1,
    }
    assert await _counts(client, ring) == response.json()


async def test_replacing_the_fallback_key_swaps_the_one_row(
    client: AsyncClient, make_user: MakeUser
) -> None:
    owner = await make_user("Owner")
    viewer = await make_user("Viewer")
    ring = Keyring(owner, 1)
    await register(client, ring, one_time_keys=0)
    old = (await _claim(client, viewer, ring))["keys"][0]
    assert old["kind"] == "fallback"

    new_key = ring.signed_key("fallback")
    response = await client.post(
        f"/v2/devices/{ring.id}/keys", json={"fallback_key": new_key}, headers=owner.headers
    )
    assert response.status_code == 200 and response.json()["fallback_keys_available"] == 1
    claimed = (await _claim(client, viewer, ring))["keys"][0]
    assert claimed["kind"] == "fallback" and claimed["key_id"] == new_key["key_id"] != old["key_id"]
    assert claimed["public_key_b64"] == new_key["public_key_b64"]
    async with get_session_factory()() as session:
        rows = (await session.execute(
            select(models.OneTimePrekey).where(models.OneTimePrekey.kind == "fallback")
        )).scalars().all()
        assert len(rows) == 1, "at most one fallback row per device"


@pytest.mark.parametrize(
    ("kind", "mutate", "code"),
    [
        ("one_time", lambda k: k.update(signature_b64=_flip(k["signature_b64"])), "invalid_one_time_key_signature"),
        ("one_time", lambda k: k.update(key_id=k["key_id"] + 77), "invalid_one_time_key_signature"),
        ("fallback", lambda k: k.update(public_key_b64=b64(os.urandom(32))), "invalid_fallback_key_signature"),
    ],
)
async def test_top_up_verifies_signatures_and_stores_nothing_on_failure(
    client: AsyncClient, make_user: MakeUser, kind: str, mutate: Any, code: str
) -> None:
    owner = await make_user("Owner")
    ring = Keyring(owner, 1)
    await register(client, ring, one_time_keys=1)
    good = ring.signed_key("one_time")
    bad = ring.signed_key(kind)
    mutate(bad)
    body = {"one_time_keys": [good, bad]} if kind == "one_time" else {"one_time_keys": [good], "fallback_key": bad}
    response = await client.post(f"/v2/devices/{ring.id}/keys", json=body, headers=owner.headers)
    assert response.status_code == 422 and response.json()["detail"] == code
    assert (await _counts(client, ring))["one_time_keys_available"] == 1, "no partial batch"


async def test_top_up_refuses_key_id_reuse_pool_overflow_and_empty_uploads(
    client: AsyncClient, make_user: MakeUser
) -> None:
    owner = await make_user("Owner")
    ring = Keyring(owner, 1)
    await register(client, ring, one_time_keys=3)  # key ids 1, 2, 3
    url = f"/v2/devices/{ring.id}/keys"

    reused = await client.post(
        url,
        json={"one_time_keys": [ring.signed_key("one_time"), ring.signed_key("one_time", key_id=2)]},
        headers=owner.headers,
    )
    assert reused.status_code == 409 and reused.json()["detail"] == "key_id_reused"
    assert (await _counts(client, ring))["one_time_keys_available"] == 3, "batch rolled back whole"

    inside = ring.signed_key("one_time")
    dupes = await client.post(url, json={"one_time_keys": [inside, inside]}, headers=owner.headers)
    assert dupes.status_code == 422 and dupes.json()["detail"] == "duplicate_key_id"

    empty = await client.post(url, json={}, headers=owner.headers)
    assert empty.status_code == 422 and empty.json()["detail"] == "no_keys_uploaded"

    fill = await client.post(
        url, json={"one_time_keys": [ring.signed_key("one_time") for _ in range(197)]}, headers=owner.headers
    )
    assert fill.status_code == 200 and fill.json()["one_time_keys_available"] == 200
    full = await client.post(url, json={"one_time_keys": [ring.signed_key("one_time")]}, headers=owner.headers)
    assert full.status_code == 409 and full.json()["detail"] == "one_time_key_pool_full"
    over_batch = await client.post(
        url, json={"one_time_keys": [ring.signed_key("one_time") for _ in range(201)]}, headers=owner.headers
    )
    assert over_batch.status_code == 422


async def test_top_up_and_count_are_owner_only_and_v2_only(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    owner = await make_user("Owner")
    stranger = await make_user("Stranger")
    ring = Keyring(owner, 1)
    await register(client, ring)
    body = {"one_time_keys": [ring.signed_key("one_time")]}

    for method, path, kwargs in (
        ("post", f"/v2/devices/{ring.id}/keys", {"json": body}),
        ("get", f"/v2/devices/{ring.id}/keys/count", {}),
    ):
        response = await getattr(client, method)(path, headers=stranger.headers, **kwargs)
        assert response.status_code == 403 and response.json()["detail"] == "not_device_owner"
    missing = await client.get(f"/v2/devices/{uuid.uuid4()}/keys/count", headers=owner.headers)
    assert missing.status_code == 404

    legacy = await register_device(owner)
    response = await client.get(f"/v2/devices/{legacy['id']}/keys/count", headers=owner.headers)
    assert response.status_code == 403 and response.json()["detail"] == "device_suite_unsupported"
    response = await client.post(f"/v2/devices/{legacy['id']}/keys", json=body, headers=owner.headers)
    assert response.status_code == 403 and response.json()["detail"] == "device_suite_unsupported"

    assert (await client.delete(f"/v2/devices/{ring.id}", headers=owner.headers)).status_code == 200
    response = await client.post(f"/v2/devices/{ring.id}/keys", json=body, headers=owner.headers)
    assert response.status_code == 403 and response.json()["detail"] == "device_revoked"


async def test_key_writes_are_rate_limited_per_device(client: AsyncClient, make_user: MakeUser) -> None:
    owner = await make_user("Owner")
    ring = Keyring(owner, 1)
    await register(client, ring, one_time_keys=0)
    url = f"/v2/devices/{ring.id}/keys"
    for _ in range(30):
        response = await client.post(url, json={"one_time_keys": [ring.signed_key("one_time")]}, headers=owner.headers)
        assert response.status_code == 200, response.text
    limited = await client.post(url, json={"one_time_keys": [ring.signed_key("one_time")]}, headers=owner.headers)
    assert limited.status_code == 429 and limited.json()["detail"] == "prekey_write_rate_limited"


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------


async def test_claim_hands_out_one_time_keys_in_order_then_falls_back_without_consuming(
    client: AsyncClient, make_user: MakeUser
) -> None:
    owner = await make_user("Owner")
    viewer = await make_user("Viewer")
    ring = Keyring(owner, 1)
    await register(client, ring, one_time_keys=2)

    first = (await _claim(client, viewer, ring))["keys"][0]
    second = (await _claim(client, viewer, ring))["keys"][0]
    assert (first["key_id"], second["key_id"]) == (1, 2)
    assert first["kind"] == second["kind"] == "one_time"
    for claimed in (first, second):
        assert claimed["device_id"] == ring.id
        _assert_claim_verifies(ring, claimed)
    assert (await _counts(client, ring))["one_time_keys_available"] == 0

    fallbacks = [(await _claim(client, viewer, ring))["keys"][0] for _ in range(3)]
    assert {f["kind"] for f in fallbacks} == {"fallback"}
    assert len({f["key_id"] for f in fallbacks}) == 1, "the same fallback key every time"
    _assert_claim_verifies(ring, fallbacks[0])
    assert (await _counts(client, ring))["fallback_keys_available"] == 1
    async with get_session_factory()() as session:
        row = (await session.execute(
            select(models.OneTimePrekey).where(models.OneTimePrekey.kind == "fallback")
        )).scalar_one()
        assert row.consumed_at is None, "a fallback key is never consumed"


async def test_claim_returns_keys_in_request_order_for_several_devices(
    client: AsyncClient, make_user: MakeUser
) -> None:
    owner = await make_user("Owner")
    viewer = await make_user("Viewer")
    root, phone = await register_approved_pair(client, owner, one_time_keys=1)
    keys = (await _claim(client, viewer, phone, root))["keys"]
    assert [k["device_id"] for k in keys] == [phone.id, root.id]
    _assert_claim_verifies(phone, keys[0])
    _assert_claim_verifies(root, keys[1])
    assert (await _counts(client, root))["one_time_keys_available"] == 0
    assert (await _counts(client, phone))["one_time_keys_available"] == 0


async def test_concurrent_claims_never_hand_out_the_same_one_time_key(
    client: AsyncClient, make_user: MakeUser
) -> None:
    owner = await make_user("Owner")
    viewer = await make_user("Viewer")
    ring = Keyring(owner, 1)
    await register(client, ring, one_time_keys=3)

    responses = await asyncio.gather(*(
        client.post("/v2/keys/claim", json={"device_ids": [ring.id]}, headers=viewer.headers)
        for _ in range(8)
    ))
    assert [r.status_code for r in responses] == [200] * 8
    claimed = [r.json()["keys"][0] for r in responses]
    one_time = [k["key_id"] for k in claimed if k["kind"] == "one_time"]
    assert sorted(one_time) == [1, 2, 3], "each one-time key is handed out exactly once"
    fallbacks = [k for k in claimed if k["kind"] == "fallback"]
    assert len(fallbacks) == 5 and len({k["key_id"] for k in fallbacks}) == 1
    assert (await _counts(client, ring))["one_time_keys_available"] == 0


async def test_concurrent_claims_across_callers_split_one_key_exactly(
    client: AsyncClient, make_user: MakeUser
) -> None:
    owner = await make_user("Owner")
    callers = [await make_user(f"Caller {n}") for n in range(4)]
    ring = Keyring(owner, 1)
    await register(client, ring, one_time_keys=1)
    responses = await asyncio.gather(*(
        client.post("/v2/keys/claim", json={"device_ids": [ring.id]}, headers=c.headers) for c in callers
    ))
    kinds = sorted(r.json()["keys"][0]["kind"] for r in responses)
    assert kinds == ["fallback", "fallback", "fallback", "one_time"]


async def test_claim_is_all_or_nothing_when_a_device_is_unavailable(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    owner = await make_user("Owner")
    viewer = await make_user("Viewer")
    root, phone = await register_approved_pair(client, owner, one_time_keys=2)
    pending = Keyring(owner, 3)
    await register(client, pending)
    revoked = Keyring(owner, 4)
    await register(client, revoked)
    await approve(client, approver=root, new=revoked)
    assert (await client.delete(f"/v2/devices/{revoked.id}", headers=owner.headers)).status_code == 200
    legacy = await register_device(owner)
    ghost = str(uuid.uuid4())

    response = await client.post(
        "/v2/keys/claim",
        json={"device_ids": [root.id, pending.id, revoked.id, legacy["id"], ghost, phone.id]},
        headers=viewer.headers,
    )
    assert response.status_code == 409
    body = response.json()
    assert body["detail"] == "device_unavailable"
    assert body["device_ids"] == [pending.id, revoked.id, legacy["id"], ghost]
    # The two valid devices lost nothing.
    assert (await _counts(client, root))["one_time_keys_available"] == 2
    assert (await _counts(client, phone))["one_time_keys_available"] == 2


async def test_claim_input_bounds_and_rate_limits(client: AsyncClient, make_user: MakeUser) -> None:
    owner = await make_user("Owner")
    viewer = await make_user("Viewer")
    ring = Keyring(owner, 1)
    await register(client, ring, one_time_keys=0)

    async def post(ids: list[str]) -> Any:
        return await client.post("/v2/keys/claim", json={"device_ids": ids}, headers=viewer.headers)

    assert (await post([])).status_code == 422
    assert (await post([str(uuid.uuid4()) for _ in range(11)])).status_code == 422
    duplicate = await post([ring.id, ring.id])
    assert duplicate.status_code == 422 and duplicate.json()["detail"] == "duplicate_device_ids"
    assert (await client.post("/v2/keys/claim", json={"device_ids": [ring.id]})).status_code == 401

    # Per (caller, target user): the v1 bundle number, 10 per window.
    for _ in range(10):
        assert (await post([ring.id])).status_code == 200
    limited = await post([ring.id])
    assert limited.status_code == 429 and limited.json()["detail"] == "e2ee_claim_rate_limited"
    # A different caller has its own budget.
    other_viewer = await make_user("Other Viewer")
    response = await client.post("/v2/keys/claim", json={"device_ids": [ring.id]}, headers=other_viewer.headers)
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Legacy rows are left alone
# ---------------------------------------------------------------------------


async def test_legacy_v1_flows_are_untouched_by_v2_devices(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    owner = await make_user("Owner")
    viewer = await make_user("Viewer")
    legacy = await register_device(owner, one_time_prekey_count=2)
    ring = Keyring(owner, 1)
    await register(client, ring, one_time_keys=3)

    # v1 still returns ONLY the legacy device's bundle and never touches v2 key material.
    bundle = await client.get(f"/users/{owner.id}/prekey-bundle", headers=viewer.headers)
    assert bundle.status_code == 200, bundle.text
    assert [d["device_id"] for d in bundle.json()["devices"]] == [legacy["id"]]
    assert (await _counts(client, ring))["one_time_keys_available"] == 3

    # The v1 replenish route cannot push unsigned v1 keys onto a v2 device, and a v1 key
    # written there by other means is never handed out by a v2 claim.
    refused = await client.post(
        f"/devices/{ring.id}/prekeys",
        json={"one_time_prekeys": [{"key_id": 99, "public_key_b64": b64(os.urandom(32))}]},
        headers=owner.headers,
    )
    assert refused.status_code == 403 and refused.json()["detail"] == "device_requires_v2"
    async with get_session_factory()() as session:
        session.add(models.OneTimePrekey(
            device_id=uuid.UUID(ring.id), key_id=99, public_key=os.urandom(32),
        ))
        await session.commit()
    claimed = [(await _claim(client, viewer, ring))["keys"][0] for _ in range(4)]
    assert 99 not in {k["key_id"] for k in claimed}, "an unsigned row must not be claimable"
    assert [k["kind"] for k in claimed] == ["one_time"] * 3 + ["fallback"]

    # The legacy device never shows up in the v2 directory, and its v1 count still works.
    assert [d["device_id"] for d in await directory(client, viewer, owner.id)] == [ring.id]
    count = await client.get(f"/devices/{legacy['id']}/prekeys/count", headers=owner.headers)
    assert count.status_code == 200 and count.json()["one_time_prekeys_available"] == 1
