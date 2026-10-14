"""Real PostgreSQL boundaries for atomic batches that silently omit held recipients."""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select, text, update

from app import models
from app.services.account_deletion_hold import DeletionHoldActive, DeletionHoldInvalid, activate_deletion_hold
from app.services.account_write_guard import (
    MAX_FILTERED_WRITE_CANDIDATES,
    bind_account_write_guard,
    bind_filtered_account_write_guard,
    eligible_account_write_targets,
)
from tests.test_c436_deletion_hold import _observe_blocked
from tests.test_c436_target_write_admission import _hold, _request, _seed


@pytest.mark.asyncio
async def test_filtered_targets_skip_holds_and_completed_stubs_but_keep_other_users(migrated_db):
    engine, factory, users = await _seed(migrated_db, count=4)
    actor, actor_uid = users[0]
    held, completed, eligible = (u[0] for u in users[1:])
    candidates = [held, completed, eligible]
    try:
        await _hold(factory, held)
        async with factory() as session:
            await session.execute(update(models.User).where(models.User.id == completed).values(
                firebase_uid=f"deleted:{completed}", is_ghost=True,
            ))
            await session.commit()
        async with factory() as guarded:
            bind_filtered_account_write_guard(guarded, user_id=actor, firebase_uid=actor_uid, candidate_user_ids=candidates)
            await guarded.scalar(text("SELECT 1"))
            assert eligible_account_write_targets(guarded, user_id=actor, candidate_user_ids=reversed(candidates)) == {eligible}
            await guarded.commit()
        await _hold(factory, actor)
        async with factory() as denied:
            bind_filtered_account_write_guard(denied, user_id=actor, firebase_uid=actor_uid, candidate_user_ids=candidates)
            with pytest.raises(DeletionHoldActive):
                await denied.scalar(text("SELECT 1"))
            with pytest.raises(DeletionHoldInvalid, match="rollback_required"):
                await denied.commit()
            await denied.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["commit", "rollback"])
async def test_eligibility_is_recomputed_after_every_transaction(migrated_db, boundary):
    engine, factory, users = await _seed(migrated_db)
    actor, actor_uid = users[0]
    target = users[1][0]
    try:
        async with factory() as guarded:
            bind_filtered_account_write_guard(guarded, user_id=actor, firebase_uid=actor_uid, candidate_user_ids=[target])
            await guarded.scalar(text("SELECT 1"))
            assert eligible_account_write_targets(guarded, user_id=actor, candidate_user_ids=[target]) == {target}
            await getattr(guarded, boundary)()
            await _hold(factory, target)
            await guarded.scalar(text("SELECT 1"))
            assert eligible_account_write_targets(guarded, user_id=actor, candidate_user_ids=[target]) == set()
            await guarded.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_stale_eligibility_and_changed_candidates_cannot_commit_pending_writes(migrated_db):
    engine, factory, users = await _seed(migrated_db, count=3)
    actor, actor_uid = users[0]
    target, other = users[1][0], users[2][0]
    try:
        async with factory() as guarded:
            bind_filtered_account_write_guard(guarded, user_id=actor, firebase_uid=actor_uid, candidate_user_ids=[target])
            await guarded.scalar(text("SELECT 1"))
            await guarded.commit()
            with pytest.raises(DeletionHoldInvalid, match="transaction_required"):
                eligible_account_write_targets(guarded, user_id=actor, candidate_user_ids=[target])
            with pytest.raises(DeletionHoldInvalid, match="rollback_required"):
                await guarded.scalar(text("SELECT 1"))
        async with factory() as guarded:
            bind_filtered_account_write_guard(guarded, user_id=actor, firebase_uid=actor_uid, candidate_user_ids=[target])
            await guarded.execute(update(models.User).where(models.User.id == actor).values(display_name="must roll back"))
            with pytest.raises(DeletionHoldInvalid, match="target_set_changed"):
                eligible_account_write_targets(guarded, user_id=actor, candidate_user_ids=[other])
            with pytest.raises(DeletionHoldInvalid, match="rollback_required"):
                await guarded.commit()
            await guarded.rollback()
        async with factory() as reader:
            assert await reader.scalar(select(models.User.display_name).where(models.User.id == actor)) == "Target"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_filtered_batch_holds_every_target_gate_until_atomic_commit(migrated_db):
    engine, factory, users = await _seed(migrated_db)
    actor, actor_uid = users[0]
    target = users[1][0]
    request_id = await _request(factory, target)
    activation = None
    try:
        async with factory() as guarded, factory() as activator:
            bind_filtered_account_write_guard(guarded, user_id=actor, firebase_uid=actor_uid, candidate_user_ids=[target])
            writer_pid = await guarded.scalar(text("SELECT pg_backend_pid()"))
            assert eligible_account_write_targets(guarded, user_id=actor, candidate_user_ids=[target]) == {target}
            activator_pid = await activator.scalar(text("SELECT pg_backend_pid()"))
            activation = asyncio.create_task(activate_deletion_hold(activator, user_id=target, request_id=request_id))
            try:
                await _observe_blocked(activation, engine, activator_pid, writer_pid)
                await guarded.commit()
                await asyncio.wait_for(activation, timeout=2)
                await activator.commit()
                await guarded.scalar(text("SELECT 1"))
                assert eligible_account_write_targets(guarded, user_id=actor, candidate_user_ids=[target]) == set()
                await guarded.rollback()
            finally:
                if not activation.done():
                    activation.cancel()
                await asyncio.gather(activation, return_exceptions=True)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_binding_cannot_switch_modes_or_grow_beyond_invite_contract(migrated_db):
    engine, factory, users = await _seed(migrated_db)
    actor, actor_uid = users[0]
    target = users[1][0]
    try:
        async with factory() as guarded:
            bind_filtered_account_write_guard(guarded, user_id=actor, firebase_uid=actor_uid, candidate_user_ids=[target])
            with pytest.raises(DeletionHoldInvalid, match="rebind_identity"):
                bind_account_write_guard(guarded, user_id=actor, firebase_uid=actor_uid, target_user_ids=[target])
        async with factory() as guarded:
            bind_account_write_guard(guarded, user_id=actor, firebase_uid=actor_uid)
            with pytest.raises(DeletionHoldInvalid, match="rebind_identity"):
                bind_filtered_account_write_guard(guarded, user_id=actor, firebase_uid=actor_uid, candidate_user_ids=[target])
        async with factory() as guarded:
            with pytest.raises(DeletionHoldInvalid, match="target_set_too_large"):
                bind_filtered_account_write_guard(guarded, user_id=actor, firebase_uid=actor_uid,
                    candidate_user_ids=[uuid.uuid4() for _ in range(MAX_FILTERED_WRITE_CANDIDATES + 1)])
            assert not guarded.in_transaction()
    finally:
        await engine.dispose()
