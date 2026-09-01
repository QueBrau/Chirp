"""c264: caller-supplied request lists are bounded, so one call cannot write unbounded rows.

Found by the c263 abuse sweep. Five request-body list fields had no upper bound while
their routes insert ONE ROW PER ELEMENT, which let a single caller decide how much work
one request costs. Rate limiting does not close this and is not a substitute: it caps
how OFTEN a caller may post, not how many rows a single permitted post inserts.

Each cap is proven BOTH WAYS - the over-limit payload is refused, and the largest
REALISTIC payload still succeeds - because a cap that quietly rejects real use is a
worse bug than the one it fixes. The realistic cases are the point of this file: a full
100-key registration batch (SPEC 6.1) and a whole-chapter invite must keep working.
"""

from __future__ import annotations

import uuid
from typing import Any

from httpx import AsyncClient

from app.schemas.e2ee import MAX_PREKEY_BATCH
from tests.conftest import MakeChapterWith, MakeUser, b64

MAX_INVITE_IDS = 500


def _otks(count: int) -> list[dict[str, Any]]:
    return [
        {"key_id": key_id, "public_key_b64": b64(f"otk-{key_id}".encode())}
        for key_id in range(1, count + 1)
    ]


def _kyber(count: int) -> list[dict[str, Any]]:
    return [
        {
            "key_id": key_id,
            "public_key_b64": b64(f"kyber-{key_id}".encode()),
            "signature_b64": b64(f"kyber-sig-{key_id}".encode()),
        }
        for key_id in range(1, count + 1)
    ]


def _device_body(**extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "device_label": "pytest-c264",
        "registration_id": 7264,
        "identity_key_b64": b64(b"identity-" + uuid.uuid4().bytes),
        "signed_prekey": {
            "key_id": 1,
            "public_key_b64": b64(b"spk-" + uuid.uuid4().bytes),
            "signature_b64": b64(b"sig-" + uuid.uuid4().bytes),
        },
        "one_time_prekeys": _otks(2),
    }
    body.update(extra)
    return body


# ---------------------------------------------------------------------------
# POST /devices
# ---------------------------------------------------------------------------


async def test_device_registration_refuses_an_oversized_otk_batch(
    client: AsyncClient, make_user: MakeUser
) -> None:
    user = await make_user()
    response = await client.post(
        "/devices", json=_device_body(one_time_prekeys=_otks(MAX_PREKEY_BATCH + 1)), headers=user.headers
    )
    assert response.status_code == 422, response.text


async def test_device_registration_refuses_an_oversized_kyber_batch(
    client: AsyncClient, make_user: MakeUser
) -> None:
    user = await make_user()
    response = await client.post(
        "/devices", json=_device_body(kyber_one_time=_kyber(MAX_PREKEY_BATCH + 1)), headers=user.headers
    )
    assert response.status_code == 422, response.text


async def test_device_registration_accepts_the_real_100_key_batch(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """SPEC 6.1's registration batch. The cap must never be what stops a device registering."""
    user = await make_user()
    response = await client.post(
        "/devices", json=_device_body(one_time_prekeys=_otks(100), kyber_one_time=_kyber(100)),
        headers=user.headers,
    )
    assert response.status_code == 201, response.text


async def test_device_registration_accepts_exactly_the_cap(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """max_length is inclusive - the boundary itself is legal, not one short of it."""
    user = await make_user()
    response = await client.post(
        "/devices", json=_device_body(one_time_prekeys=_otks(MAX_PREKEY_BATCH)), headers=user.headers
    )
    assert response.status_code == 201, response.text


# ---------------------------------------------------------------------------
# POST /devices/{device_id}/prekeys
# ---------------------------------------------------------------------------


async def test_replenish_refuses_an_oversized_otk_batch(
    client: AsyncClient, make_user: MakeUser
) -> None:
    user = await make_user()
    device = await client.post("/devices", json=_device_body(), headers=user.headers)
    assert device.status_code == 201, device.text
    response = await client.post(
        f"/devices/{device.json()['id']}/prekeys",
        json={"one_time_prekeys": _otks(MAX_PREKEY_BATCH + 1)},
        headers=user.headers,
    )
    assert response.status_code == 422, response.text


async def test_replenish_refuses_an_oversized_kyber_batch(
    client: AsyncClient, make_user: MakeUser
) -> None:
    user = await make_user()
    device = await client.post("/devices", json=_device_body(), headers=user.headers)
    assert device.status_code == 201, device.text
    response = await client.post(
        f"/devices/{device.json()['id']}/prekeys",
        json={"one_time_prekeys": [], "kyber_one_time": _kyber(MAX_PREKEY_BATCH + 1)},
        headers=user.headers,
    )
    assert response.status_code == 422, response.text


async def test_replenish_accepts_a_real_full_top_up(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """The client tops up to INITIAL_ONE_TIME_PREKEY_COUNT = 100 when it runs low."""
    user = await make_user()
    device = await client.post("/devices", json=_device_body(), headers=user.headers)
    assert device.status_code == 201, device.text
    response = await client.post(
        f"/devices/{device.json()['id']}/prekeys",
        json={"one_time_prekeys": _otks(100)},
        headers=user.headers,
    )
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# POST /events/{event_id}/invites
# ---------------------------------------------------------------------------


async def test_event_invites_refuse_an_oversized_id_list(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member")
    created = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json={
            "title": "Cap test",
            "starts_at": "2026-09-27T19:00:00Z",
            "location": "Chapter House",
            "cover_url": "https://picsum.photos/seed/c264/800/600",
        },
        headers=setup.president.headers,
    )
    assert created.status_code == 201, created.text

    response = await client.post(
        f"/events/{created.json()['id']}/invites",
        json={"user_ids": [str(uuid.uuid4()) for _ in range(MAX_INVITE_IDS + 1)]},
        headers=setup.president.headers,
    )
    assert response.status_code == 422, response.text

    # ASSERTING 422 ALONE WOULD BE VACUOUS HERE, and the falsification pass proved it:
    # with the cap removed this test still passed, because 501 unrecognised ids get a
    # 422 from the route's own `unknown_user_in_invite_list` check anyway. The status
    # code cannot tell the two refusals apart, so the SHAPE has to. A cap rejection is
    # pydantic's list of validation errors with type "too_long"; the route's rejection
    # is a plain string detail. Only the former proves the request was stopped before
    # the handler ran.
    detail = response.json()["detail"]
    assert isinstance(detail, list), (
        f"expected a pydantic validation error, got the route's own refusal: {detail!r} "
        "- that means the length cap did not fire"
    )
    assert any(err.get("type") == "too_long" for err in detail), detail


async def test_a_roster_sized_invite_list_gets_past_the_cap(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """A list AT the cap must be refused by the route's own rules, never by the cap.

    Seeding 500 real members to get a 201 would cost 500 bootstraps for one assertion,
    so this proves the same thing more precisely: send exactly MAX_INVITE_IDS and show
    the refusal is `unknown_user_in_invite_list` - the route's semantic check, which
    only runs AFTER validation accepted the length. A cap set too low would produce
    pydantic's list-shaped 422 here instead, and never reach that check.

    (The route rejects unrecognised ids outright rather than filtering them, which is
    the stricter of the two behaviours and worth stating: an invite list is not
    silently trimmed.)
    """
    setup = await make_chapter_with("member")
    created = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json={
            "title": "Roster invite",
            "starts_at": "2026-09-27T19:00:00Z",
            "location": "Chapter House",
            "cover_url": "https://picsum.photos/seed/c264/800/600",
        },
        headers=setup.president.headers,
    )
    assert created.status_code == 201, created.text

    at_cap = await client.post(
        f"/events/{created.json()['id']}/invites",
        json={"user_ids": [setup.member.id] + [str(uuid.uuid4()) for _ in range(MAX_INVITE_IDS - 1)]},
        headers=setup.president.headers,
    )
    assert at_cap.status_code == 422, at_cap.text
    assert at_cap.json() == {"detail": "unknown_user_in_invite_list"}, (
        "a roster-sized list must reach the route's own membership check - a "
        "list-shaped pydantic error here would mean the cap itself refused it"
    )


async def test_a_real_small_invite_still_succeeds(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The ordinary case, so the cap work cannot have broken inviting at all."""
    setup = await make_chapter_with("member")
    created = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json={
            "title": "Small invite",
            "starts_at": "2026-09-27T19:00:00Z",
            "location": "Chapter House",
            "cover_url": "https://picsum.photos/seed/c264b/800/600",
        },
        headers=setup.president.headers,
    )
    assert created.status_code == 201, created.text

    response = await client.post(
        f"/events/{created.json()['id']}/invites",
        json={"user_ids": [setup.member.id]},
        headers=setup.president.headers,
    )
    assert response.status_code == 201, response.text
