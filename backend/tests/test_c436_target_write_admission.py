"""Focused PostgreSQL proofs for multi-account write admission."""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import models
from app.services.account_deletion_hold import (
    DeletionHoldActive,
    DeletionHoldInvalid,
    activate_deletion_hold,
    advisory_key,
)
from app.services.account_write_guard import (
    MAX_GUARDED_ACCOUNTS,
    _target_ids,
    _advisory_keys,
    assert_account_write_targets,
    bind_account_write_guard,
    is_account_write_guard_bound,
)
from tests.test_c436_deletion_hold import _observe_blocked


async def _seed(url: str, count: int = 2):
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    users = [(uuid.uuid4(), f"target-{uuid.uuid4()}") for _ in range(count)]
    async with factory() as session:
        session.add_all(
            models.User(
                id=user_id, firebase_uid=firebase_uid,
                email=f"{firebase_uid}@invalid.test", display_name="Target", account_type="alumni",
            )
            for user_id, firebase_uid in users
        )
        await session.commit()
    return engine, factory, users


async def _hold(factory, user_id: uuid.UUID) -> uuid.UUID:
    request_id = uuid.uuid4()
    async with factory() as session:
        session.add(models.AccountDataRequest(
            id=request_id, user_id=user_id, kind="deletion", status="processing",
        ))
        await session.flush()
        session.add(models.AccountDeletionHold(
            user_id=user_id, request_id=request_id, state="active", reason="account_deletion",
        ))
        await session.commit()
    return request_id


async def _request(factory, user_id: uuid.UUID) -> uuid.UUID:
    request_id = uuid.uuid4()
    async with factory() as session:
        session.add(models.AccountDataRequest(
            id=request_id, user_id=user_id, kind="deletion", status="processing",
        ))
        await session.commit()
    return request_id


@pytest.mark.asyncio
async def test_target_hold_rechecked_and_denial_poisoned_until_rollback(migrated_db: str) -> None:
    engine, factory, users = await _seed(migrated_db)
    actor, actor_uid = users[0]
    target, _target_uid = users[1]
    try:
        await _hold(factory, target)
        async with factory() as guarded:
            bind_account_write_guard(guarded, user_id=actor, firebase_uid=actor_uid, target_user_ids=[target])
            with pytest.raises(DeletionHoldActive, match="account_deletion_hold_active"):
                await guarded.scalar(text("SELECT 1"))
            with pytest.raises(DeletionHoldInvalid, match="rollback_required"):
                await guarded.scalar(text("SELECT 1"))
            with pytest.raises(DeletionHoldInvalid, match="rollback_required"):
                await guarded.commit()
            await guarded.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_missing_target_is_generic_and_cannot_run_application_sql(migrated_db: str) -> None:
    engine, factory, users = await _seed(migrated_db)
    actor, actor_uid = users[0]
    missing = uuid.uuid4()
    try:
        async with factory() as guarded:
            bind_account_write_guard(guarded, user_id=actor, firebase_uid=actor_uid, target_user_ids=[missing])
            with pytest.raises(DeletionHoldInvalid, match="write_guard_target_missing") as error:
                await guarded.scalar(text("SELECT 1"))
            assert str(missing) not in str(error.value)
            await guarded.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_scope_is_immutable_and_exact_set_assertion_is_order_independent(migrated_db: str) -> None:
    engine, factory, users = await _seed(migrated_db, count=3)
    actor, actor_uid = users[0]
    target, _ = users[1]
    other, _ = users[2]
    try:
        async with factory() as session:
            bind_account_write_guard(session, user_id=actor, firebase_uid=actor_uid, target_user_ids=[target])
            assert is_account_write_guard_bound(
                session, firebase_uid=actor_uid, target_user_ids=[target],
            )
            assert is_account_write_guard_bound(
                session, firebase_uid=actor_uid, target_user_ids=[actor, target],
            )
            assert not is_account_write_guard_bound(
                session, firebase_uid=actor_uid, target_user_ids=[other],
            )
            assert_account_write_targets(session, user_id=actor, target_user_ids=[target, actor])
            with pytest.raises(DeletionHoldInvalid, match="target_set_changed"):
                assert_account_write_targets(session, user_id=actor, target_user_ids=[other])
            with pytest.raises(DeletionHoldInvalid, match="rollback_required"):
                await session.scalar(text("SELECT 1"))
            await session.rollback()
            # A fresh admission is required after a scope mismatch; the old
            # binding cannot be widened after rollback.
            with pytest.raises(DeletionHoldInvalid, match="write_guard_rebind_identity"):
                bind_account_write_guard(session, user_id=actor, firebase_uid=actor_uid, target_user_ids=[other])
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_target_hold_between_transactions_blocks_next_boundary(migrated_db: str) -> None:
    engine, factory, users = await _seed(migrated_db)
    actor, actor_uid = users[0]
    target, _ = users[1]
    try:
        async with factory() as guarded:
            bind_account_write_guard(guarded, user_id=actor, firebase_uid=actor_uid, target_user_ids=[target])
            assert await guarded.scalar(text("SELECT 1")) == 1
            await guarded.commit()
            await _hold(factory, target)
            with pytest.raises(DeletionHoldActive, match="account_deletion_hold_active"):
                await guarded.scalar(text("SELECT 1"))
            await guarded.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_target_exclusive_activation_waits_for_admitted_writer(migrated_db: str) -> None:
    """A target deletion cannot activate while its admitted writer holds shared scope."""
    engine, factory, users = await _seed(migrated_db)
    actor, actor_uid = users[0]
    target, _ = users[1]
    request_id = await _request(factory, target)
    try:
        async with factory() as guarded, factory() as activator:
            bind_account_write_guard(
                guarded, user_id=actor, firebase_uid=actor_uid, target_user_ids=[target],
            )
            assert await guarded.scalar(text("SELECT 1")) == 1
            writer_pid = await guarded.scalar(text("SELECT pg_backend_pid()"))
            activator_pid = await activator.scalar(text("SELECT pg_backend_pid()"))
            activation = asyncio.create_task(
                activate_deletion_hold(activator, user_id=target, request_id=request_id)
            )
            await _observe_blocked(activation, engine, activator_pid, writer_pid)
            await guarded.commit()
            view = await asyncio.wait_for(activation, timeout=2)
            assert view.user_id == target and view.request_id == request_id
            await activator.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_exactly_maximum_bound_accounts_execute_guarded_sql(migrated_db: str) -> None:
    engine, factory, users = await _seed(migrated_db, count=MAX_GUARDED_ACCOUNTS)
    actor, actor_uid = users[0]
    targets = [user_id for user_id, _uid in users[1:]]
    try:
        async with factory() as guarded:
            bind_account_write_guard(
                guarded, user_id=actor, firebase_uid=actor_uid, target_user_ids=targets,
            )
            assert await guarded.scalar(text("SELECT 1")) == 1
            await guarded.rollback()
    finally:
        await engine.dispose()


def test_advisory_scope_is_sorted_deduplicated_and_bounded() -> None:
    actor, target = uuid.uuid4(), uuid.uuid4()
    keys = _advisory_keys(tuple(sorted((actor, target), key=str)))
    assert keys == tuple(sorted(set(keys)))
    assert len(keys) <= 2
    assert MAX_GUARDED_ACCOUNTS >= 10
    exact = _target_ids(actor, [uuid.uuid4() for _ in range(MAX_GUARDED_ACCOUNTS - 1)])
    assert len(exact) == MAX_GUARDED_ACCOUNTS
    with pytest.raises(DeletionHoldInvalid, match="target_set_too_large"):
        _target_ids(actor, [uuid.uuid4() for _ in range(MAX_GUARDED_ACCOUNTS)])
