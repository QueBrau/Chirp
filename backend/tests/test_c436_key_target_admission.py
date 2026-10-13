"""Target-account admission for key-consuming endpoints."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text

from app import models
from app.config import get_settings
from app.db import get_session_factory
from app.services.account_deletion_hold import activate_deletion_hold
from tests.conftest import MakeUser, RegisterDevice, share_verified_campus
from tests.e2ee_v2_helpers import Keyring, open_dm, register


def _set_admission(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setenv("ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED", str(enabled).lower())
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clear_settings() -> None:
    yield
    get_settings.cache_clear()


async def _hold(user_id: str) -> None:
    async with get_session_factory()() as session:
        request = models.AccountDataRequest(
            user_id=uuid.UUID(user_id), kind="deletion", open_key="target-key"
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


async def _key_snapshot(device_id: str) -> list[tuple[uuid.UUID, object]]:
    async with get_session_factory()() as session:
        return list(
            (
                await session.execute(
                    select(models.OneTimePrekey.id, models.OneTimePrekey.consumed_at)
                    .where(models.OneTimePrekey.device_id == uuid.UUID(device_id))
                    .order_by(models.OneTimePrekey.id)
                )
            ).tuples()
        )


@pytest.mark.asyncio
async def test_v1_bundle_target_hold_blocks_before_consumption(
    client, make_user: MakeUser, register_device: RegisterDevice, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission(monkeypatch, True)
    caller = await make_user("bundle caller")
    target = await make_user("bundle target")
    device = await register_device(target)
    before = await _key_snapshot(device["id"])
    assert before
    await _hold(target.id)
    response = await client.get(
        f"/users/{target.id}/prekey-bundle", headers=caller.headers
    )
    assert response.status_code == 423, response.text
    assert await _key_snapshot(device["id"]) == before


@pytest.mark.asyncio
async def test_v2_claim_actor_and_target_holds_block_without_consumption(
    client, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission(monkeypatch, True)
    caller = await make_user("claim caller")
    target = await make_user("claim target")
    await share_verified_campus(caller.id, target.id)
    await open_dm(client, caller, target)
    ring = Keyring(target, 1)
    await register(client, ring)
    before = await _key_snapshot(ring.id)
    assert before
    await _hold(target.id)
    blocked_target = await client.post(
        "/v2/keys/claim",
        json={"device_ids": [ring.id]},
        headers=caller.headers,
    )
    assert blocked_target.status_code == 423, blocked_target.text

    caller2 = await make_user("claim actor held")
    target2 = await make_user("claim actor target")
    await share_verified_campus(caller2.id, target2.id)
    await open_dm(client, caller2, target2)
    ring2 = Keyring(target2, 1)
    await register(client, ring2)
    before2 = await _key_snapshot(ring2.id)
    assert before2
    await _hold(caller2.id)
    blocked_actor = await client.post(
        "/v2/keys/claim",
        json={"device_ids": [ring2.id]},
        headers=caller2.headers,
    )
    assert blocked_actor.status_code == 423, blocked_actor.text
    assert await _key_snapshot(ring.id) == before
    assert await _key_snapshot(ring2.id) == before2


@pytest.mark.asyncio
async def test_unrelated_target_hold_allows_v2_claim_and_flag_off_preserves_behavior(
    client, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission(monkeypatch, True)
    caller = await make_user("claim allowed caller")
    target = await make_user("claim allowed target")
    unrelated = await make_user("claim unrelated")
    stranger = await make_user("held stranger")
    await share_verified_campus(caller.id, target.id)
    await open_dm(client, caller, target)
    ring = Keyring(target, 1)
    await register(client, ring)
    await _hold(unrelated.id)
    allowed = await client.post(
        "/v2/keys/claim", json={"device_ids": [ring.id]}, headers=caller.headers
    )
    assert allowed.status_code == 200, allowed.text

    await _hold(stranger.id)
    _set_admission(monkeypatch, False)
    from app.db import get_engine
    from sqlalchemy import event
    statements: list[str] = []

    def record(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    event.listen(get_engine().sync_engine, "before_cursor_execute", record)
    legacy_behavior = await client.post(
        "/v2/keys/claim", json={"device_ids": [ring.id]}, headers=caller.headers
    )
    event.remove(get_engine().sync_engine, "before_cursor_execute", record)
    assert legacy_behavior.status_code == 200, legacy_behavior.text
    assert not any("account_deletion_holds" in statement for statement in statements)


@pytest.mark.asyncio
async def test_v2_unknown_or_stranger_device_has_same_403_before_hold_state(
    client, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission(monkeypatch, True)
    caller = await make_user("claim privacy caller")
    stranger = await make_user("claim privacy stranger")
    ring = Keyring(stranger, 1)
    await register(client, ring)
    await _hold(stranger.id)
    stranger_attempt = await client.post(
        "/v2/keys/claim", json={"device_ids": [ring.id]}, headers=caller.headers
    )
    unknown_attempt = await client.post(
        "/v2/keys/claim", json={"device_ids": [str(uuid.uuid4())]}, headers=caller.headers
    )
    assert stranger_attempt.status_code == unknown_attempt.status_code == 403
    assert stranger_attempt.json() == unknown_attempt.json() == {
        "detail": "recipient_not_reachable"
    }


@pytest.mark.asyncio
async def test_claim_rejects_target_owner_change_between_preflight_and_handler(
    client, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission(monkeypatch, True)
    caller = await make_user("race caller")
    target = await make_user("race target")
    await share_verified_campus(caller.id, target.id)
    await open_dm(client, caller, target)
    ring = Keyring(target, 1)
    await register(client, ring)
    before = await _key_snapshot(ring.id)
    from app.routers import keys_v2

    original = keys_v2._claim_owners
    calls = 0

    async def move_after_preflight(session, *, actor_id, ids):
        nonlocal calls
        owners = await original(session, actor_id=actor_id, ids=ids)
        calls += 1
        if calls == 1:
            async with get_session_factory()() as move_session:
                await move_session.execute(
                    text("UPDATE devices SET user_id = :actor WHERE id = :device"),
                    {"actor": uuid.UUID(caller.id), "device": uuid.UUID(ring.id)},
                )
                await move_session.commit()
        return owners

    monkeypatch.setattr(keys_v2, "_claim_owners", move_after_preflight)
    response = await client.post(
        "/v2/keys/claim", json={"device_ids": [ring.id]}, headers=caller.headers
    )
    assert response.status_code == 503, response.text
    assert calls == 2
    assert await _key_snapshot(ring.id) == before


@pytest.mark.asyncio
async def test_claim_rejects_owner_change_during_handler_recheck_without_consumption(
    client, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission(monkeypatch, True)
    caller = await make_user("handler race caller")
    target = await make_user("handler race target")
    await share_verified_campus(caller.id, target.id)
    await open_dm(client, caller, target)
    ring = Keyring(target, 1)
    await register(client, ring)
    before = await _key_snapshot(ring.id)
    from app.routers import keys_v2

    original = keys_v2._claim_owners
    calls = 0

    async def move_during_handler(session, *, actor_id, ids):
        nonlocal calls
        owners = await original(session, actor_id=actor_id, ids=ids)
        calls += 1
        if calls == 2:
            async with get_session_factory()() as move_session:
                await move_session.execute(
                    text("UPDATE devices SET user_id = :actor WHERE id = :device"),
                    {"actor": uuid.UUID(caller.id), "device": uuid.UUID(ring.id)},
                )
                await move_session.commit()
        return owners

    monkeypatch.setattr(keys_v2, "_claim_owners", move_during_handler)
    response = await client.post(
        "/v2/keys/claim", json={"device_ids": [ring.id]}, headers=caller.headers
    )
    assert response.status_code == 503, response.text
    assert calls == 2
    assert await _key_snapshot(ring.id) == before


@pytest.mark.asyncio
async def test_claim_openapi_body_contract_is_device_id_list(client) -> None:
    document = (await client.get("/openapi.json")).json()
    schema_ref = document["paths"]["/v2/keys/claim"]["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    schema = document["components"]["schemas"][schema_ref.rsplit("/", 1)[-1]]
    assert schema["required"] == ["device_ids"]
    assert schema["properties"]["device_ids"]["type"] == "array"
