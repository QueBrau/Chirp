"""Board c85: campus is server-owned, not client-asserted.

The hole these guard: POST /auth/bootstrap used to write body.campus_id straight to
users.campus_id with no check, while the campus feed's guard is
`user.campus_id != campus_id -> 403` — a comparison against a value the caller chose.
That enforces consistency, not identity, so a direct API call could claim any
university and then read its feed.

These tests are written against the ATTACK, not the implementation: the first one
posts a campus_id the way an attacker would and asserts it does not stick. Deleting
the field from the schema is one way to pass it; validating it server-side would be
another. Either is fine, and the test stays meaningful under both.
"""
from __future__ import annotations

import uuid

from httpx import AsyncClient

async def _bootstrap(client: AsyncClient, uid: str, extra: dict | None = None):
    """Same idiom as conftest's make_user factory, plus an optional attacker-supplied field."""
    body = {
        "email": f"{uid}@example.edu",
        "display_name": "Test Person",
        "account_type": "non_greek",
    }
    if extra:
        body.update(extra)
    return await client.post(
        "/auth/bootstrap", json=body, headers={"X-Debug-Firebase-Uid": uid}
    )


async def test_bootstrap_ignores_a_client_supplied_campus_id(client: AsyncClient) -> None:
    """The attack, exactly: claim a campus at signup and see if it sticks."""
    claimed = str(uuid.uuid4())
    resp = await _bootstrap(client, "c85-claimer", {"campus_id": claimed})

    assert resp.status_code == 201, resp.text
    body = resp.json()
    # Not merely "different from claimed" — absent. A user who has verified nothing
    # has no campus, which is the correct state rather than an edge case.
    assert body.get("campus_id") is None, (
        f"bootstrap accepted a client-supplied campus_id: {body.get('campus_id')}"
    )


async def test_a_fresh_account_has_no_campus_at_all(client: AsyncClient) -> None:
    """Without the attack either — the default must be no campus, not some campus."""
    resp = await _bootstrap(client, "c85-plain")
    assert resp.status_code == 201, resp.text
    assert resp.json().get("campus_id") is None


async def test_campus_id_is_not_an_accepted_field_on_the_schema(client: AsyncClient) -> None:
    """Sending it must not 422 either.

    _Schema does not set extra="forbid", so an older client that still includes
    campus_id keeps working and has the value ignored. If someone later turns on
    strict extras, this test fails and forces the mobile client to be updated in the
    same change rather than discovering it as a signup outage.
    """
    resp = await _bootstrap(client, "c85-oldclient", {"campus_id": str(uuid.uuid4())})
    assert resp.status_code == 201, (
        "an old client sending campus_id must not be rejected: " + resp.text
    )
