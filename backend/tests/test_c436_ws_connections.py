"""PostgreSQL proofs for bounded WebSocket admission slots and deletion drain."""
from __future__ import annotations

import asyncio
import dataclasses
import uuid

import pytest
from sqlalchemy import func, select, text

from app import models
from app.db import get_session_factory
from app.services.account_deletion_hold import (
    DeletionHoldActive,
    DeletionHoldInvalid,
    activate_deletion_hold,
    advisory_key,
    _lock_exclusive,
)
from app.services.account_external_operations import inspect_registered_operation_drain
from app.services.account_fulfillment import _lock_executor_finalization
from app.services.account_ws_connections import (
    MAX_WS_CONNECTIONS_PER_ACCOUNT,
    WsConnectionLimit,
    finish_ws_connection,
    register_ws_connection,
)


async def _deletion_request(client, owner) -> uuid.UUID:
    response = await client.post(
        "/me/data-requests", json={"kind": "deletion"}, headers=owner.headers,
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


async def _activate(user_id: uuid.UUID, request_id: uuid.UUID) -> None:
    async with get_session_factory()() as session:
        await activate_deletion_hold(
            session, user_id=user_id, request_id=request_id,
        )
        await session.commit()


async def _register(owner):
    return await register_ws_connection(
        user_id=uuid.UUID(owner.id), firebase_uid=owner.firebase_uid,
    )


async def _rows(user_id: uuid.UUID):
    async with get_session_factory()() as session:
        return list((await session.scalars(
            select(models.AccountWsConnection)
            .where(models.AccountWsConnection.user_id == user_id)
            .order_by(models.AccountWsConnection.slot)
        )).all())


@pytest.mark.asyncio
async def test_concurrent_registrations_are_bounded_fenced_and_reuse_released_slot(
    client, make_user,
):
    owner = await make_user("Bounded WebSocket owner")
    user_id = uuid.UUID(owner.id)

    outcomes = await asyncio.gather(*(
        _register(owner) for _ in range(MAX_WS_CONNECTIONS_PER_ACCOUNT + 1)
    ), return_exceptions=True)
    handles = [value for value in outcomes if not isinstance(value, BaseException)]
    limits = [value for value in outcomes if isinstance(value, WsConnectionLimit)]
    assert all(
        not isinstance(value, BaseException) or isinstance(value, WsConnectionLimit)
        for value in outcomes
    ), outcomes
    assert len(handles) == MAX_WS_CONNECTIONS_PER_ACCOUNT
    assert len(limits) == 1
    assert len({handle.slot for handle in handles}) == MAX_WS_CONNECTIONS_PER_ACCOUNT
    assert len({handle.connection_id for handle in handles}) == MAX_WS_CONNECTIONS_PER_ACCOUNT
    assert {row.slot for row in await _rows(user_id)} == set(range(MAX_WS_CONNECTIONS_PER_ACCOUNT))

    released = handles[0]
    await finish_ws_connection(released, state="finished")
    replacement = await _register(owner)
    assert replacement.slot == released.slot
    assert replacement.connection_id != released.connection_id
    assert replacement.owner_token != released.owner_token

    # Replaying an old handle is harmless and cannot remove the replacement row.
    await finish_ws_connection(released, state="finished")
    rows = await _rows(user_id)
    assert len(rows) == MAX_WS_CONNECTIONS_PER_ACCOUNT
    assert any(
        row.id == replacement.connection_id
        and row.owner_token == replacement.owner_token
        and row.slot == replacement.slot
        for row in rows
    )

    for forged in (
        dataclasses.replace(replacement, owner_token=uuid.uuid4()),
        dataclasses.replace(replacement, user_id=uuid.uuid4()),
        dataclasses.replace(replacement, slot=(replacement.slot + 1) % MAX_WS_CONNECTIONS_PER_ACCOUNT),
    ):
        with pytest.raises(DeletionHoldInvalid, match="websocket_connection_fence_invalid"):
            await finish_ws_connection(forged, state="finished")
    assert any(row.id == replacement.connection_id for row in await _rows(user_id))


@pytest.mark.asyncio
async def test_uncertain_connections_are_not_released_or_reused(client, make_user):
    owner = await make_user("Uncertain WebSocket owner")
    user_id = uuid.UUID(owner.id)
    uncertain = await _register(owner)
    await finish_ws_connection(uncertain, state="uncertain")

    with pytest.raises(DeletionHoldInvalid, match="websocket_connection_uncertain"):
        await finish_ws_connection(uncertain, state="finished")
    next_handle = await _register(owner)
    assert next_handle.slot != uncertain.slot
    rows = await _rows(user_id)
    assert len(rows) == 2
    assert rows[uncertain.slot].state == "uncertain"
    assert rows[next_handle.slot].state == "running"


@pytest.mark.asyncio
async def test_active_hold_rejects_new_ws_registration_and_both_drains_count_existing(
    client, make_user,
):
    owner = await make_user("Held WebSocket owner")
    user_id = uuid.UUID(owner.id)
    request_id = await _deletion_request(client, owner)
    handle = await _register(owner)
    await _activate(user_id, request_id)

    with pytest.raises(DeletionHoldActive):
        await _register(owner)

    drain = await inspect_registered_operation_drain(
        user_id=user_id, request_id=request_id,
    )
    assert (drain.running_count, drain.uncertain_count) == (1, 0)
    assert not drain.registered_operations_drained

    async with get_session_factory()() as session:
        assert not await _lock_executor_finalization(session, user_id, request_id)

    await finish_ws_connection(handle, state="uncertain")
    drain = await inspect_registered_operation_drain(
        user_id=user_id, request_id=request_id,
    )
    assert (drain.running_count, drain.uncertain_count) == (0, 1)
    assert not drain.registered_operations_drained
    async with get_session_factory()() as session:
        assert not await _lock_executor_finalization(session, user_id, request_id)


@pytest.mark.asyncio
async def test_registration_and_hold_activation_serialize_on_real_pg_advisory_gate(
    client, make_user,
):
    owner = await make_user("WebSocket admission race owner")
    user_id = uuid.UUID(owner.id)
    request_id = await _deletion_request(client, owner)
    key = advisory_key(user_id) & ((1 << 64) - 1)
    lock_class, lock_object = key >> 32, key & 0xFFFFFFFF

    async with get_session_factory()() as blocker:
        await _lock_exclusive(blocker, user_id)
        registration = asyncio.create_task(_register(owner))
        activation = asyncio.create_task(_activate(user_id, request_id))

        # Observe both real PostgreSQL advisory-lock waiters before releasing the
        # blocker; this makes the race evidence-based instead of sleep-order based.
        waiters = 0
        async with get_session_factory()() as observer:
            for _ in range(400):
                waiters = await observer.scalar(text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                    "AND NOT granted AND classid = :lock_class "
                    "AND objid = :lock_object AND objsubid = 1"
                ), {"lock_class": lock_class, "lock_object": lock_object})
                if waiters >= 2:
                    break
                await asyncio.sleep(0.025)
            assert waiters >= 2, "registration and activation did not both wait on the account gate"
        await blocker.commit()

        registration_result, activation_result = await asyncio.gather(
            registration, activation, return_exceptions=True,
        )

    assert activation_result is None
    if isinstance(registration_result, DeletionHoldActive):
        # Hold acquired the exclusive gate first; registration saw the committed hold.
        assert await _rows(user_id) == []
        drain = await inspect_registered_operation_drain(
            user_id=user_id, request_id=request_id,
        )
        assert drain.registered_operations_drained
    else:
        # Registration acquired the gate first; its durable running row is visible
        # under the exact hold once activation commits and must block both drains.
        assert not isinstance(registration_result, BaseException), registration_result
        handle = registration_result
        assert handle.user_id == user_id
        drain = await inspect_registered_operation_drain(
            user_id=user_id, request_id=request_id,
        )
        assert drain.running_count == 1 and not drain.registered_operations_drained
        async with get_session_factory()() as session:
            assert not await _lock_executor_finalization(session, user_id, request_id)

