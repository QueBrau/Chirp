"""Real-PostgreSQL admission and fencing tests for external provider operations."""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app import models
from app.db import get_engine, get_session_factory
from app.services.account_deletion_hold import DeletionHoldActive, DeletionHoldInvalid, activate_deletion_hold
from app.services.account_external_operations import (
    ExternalOperationConflict,
    finish_external_operation,
    inspect_registered_operation_drain,
    register_external_operation,
)


def _effect(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


async def _deletion_request(client, owner) -> str:
    response = await client.post(
        "/me/data-requests", json={"kind": "deletion"}, headers=owner.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _activate(owner_id: str, request_id: str) -> None:
    async with get_session_factory()() as session:
        await activate_deletion_hold(
            session, user_id=uuid.UUID(owner_id), request_id=uuid.UUID(request_id),
        )
        await session.commit()


async def _register(owner, effect: str, *, kind: str = "media_finalize_post", targets=()):
    return await register_external_operation(
        user_id=uuid.UUID(owner.id), firebase_uid=owner.firebase_uid,
        kind=kind, effect_key=_effect(effect), target_user_ids=targets,
    )


async def _counts(operation_id: uuid.UUID) -> tuple[str, int]:
    async with get_session_factory()() as session:
        state = await session.scalar(select(models.AccountExternalOperation.state).where(
            models.AccountExternalOperation.id == operation_id,
        ))
        scopes = await session.scalar(select(func.count()).select_from(models.AccountExternalOperationScope).where(
            models.AccountExternalOperationScope.operation_id == operation_id,
        ))
        return state, scopes


@pytest.mark.asyncio
async def test_registration_commits_before_hold_and_drain_waits_for_owner_fenced_finish(
    client, make_user,
):
    actor = await make_user("External operation actor")
    request_id = await _deletion_request(client, actor)
    handle = await _register(actor, "race-finished")
    await _activate(actor.id, request_id)

    with pytest.raises(DeletionHoldActive):
        await _register(actor, "fresh-work-after-hold")

    draining = await inspect_registered_operation_drain(
        user_id=uuid.UUID(actor.id), request_id=uuid.UUID(request_id),
    )
    assert (draining.running_count, draining.uncertain_count) == (1, 0)
    assert draining.registered_operations_drained is False

    await finish_external_operation(handle, state="finished")
    # Completion is allowed after activation, but only for the exact immutable owner handle.
    await finish_external_operation(handle, state="finished")
    drained = await inspect_registered_operation_drain(
        user_id=uuid.UUID(actor.id), request_id=uuid.UUID(request_id),
    )
    assert (drained.running_count, drained.uncertain_count) == (0, 0)
    assert drained.registered_operations_drained is True
    assert await _counts(handle.operation_id) == ("finished", 1)


@pytest.mark.asyncio
async def test_multiaccount_scope_blocks_when_any_bound_account_is_held(client, make_user):
    actor = await make_user("Multi-account operation actor")
    target = await make_user("Multi-account operation target")
    handle = await _register(actor, "multi-scope", targets=[target.id, actor.id, target.id])
    assert handle.user_ids == tuple(sorted((uuid.UUID(actor.id), uuid.UUID(target.id)), key=str))

    request_id = await _deletion_request(client, target)
    await _activate(target.id, request_id)
    with pytest.raises(DeletionHoldActive):
        await _register(actor, "new-work-on-held-target", targets=[target.id])

    # The admitted operation retains its target link and must drain under that target's hold.
    drain = await inspect_registered_operation_drain(
        user_id=uuid.UUID(target.id), request_id=uuid.UUID(request_id),
    )
    assert drain.running_count == 1
    await finish_external_operation(handle, state="finished")
    drain = await inspect_registered_operation_drain(
        user_id=uuid.UUID(target.id), request_id=uuid.UUID(request_id),
    )
    assert drain.registered_operations_drained is True


@pytest.mark.asyncio
async def test_duplicate_effect_key_is_single_winner_and_never_reclaimed(client, make_user):
    owner = await make_user("Concurrent effect-key owner")
    key = _effect("one-logical-provider-effect")

    async def register_one():
        return await register_external_operation(
            user_id=uuid.UUID(owner.id), firebase_uid=owner.firebase_uid,
            kind="media_finalize_avatar", effect_key=key,
        )

    outcomes = await asyncio.gather(register_one(), register_one(), return_exceptions=True)
    handles = [value for value in outcomes if not isinstance(value, BaseException)]
    conflicts = [value for value in outcomes if isinstance(value, ExternalOperationConflict)]
    assert len(handles) == 1
    assert len(conflicts) == 1
    await finish_external_operation(handles[0], state="finished")
    with pytest.raises(ExternalOperationConflict):
        await register_one()
    async with get_session_factory()() as session:
        count = await session.scalar(select(func.count()).select_from(models.AccountExternalOperation).where(
            models.AccountExternalOperation.actor_user_id == uuid.UUID(owner.id),
            models.AccountExternalOperation.effect_key == key,
        ))
    assert count == 1


@pytest.mark.asyncio
async def test_uncertain_and_old_running_operations_still_block_drain(client, make_user):
    owner = await make_user("Uncertain external operation owner")
    request_id = await _deletion_request(client, owner)
    uncertain = await _register(owner, "uncertain-operation", kind="media_finalize_avatar")
    old_running = await _register(owner, "old-running-operation")
    await finish_external_operation(uncertain, state="uncertain")
    async with get_session_factory()() as session:
        row = await session.get(models.AccountExternalOperation, old_running.operation_id)
        row.created_at = datetime.now(timezone.utc) - timedelta(days=3650)
        await session.commit()
    await _activate(owner.id, request_id)

    drain = await inspect_registered_operation_drain(
        user_id=uuid.UUID(owner.id), request_id=uuid.UUID(request_id),
    )
    assert (drain.running_count, drain.uncertain_count) == (1, 1)
    assert drain.registered_operations_drained is False
    await finish_external_operation(old_running, state="finished")
    drain = await inspect_registered_operation_drain(
        user_id=uuid.UUID(owner.id), request_id=uuid.UUID(request_id),
    )
    assert (drain.running_count, drain.uncertain_count) == (0, 1)
    assert drain.registered_operations_drained is False


@pytest.mark.asyncio
async def test_drain_requires_matching_active_hold(client, make_user):
    owner = await make_user("Matching hold owner")
    request_id = await _deletion_request(client, owner)
    wrong_request_id = str(uuid.uuid4())
    with pytest.raises(DeletionHoldInvalid, match="active_deletion_hold_required"):
        await inspect_registered_operation_drain(
            user_id=uuid.UUID(owner.id), request_id=uuid.UUID(request_id),
        )
    await _activate(owner.id, request_id)
    with pytest.raises(DeletionHoldInvalid, match="active_deletion_hold_required"):
        await inspect_registered_operation_drain(
            user_id=uuid.UUID(owner.id), request_id=uuid.UUID(wrong_request_id),
        )


@pytest.mark.asyncio
async def test_finish_rejects_owner_token_scope_and_terminal_state_theft(client, make_user):
    owner = await make_user("Fenced operation owner")
    target = await make_user("Fenced operation target")
    handle = await _register(owner, "fence-check", targets=[target.id])

    with pytest.raises(DeletionHoldInvalid, match="external_operation_fence_invalid"):
        await finish_external_operation(replace(handle, owner_token=uuid.uuid4()))
    with pytest.raises(DeletionHoldInvalid, match="external_operation_fence_invalid"):
        await finish_external_operation(replace(handle, user_ids=(uuid.UUID(owner.id),)))
    with pytest.raises(DeletionHoldInvalid, match="invalid_external_operation_state"):
        await finish_external_operation(handle, state="running")  # type: ignore[arg-type]

    await finish_external_operation(handle, state="uncertain")
    with pytest.raises(DeletionHoldInvalid, match="external_operation_terminal_state"):
        await finish_external_operation(handle, state="finished")
    assert await _counts(handle.operation_id) == ("uncertain", 2)


@pytest.mark.asyncio
async def test_registration_rejects_identity_drift_tombstones_and_unknown_targets(client, make_user):
    actor = await make_user("Identity-bound external operation actor")
    target = await make_user("Tombstoned external operation target")
    with pytest.raises(DeletionHoldInvalid, match="write_guard_identity_changed"):
        await register_external_operation(
            user_id=uuid.UUID(actor.id), firebase_uid="wrong-firebase-uid",
            kind="media_finalize_post", effect_key=_effect("wrong-identity"),
        )
    with pytest.raises(DeletionHoldInvalid, match="write_guard_target_missing"):
        await _register(actor, "unknown-target", targets=[uuid.uuid4()])

    async with get_session_factory()() as session:
        user = await session.get(models.User, uuid.UUID(target.id))
        user.firebase_uid = f"deleted:{target.id}"
        user.is_ghost = True
        await session.commit()
    with pytest.raises(DeletionHoldInvalid, match="external_operation_target_deleted"):
        await _register(actor, "tombstoned-target", targets=[target.id])


@pytest.mark.asyncio
async def test_registration_insert_and_scope_links_rollback_as_one_unit(client, make_user):
    owner = await make_user("Atomic operation journal owner")
    target = await make_user("Atomic operation scope target")
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE FUNCTION public.test_reject_external_operation_scope() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'test scope insertion failure'; END $$
        """))
        await conn.execute(text("""
            CREATE TRIGGER test_reject_external_operation_scope
            BEFORE INSERT ON account_external_operation_scopes
            FOR EACH ROW EXECUTE FUNCTION public.test_reject_external_operation_scope()
        """))
    try:
        with pytest.raises(DeletionHoldInvalid, match="external_operation_unavailable"):
            await _register(owner, "atomic-rollback", targets=[target.id])
        async with get_session_factory()() as session:
            operations = await session.scalar(select(func.count()).select_from(models.AccountExternalOperation).where(
                models.AccountExternalOperation.actor_user_id == uuid.UUID(owner.id),
                models.AccountExternalOperation.effect_key == _effect("atomic-rollback"),
            ))
            scopes = await session.scalar(select(func.count()).select_from(models.AccountExternalOperationScope).join(
                models.AccountExternalOperation,
                models.AccountExternalOperationScope.operation_id == models.AccountExternalOperation.id,
            ).where(
                models.AccountExternalOperation.actor_user_id == uuid.UUID(owner.id),
                models.AccountExternalOperation.effect_key == _effect("atomic-rollback"),
            ))
        assert (operations, scopes) == (0, 0)
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DROP TRIGGER IF EXISTS test_reject_external_operation_scope ON account_external_operation_scopes"))
            await conn.execute(text("DROP FUNCTION IF EXISTS public.test_reject_external_operation_scope()"))


@pytest.mark.asyncio
async def test_registration_that_holds_shared_gate_finishes_before_exclusive_hold(client, make_user):
    """Pause at the real journal INSERT: hold activation must wait on its shared gate."""
    owner = await make_user("Admission race owner")
    request_id = await _deletion_request(client, owner)
    engine = get_engine()
    barrier_key = int.from_bytes(uuid.uuid4().bytes[:8], "big", signed=True)
    barrier_connection = await engine.connect()
    barrier_transaction = await barrier_connection.begin()
    await barrier_connection.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": barrier_key},
    )
    async with engine.begin() as conn:
        await conn.execute(text(f"""
            CREATE FUNCTION public.test_pause_external_operation_insert() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN
                PERFORM pg_advisory_xact_lock({barrier_key});
                RETURN NEW;
            END $$
        """))
        await conn.execute(text("""
            CREATE TRIGGER test_pause_external_operation_insert
            BEFORE INSERT ON account_external_operations
            FOR EACH ROW EXECUTE FUNCTION public.test_pause_external_operation_insert()
        """))

    task = asyncio.create_task(_register(owner, "admission-hold-race"))
    hold_task = None
    try:
        # The trigger is waiting on our unrelated barrier only after the registration's
        # transaction-scoped shared account lock has been acquired.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10.0
        registration_waiting = False
        while loop.time() < deadline:
            async with engine.connect() as conn:
                registration_waiting = bool(await conn.scalar(text(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "AND lower(wait_event) = 'advisory' "
                    "AND query ILIKE '%INSERT INTO account_external_operations%')"
                )))
            if registration_waiting:
                break
            await asyncio.sleep(0.02)
        assert registration_waiting, "registration did not reach its post-admission journal barrier"

        hold_task = asyncio.create_task(_activate(owner.id, request_id))
        # Confirm the exclusive activation is queued behind the registration's shared lock.
        deadline = loop.time() + 10.0
        hold_waiting = False
        while loop.time() < deadline:
            async with engine.connect() as conn:
                hold_waiting = bool(await conn.scalar(text(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "AND lower(wait_event) = 'advisory' "
                    "AND query ILIKE '%pg_advisory_xact_lock%' "
                    "AND query NOT ILIKE '%INSERT INTO account_external_operations%')"
                )))
            if hold_waiting:
                break
            await asyncio.sleep(0.02)
        assert hold_waiting, "hold activation did not queue behind the admitted shared gate"

        # Releasing this unrelated barrier lets registration commit, which releases its
        # shared account gate; only then can the queued exclusive activation proceed.
        await barrier_transaction.rollback()
        handle, _ = await asyncio.wait_for(
            asyncio.gather(task, hold_task), timeout=10.0,
        )
        drain = await inspect_registered_operation_drain(
            user_id=uuid.UUID(owner.id), request_id=uuid.UUID(request_id),
        )
        assert drain.running_count == 1
        await finish_external_operation(handle, state="finished")
    finally:
        if barrier_transaction.is_active:
            await barrier_transaction.rollback()
        await barrier_connection.close()
        all_tasks = [candidate for candidate in (task, hold_task) if candidate is not None]
        if all_tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*all_tasks, return_exceptions=True), timeout=10.0)
            except asyncio.TimeoutError:
                for candidate in all_tasks:
                    if not candidate.done():
                        candidate.cancel()
                await asyncio.gather(*all_tasks, return_exceptions=True)
        async with engine.begin() as conn:
            await conn.execute(text("DROP TRIGGER IF EXISTS test_pause_external_operation_insert ON account_external_operations"))
            await conn.execute(text("DROP FUNCTION IF EXISTS public.test_pause_external_operation_insert()"))
