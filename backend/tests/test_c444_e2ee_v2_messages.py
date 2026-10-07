"""E2EE v2 message transport: per-device legs, exact device set, idempotency, history (c444).

Fixture world used throughout: Alice has two approved devices (a1 root, a2), Bob has one
(b1), and they share a DM. A message from a1 therefore needs exactly two legs: a2 (Alice's
own other phone, so her history stays in sync) and b1.
"""
from __future__ import annotations

import asyncio
import base64
import json
import uuid
from datetime import datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, text

from app import models
from app.db import get_session_factory
from app.schemas.e2ee_v2 import MAX_V2_LEGS_PER_MESSAGE
from app.services import outbox
from tests.conftest import ApiUser, MakeUser, RegisterDevice, b64, share_verified_campus
from tests.e2ee_v2_helpers import (
    Keyring,
    approve,
    leg,
    open_dm,
    register,
    register_approved_pair,
    send,
)


class World:
    def __init__(self, alice: ApiUser, bob: ApiUser, a1: Keyring, a2: Keyring, b1: Keyring, convo: str):
        self.alice, self.bob, self.a1, self.a2, self.b1, self.convo = alice, bob, a1, a2, b1, convo

    def required_from_a1(self) -> list[dict[str, Any]]:
        return [leg(self.a2), leg(self.b1)]


async def _world(client: AsyncClient, make_user: MakeUser) -> World:
    alice = await make_user("Alice")
    bob = await make_user("Bob")
    a1, a2 = await register_approved_pair(client, alice)
    b1 = Keyring(bob, 1)
    await register(client, b1)
    return World(alice, bob, a1, a2, b1, await open_dm(client, alice, bob))


async def _history(
    client: AsyncClient, world: World, ring: Keyring, *, user: ApiUser | None = None,
    expect: int = 200, **params: Any,
) -> Any:
    response = await client.get(
        f"/v2/conversations/{world.convo}/messages",
        params={"device_id": ring.id, **params},
        headers=(user or ring.user).headers,
    )
    assert response.status_code == expect, response.text
    return response.json()


async def _row_counts() -> dict[str, int]:
    async with get_session_factory()() as session:
        return {
            "messages": await session.scalar(select(func.count()).select_from(models.Message)) or 0,
            "legs": await session.scalar(select(func.count()).select_from(models.MessageLeg)) or 0,
            "outbox": await session.scalar(select(func.count()).select_from(models.DeliveryOutbox)) or 0,
        }


# ---------------------------------------------------------------------------
# The happy path and per-device history
# ---------------------------------------------------------------------------


async def test_send_stores_one_message_with_one_leg_per_recipient_device(
    client: AsyncClient, make_user: MakeUser
) -> None:
    w = await _world(client, make_user)
    cmid = str(uuid.uuid4())
    response = await send(
        client, w.convo, w.a1,
        [leg(w.a2, olm_type=0, body=b"for-a2"), leg(w.b1, olm_type=1, body=b"for-b1")],
        client_message_id=cmid, expect=201,
    )
    body = response.json()
    assert set(body) == {"id", "conversation_id", "sender_device_id", "client_message_id", "envelope_version", "created_at"}
    assert body["conversation_id"] == w.convo and body["sender_device_id"] == w.a1.id
    assert body["client_message_id"] == cmid and body["envelope_version"] == 1

    async with get_session_factory()() as session:
        message = await session.get(models.Message, uuid.UUID(body["id"]))
        assert message is not None
        assert message.ciphertext is None and message.envelope_version == 1
        legs = {
            str(row.recipient_device_id): (row.olm_type, bytes(row.ciphertext))
            for row in (await session.execute(select(models.MessageLeg))).scalars()
        }
    assert legs == {w.a2.id: (0, b"for-a2"), w.b1.id: (1, b"for-b1")}


async def test_each_device_reads_only_its_own_leg(client: AsyncClient, make_user: MakeUser) -> None:
    w = await _world(client, make_user)
    first = (await send(
        client, w.convo, w.a1,
        [leg(w.a2, olm_type=0, body=b"A2-SECRET"), leg(w.b1, olm_type=1, body=b"B1-SECRET")],
        expect=201,
    )).json()

    for ring, own, other in ((w.b1, b"B1-SECRET", b"A2-SECRET"), (w.a2, b"A2-SECRET", b"B1-SECRET")):
        raw = json.dumps(await _history(client, w, ring))
        (row,) = json.loads(raw)
        assert row["id"] == first["id"] and row["sender_device_id"] == w.a1.id
        assert base64.b64decode(row["leg"]["ciphertext_b64"]) == own
        assert b64(other) not in raw, "another device's leg must never appear"
        assert "ciphertext" not in {k for k in row}, "no message-level ciphertext"
    assert (await _history(client, w, w.b1))[0]["leg"]["olm_type"] == 1
    assert (await _history(client, w, w.a2))[0]["leg"]["olm_type"] == 0
    # The sender's own device has no leg for its own message: omitted, not empty.
    assert await _history(client, w, w.a1) == []

    # A reply from Bob reaches BOTH of Alice's phones.
    await send(client, w.convo, w.b1, [leg(w.a1), leg(w.a2)], expect=201)
    assert len(await _history(client, w, w.a1)) == 1
    assert len(await _history(client, w, w.a2)) == 2
    assert len(await _history(client, w, w.b1)) == 1


async def test_a_device_approved_later_has_no_history_for_earlier_messages(
    client: AsyncClient, make_user: MakeUser
) -> None:
    w = await _world(client, make_user)
    await send(client, w.convo, w.a1, w.required_from_a1(), expect=201)
    a3 = Keyring(w.alice, 3)
    await register(client, a3)
    await approve(client, approver=w.a1, new=a3)
    assert await _history(client, w, a3) == [], "messages that predate a device have no leg for it"
    await send(client, w.convo, w.a1, [leg(w.a2), leg(a3), leg(w.b1)], expect=201)
    assert len(await _history(client, w, a3)) == 1


async def test_history_pagination_uses_the_created_at_id_cursor(
    client: AsyncClient, make_user: MakeUser
) -> None:
    w = await _world(client, make_user)
    ids = [
        (await send(client, w.convo, w.a1, w.required_from_a1(), expect=201)).json()["id"]
        for _ in range(5)
    ]
    page1 = await _history(client, w, w.b1, limit=2)
    assert [m["id"] for m in page1] == ids[::-1][:2], "newest first"
    page2 = await _history(client, w, w.b1, limit=2, before=page1[-1]["created_at"], before_id=page1[-1]["id"])
    assert [m["id"] for m in page2] == ids[::-1][2:4]
    page3 = await _history(client, w, w.b1, limit=2, before=page2[-1]["created_at"], before_id=page2[-1]["id"])
    assert [m["id"] for m in page3] == ids[::-1][4:]
    assert (await client.get(
        f"/v2/conversations/{w.convo}/messages", params={"device_id": w.b1.id, "limit": 201},
        headers=w.bob.headers,
    )).status_code == 422


async def test_history_requires_membership_and_a_usable_owned_device(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    w = await _world(client, make_user)
    stranger = await make_user("Stranger")
    stranger_ring = Keyring(stranger, 1)
    await register(client, stranger_ring)

    # Not a member of the conversation.
    refused = await _history(client, w, stranger_ring, expect=403)
    assert refused["detail"] == "not_a_member"
    # Member, but the device is somebody else's.
    refused = await _history(client, w, w.a1, user=w.bob, expect=403)
    assert refused["detail"] == "not_your_device"
    # device_id is required.
    missing = await client.get(f"/v2/conversations/{w.convo}/messages", headers=w.bob.headers)
    assert missing.status_code == 422

    pending = Keyring(w.alice, 3)
    await register(client, pending)
    assert (await _history(client, w, pending, expect=403))["detail"] == "device_not_approved"
    legacy = await register_device(w.alice)
    legacy_ring = Keyring(w.alice, 99)
    legacy_ring.id = legacy["id"]
    assert (await _history(client, w, legacy_ring, expect=403))["detail"] == "device_suite_unsupported"
    assert (await client.delete(f"/v2/devices/{w.a2.id}", headers=w.alice.headers)).status_code == 200
    assert (await _history(client, w, w.a2, expect=403))["detail"] == "device_revoked"


# ---------------------------------------------------------------------------
# The exact-device-set rule
# ---------------------------------------------------------------------------


async def _expect_mismatch(
    client: AsyncClient, w: World, sender: Keyring, legs: list[dict[str, Any]], required: list[Keyring]
) -> None:
    before = await _row_counts()
    response = await send(client, w.convo, sender, legs)
    assert response.status_code == 409, response.text
    assert response.json() == {
        "detail": "device_list_mismatch",
        "device_ids": sorted(r.id for r in required if r.id),
    }
    assert await _row_counts() == before, "NOTHING is stored on a mismatch"


async def test_exact_device_set_rejects_a_missing_device(client: AsyncClient, make_user: MakeUser) -> None:
    w = await _world(client, make_user)
    required = [w.a2, w.b1]
    await _expect_mismatch(client, w, w.a1, [leg(w.b1)], required)  # forgot the sender's other phone
    await _expect_mismatch(client, w, w.a1, [leg(w.a2)], required)  # forgot the recipient
    # Bob's second device appears (approved): a message built for the old set is stale.
    b2 = Keyring(w.bob, 2)
    await register(client, b2)
    await approve(client, approver=w.b1, new=b2)
    await _expect_mismatch(client, w, w.a1, w.required_from_a1(), [w.a2, w.b1, b2])
    await send(client, w.convo, w.a1, [leg(w.a2), leg(w.b1), leg(b2)], expect=201)


async def test_exact_device_set_rejects_an_extra_device(client: AsyncClient, make_user: MakeUser) -> None:
    w = await _world(client, make_user)
    required = [w.a2, w.b1]
    stranger = await make_user("Stranger")
    outsider = Keyring(stranger, 1)
    await register(client, outsider)
    ghost = Keyring(w.alice, 99)
    ghost.id = str(uuid.uuid4())

    await _expect_mismatch(client, w, w.a1, [*w.required_from_a1(), leg(outsider)], required)
    await _expect_mismatch(client, w, w.a1, [*w.required_from_a1(), leg(ghost)], required)
    # A leg to the sender's OWN sending device is an extra one too.
    await _expect_mismatch(client, w, w.a1, [*w.required_from_a1(), leg(w.a1)], required)


async def test_a_revoked_device_is_neither_required_nor_allowed(
    client: AsyncClient, make_user: MakeUser
) -> None:
    w = await _world(client, make_user)
    a3 = Keyring(w.alice, 3)
    await register(client, a3)
    await approve(client, approver=w.a1, new=a3)
    await send(client, w.convo, w.a1, [leg(w.a2), leg(a3), leg(w.b1)], expect=201)

    assert (await client.delete(f"/v2/devices/{a3.id}", headers=w.alice.headers)).status_code == 200
    # Still addressing the revoked device is an EXTRA leg ...
    await _expect_mismatch(client, w, w.a1, [leg(w.a2), leg(a3), leg(w.b1)], [w.a2, w.b1])
    # ... and the set without it is exactly right.
    await send(client, w.convo, w.a1, w.required_from_a1(), expect=201)


async def test_a_pending_device_is_neither_required_nor_allowed_until_approved(
    client: AsyncClient, make_user: MakeUser
) -> None:
    w = await _world(client, make_user)
    a3 = Keyring(w.alice, 3)
    await register(client, a3)  # pending
    await send(client, w.convo, w.a1, w.required_from_a1(), expect=201)  # not required
    await _expect_mismatch(client, w, w.a1, [*w.required_from_a1(), leg(a3)], [w.a2, w.b1])  # not allowed
    # A pending device cannot SEND either.
    refused = await send(client, w.convo, a3, [leg(w.a1), leg(w.a2), leg(w.b1)])
    assert refused.status_code == 403 and refused.json()["detail"] == "device_not_approved"

    await approve(client, approver=w.a1, new=a3)
    await _expect_mismatch(client, w, w.a1, w.required_from_a1(), [w.a2, a3, w.b1])
    await send(client, w.convo, w.a1, [leg(w.a2), leg(a3), leg(w.b1)], expect=201)


async def test_a_message_nobody_else_could_read_is_refused(
    client: AsyncClient, make_user: MakeUser
) -> None:
    alice = await make_user("Alice")
    bob = await make_user("Bob")
    a1 = Keyring(alice, 1)
    await register(client, a1)
    convo = await open_dm(client, alice, bob)

    # Bob has no v2 device: the only possible leg set is empty or Alice's own phones.
    response = await send(client, convo, a1, [leg(a1)])
    assert response.status_code == 409 and response.json()["detail"] == "no_recipient_devices"
    assert await _row_counts() == {"messages": 0, "legs": 0, "outbox": 0}

    # Bob registers a legacy-free v2 device, then leaves: no active recipient remains.
    b1 = Keyring(bob, 1)
    await register(client, b1)
    await send(client, convo, a1, [leg(b1)], expect=201)
    assert (await client.post(f"/conversations/{convo}/leave", headers=bob.headers)).status_code == 200
    response = await send(client, convo, a1, [leg(b1)])
    assert response.status_code == 409 and response.json()["detail"] == "no_recipient_devices"


# ---------------------------------------------------------------------------
# Idempotency on (sender_device_id, client_message_id)
# ---------------------------------------------------------------------------


async def test_a_retry_with_the_same_legs_returns_the_stored_message(
    client: AsyncClient, make_user: MakeUser
) -> None:
    w = await _world(client, make_user)
    cmid = str(uuid.uuid4())
    legs = [leg(w.a2, body=b"x2"), leg(w.b1, body=b"x1")]
    first = await send(client, w.convo, w.a1, legs, client_message_id=cmid, expect=201)
    retry = await send(client, w.convo, w.a1, list(reversed(legs)), client_message_id=cmid, expect=200)
    assert retry.json() == first.json(), "same id and same created_at: nothing was re-stored"
    counts = await _row_counts()
    assert counts["messages"] == 1 and counts["legs"] == 2
    assert counts["outbox"] <= 1, "a retry must not enqueue a second delivery"


async def test_a_retry_with_different_legs_is_client_message_id_reused(
    client: AsyncClient, make_user: MakeUser
) -> None:
    w = await _world(client, make_user)
    cmid = str(uuid.uuid4())
    await send(client, w.convo, w.a1, w.required_from_a1(), client_message_id=cmid, expect=201)
    before = await _row_counts()

    variants = {
        "different ciphertext": [leg(w.a2), leg(w.b1, body=b"another body")],
        "different olm_type": [leg(w.a2), leg(w.b1, olm_type=1)],
        "fewer legs": [leg(w.b1)],
        "more legs": [leg(w.a2), leg(w.b1), leg(w.a1)],
    }
    for why, legs in variants.items():
        response = await send(client, w.convo, w.a1, legs, client_message_id=cmid)
        assert response.status_code == 409, why
        assert response.json() == {"detail": "client_message_id_reused"}, why
    assert await _row_counts() == before


async def test_client_message_id_is_scoped_to_the_sending_device_and_its_conversation(
    client: AsyncClient, make_user: MakeUser
) -> None:
    w = await _world(client, make_user)
    cmid = str(uuid.uuid4())
    await send(client, w.convo, w.a1, w.required_from_a1(), client_message_id=cmid, expect=201)

    # Another device may use the same id: the key is the PAIR.
    other = await send(client, w.convo, w.a2, [leg(w.a1), leg(w.b1)], client_message_id=cmid, expect=201)
    assert other.json()["sender_device_id"] == w.a2.id

    # The same device + id in a different conversation is a reuse, not a new message.
    carol = await make_user("Carol")
    c1 = Keyring(carol, 1)
    await register(client, c1)
    other_convo = await open_dm(client, w.alice, carol)
    response = await send(client, other_convo, w.a1, [leg(w.a2), leg(c1)], client_message_id=cmid)
    assert response.status_code == 409 and response.json()["detail"] == "client_message_id_reused"


async def test_a_retry_still_succeeds_after_the_device_set_changed(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """A lost response must not turn into a spurious device_list_mismatch on retry."""
    w = await _world(client, make_user)
    cmid = str(uuid.uuid4())
    first = await send(client, w.convo, w.a1, w.required_from_a1(), client_message_id=cmid, expect=201)
    a3 = Keyring(w.alice, 3)
    await register(client, a3)
    await approve(client, approver=w.a1, new=a3)  # the required set is now different
    retry = await send(client, w.convo, w.a1, w.required_from_a1(), client_message_id=cmid, expect=200)
    assert retry.json()["id"] == first.json()["id"]


async def test_concurrent_identical_sends_store_exactly_one_message(
    client: AsyncClient, make_user: MakeUser
) -> None:
    w = await _world(client, make_user)
    cmid = str(uuid.uuid4())
    legs = w.required_from_a1()
    responses = await asyncio.gather(*(
        send(client, w.convo, w.a1, legs, client_message_id=cmid) for _ in range(4)
    ))
    statuses = [r.status_code for r in responses]
    assert set(statuses) <= {200, 201} and statuses.count(201) == 1, statuses
    assert len({r.json()["id"] for r in responses}) == 1
    counts = await _row_counts()
    assert counts["messages"] == 1 and counts["legs"] == 2


# ---------------------------------------------------------------------------
# Limits and request validation
# ---------------------------------------------------------------------------


async def test_leg_and_request_limits(client: AsyncClient, make_user: MakeUser) -> None:
    w = await _world(client, make_user)

    async def post(legs: list[dict[str, Any]], **extra: Any) -> Any:
        body = {
            "sender_device_id": w.a1.id, "client_message_id": str(uuid.uuid4()), "legs": legs, **extra,
        }
        return await client.post(f"/v2/conversations/{w.convo}/messages", json=body, headers=w.alice.headers)

    oversized = {**leg(w.b1), "ciphertext_b64": "A" * 65_540}
    assert (await post([leg(w.a2), oversized])).status_code == 422
    just_over = {**leg(w.b1), "ciphertext_b64": "A" * 65_537}
    assert (await post([leg(w.a2), just_over])).status_code == 422
    biggest = {**leg(w.b1), "ciphertext_b64": "A" * 65_536}  # exactly the cap
    assert (await post([leg(w.a2), biggest])).status_code == 201

    assert (await post([])).status_code == 422
    many = [{**leg(w.b1), "recipient_device_id": str(uuid.uuid4())} for _ in range(MAX_V2_LEGS_PER_MESSAGE + 1)]
    assert (await post(many)).status_code == 422
    assert (await post([leg(w.a2), {**leg(w.b1), "olm_type": 2}])).status_code == 422
    assert (await post([leg(w.a2), {**leg(w.b1), "ciphertext_b64": ""}])).status_code == 422

    bad_base64 = await post([leg(w.a2), {**leg(w.b1), "ciphertext_b64": "not base64!"}])
    assert bad_base64.status_code == 422 and bad_base64.json()["detail"] == "invalid_base64"
    duplicate = await post([leg(w.a2), leg(w.b1), leg(w.b1)])
    assert duplicate.status_code == 422 and duplicate.json()["detail"] == "duplicate_leg_recipient"
    version = await post(w.required_from_a1(), envelope_version=2)
    assert version.status_code == 422 and version.json()["detail"] == "unsupported_envelope_version"
    bad_id = await client.post(
        f"/v2/conversations/{w.convo}/messages",
        json={"sender_device_id": w.a1.id, "client_message_id": "nope", "legs": w.required_from_a1()},
        headers=w.alice.headers,
    )
    assert bad_id.status_code == 422


async def test_a_dm_with_the_maximum_devices_on_both_sides_fits_the_leg_cap(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """5 devices each: the sender's 4 others + the recipient's 5 = 9 legs, within the cap of 10."""
    alice = await make_user("Alice")
    bob = await make_user("Bob")
    a_rings = [Keyring(alice, n) for n in range(1, 6)]
    b_rings = [Keyring(bob, n) for n in range(1, 6)]
    for rings in (a_rings, b_rings):
        await register(client, rings[0], one_time_keys=0)
        for ring in rings[1:]:
            await register(client, ring, one_time_keys=0)
            await approve(client, approver=rings[0], new=ring)
    convo = await open_dm(client, alice, bob)
    world = World(alice, bob, a_rings[0], a_rings[1], b_rings[0], convo)

    legs = [leg(r) for r in (*a_rings[1:], *b_rings)]
    assert len(legs) == 9 <= MAX_V2_LEGS_PER_MESSAGE
    await send(client, convo, a_rings[0], legs, expect=201)
    for ring in (*a_rings[1:], *b_rings):
        assert len(await _history(client, world, ring)) == 1


async def test_the_leg_cap_tracks_the_active_device_cap() -> None:
    from app.routers.keys import MAX_ACTIVE_DEVICES

    assert MAX_V2_LEGS_PER_MESSAGE == 2 * MAX_ACTIVE_DEVICES


# ---------------------------------------------------------------------------
# Who may send
# ---------------------------------------------------------------------------


async def test_sender_device_must_be_owned_approved_unrevoked_and_v2(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    w = await _world(client, make_user)
    legs = w.required_from_a1()

    # Bob using Alice's device id.
    response = await send(client, w.convo, w.a1, legs, user=w.bob)
    assert response.status_code == 403 and response.json()["detail"] == "not_your_device"
    ghost = Keyring(w.alice, 9)
    ghost.id = str(uuid.uuid4())
    response = await send(client, w.convo, ghost, legs)
    assert response.status_code == 403 and response.json()["detail"] == "not_your_device"
    # A legacy device.
    legacy = await register_device(w.alice)
    legacy_ring = Keyring(w.alice, 9)
    legacy_ring.id = legacy["id"]
    response = await send(client, w.convo, legacy_ring, legs)
    assert response.status_code == 403 and response.json()["detail"] == "device_suite_unsupported"
    # A revoked device.
    assert (await client.delete(f"/v2/devices/{w.a2.id}", headers=w.alice.headers)).status_code == 200
    response = await send(client, w.convo, w.a2, [leg(w.a1), leg(w.b1)])
    assert response.status_code == 403 and response.json()["detail"] == "device_revoked"
    assert await _row_counts() == {"messages": 0, "legs": 0, "outbox": 0}


async def test_only_active_members_may_send_and_only_to_dms(
    client: AsyncClient, make_user: MakeUser
) -> None:
    w = await _world(client, make_user)
    stranger = await make_user("Stranger")
    s1 = Keyring(stranger, 1)
    await register(client, s1)
    response = await send(client, w.convo, s1, [leg(w.a1), leg(w.a2), leg(w.b1)])
    assert response.status_code == 403 and response.json()["detail"] == "not_a_member"

    carol = await make_user("Carol")
    c1 = Keyring(carol, 1)
    await register(client, c1)
    await share_verified_campus(w.alice.id, w.bob.id, carol.id)
    group = await client.post(
        "/conversations",
        json={"kind": "group", "title": "Trio", "member_user_ids": [w.bob.id, carol.id]},
        headers=w.alice.headers,
    )
    assert group.status_code == 201, group.text
    response = await send(
        client, group.json()["id"], w.a1, [leg(w.a2), leg(w.b1), leg(c1)]
    )
    assert response.status_code == 409 and response.json()["detail"] == "e2ee_v2_requires_dm"
    assert await _row_counts() == {"messages": 0, "legs": 0, "outbox": 0}


async def test_blocks_behave_like_v1(client: AsyncClient, make_user: MakeUser) -> None:
    w = await _world(client, make_user)
    first = await send(client, w.convo, w.a1, w.required_from_a1(), expect=201)

    # Bob (named-)blocks Alice: his history hides her messages (c348); hers is unaffected.
    blocked = await client.post("/moderation/blocks", json={"blocked_id": w.alice.id}, headers=w.bob.headers)
    assert blocked.status_code == 201, blocked.text
    assert await _history(client, w, w.b1) == []
    # Alice can no longer reach anyone in this DM; nothing is stored.
    before = await _row_counts()
    response = await send(client, w.convo, w.a1, w.required_from_a1())
    assert response.status_code == 403 and response.json()["detail"] == "recipient_not_reachable"
    assert await _row_counts() == before
    assert first.json()["id"]


# ---------------------------------------------------------------------------
# v1 coexistence
# ---------------------------------------------------------------------------


async def test_v1_history_and_lookup_never_return_v2_messages_and_v1_still_works(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    w = await _world(client, make_user)
    v2_message = (await send(client, w.convo, w.a1, w.required_from_a1(), expect=201)).json()

    # Bob also has a LEGACY device and sends one v1 message into the same conversation.
    legacy = await register_device(w.bob)
    v1 = await client.post(
        f"/conversations/{w.convo}/messages",
        json={"sender_device_id": legacy["id"], "ciphertext_b64": b64(b"v1-bytes"), "message_type": "signal"},
        headers=w.bob.headers,
    )
    assert v1.status_code == 201, v1.text

    history = await client.get(f"/conversations/{w.convo}/messages", headers=w.alice.headers)
    assert history.status_code == 200, history.text
    assert [m["id"] for m in history.json()] == [v1.json()["id"]]
    assert base64.b64decode(history.json()[0]["ciphertext_b64"]) == b"v1-bytes"
    lookup = await client.get(
        f"/conversations/{w.convo}/messages/by-id",
        params={"ids": f"{v2_message['id']},{v1.json()['id']}"}, headers=w.alice.headers,
    )
    assert lookup.status_code == 200 and [m["id"] for m in lookup.json()] == [v1.json()["id"]]
    # And the v2 history shows only the v2 message.
    assert [m["id"] for m in await _history(client, w, w.b1)] == [v2_message["id"]]


async def test_a_v2_device_cannot_post_a_v1_message(client: AsyncClient, make_user: MakeUser) -> None:
    w = await _world(client, make_user)
    response = await client.post(
        f"/conversations/{w.convo}/messages",
        json={"sender_device_id": w.a1.id, "ciphertext_b64": b64(b"opaque"), "message_type": "signal"},
        headers=w.alice.headers,
    )
    assert response.status_code == 403 and response.json()["detail"] == "device_requires_v2"
    assert await _row_counts() == {"messages": 0, "legs": 0, "outbox": 0}


async def test_receipts_are_per_device_for_v2_messages(client: AsyncClient, make_user: MakeUser) -> None:
    w = await _world(client, make_user)
    message = (await send(client, w.convo, w.a1, w.required_from_a1(), expect=201)).json()
    for ring, user in ((w.b1, w.bob), (w.a2, w.alice)):
        response = await client.post(
            f"/messages/{message['id']}/receipts", json={"device_id": ring.id}, headers=user.headers
        )
        assert response.status_code == 200, response.text
        assert response.json()["device_id"] == ring.id
    async with get_session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(models.MessageReceipt)) == 2


# ---------------------------------------------------------------------------
# Delivery outbox and WebSocket hints
# ---------------------------------------------------------------------------


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    published: list[tuple[str, dict[str, Any]]] = []

    async def publish(user_id: str, event: dict[str, Any]) -> None:
        published.append((user_id, event))

    async def push(user_id: str, title: str) -> None:
        return None

    import app.routers.messages as messages_router

    monkeypatch.setattr(messages_router, "publish_to_user", publish)
    monkeypatch.setattr(messages_router, "send_content_free_push", push)
    return published


async def test_live_ws_hint_fires_for_every_member_and_carries_no_ciphertext(
    client: AsyncClient, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = await _world(client, make_user)
    published = _capture(monkeypatch)
    response = await send(
        client, w.convo, w.a1, [leg(w.a2, body=b"SECRET-A2"), leg(w.b1, body=b"SECRET-B1")], expect=201
    )
    message = response.json()

    assert sorted(user for user, _ in published) == sorted([w.alice.id, w.bob.id])
    for _, published_event in published:
        # One dict is shared by every recipient; copy before editing it.
        event = dict(published_event)
        # The event uses isoformat() and the response uses "Z": compare as instants.
        created_at = event.pop("created_at")
        assert datetime.fromisoformat(created_at) == datetime.fromisoformat(message["created_at"])
        assert event == {
            "type": "message",
            "conversation_id": w.convo,
            "message_id": message["id"],
            "sender_device_id": w.a1.id,
            "envelope_version": 1,
        }
        assert "ciphertext" not in event
        assert b64(b"SECRET-A2") not in json.dumps(event) and b64(b"SECRET-B1") not in json.dumps(event)
    # Delivered, so the outbox row is gone; and it never held ciphertext to begin with.
    assert (await _row_counts())["outbox"] == 0


async def test_the_sweeper_rebuilds_a_v2_event_without_ciphertext_and_does_not_crash(
    client: AsyncClient, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    """message.ciphertext is NULL on a v2 row; the sweeper used to b64encode it."""
    w = await _world(client, make_user)

    async def crashed_dispatch(*args: Any, **kwargs: Any) -> None:
        return None  # leave the row pending, as if the process died right after commit

    monkeypatch.setattr(outbox, "dispatch_now", crashed_dispatch)
    message = (await send(
        client, w.convo, w.a1, [leg(w.a2, body=b"SECRET-A2"), leg(w.b1, body=b"SECRET-B1")], expect=201
    )).json()

    async with get_session_factory()() as session:
        payload = (await session.execute(text("SELECT payload FROM delivery_outbox"))).scalar_one()
    assert "ciphertext" not in payload
    assert b64(b"SECRET-A2") not in json.dumps(payload)

    published = _capture(monkeypatch)
    stats = await outbox.dispatch_pending(limit=10)
    assert (stats.claimed, stats.delivered, stats.dead) == (1, 1, 0)
    assert sorted(user for user, _ in published) == sorted([w.alice.id, w.bob.id])
    for _, event in published:
        assert event["message_id"] == message["id"] and event["envelope_version"] == 1
        assert "ciphertext" not in event
    assert (await _row_counts())["outbox"] == 0


async def test_message_legs_and_outbox_row_commit_in_one_transaction(
    client: AsyncClient, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = await _world(client, make_user)

    async def raising_enqueue(*args: Any, **kwargs: Any) -> uuid.UUID:
        raise RuntimeError("c444 simulated enqueue failure")

    monkeypatch.setattr(outbox, "enqueue", raising_enqueue)
    with pytest.raises(RuntimeError, match="c444 simulated enqueue failure"):
        await send(client, w.convo, w.a1, w.required_from_a1())
    assert await _row_counts() == {"messages": 0, "legs": 0, "outbox": 0}, (
        "the message and its legs must roll back with the failed delivery intent"
    )
