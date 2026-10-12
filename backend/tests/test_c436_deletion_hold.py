"""Real PostgreSQL proofs for the isolated c436 deletion-hold primitive."""
import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import models
from app.services.account_deletion_hold import (
    DeletionHoldActive,
    DeletionHoldInvalid,
    activate_deletion_hold,
    admit_user_write,
    inspect_deletion_hold,
)


async def _seed(url, *, users=1):
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    ids = [uuid.uuid4() for _ in range(users)]
    request_ids = [uuid.uuid4() for _ in ids]
    async with factory() as session:
        for user_id in ids:
            session.add(models.User(
                id=user_id, firebase_uid=f"hold-{user_id}", email=f"hold-{user_id}@invalid.test",
                display_name="Hold test", account_type="alumni",
            ))
        await session.flush()
        for index, user_id in enumerate(ids):
            session.add(models.AccountDataRequest(
                id=request_ids[index], user_id=user_id, kind="deletion", status="processing",
            ))
        await session.commit()
    return engine, factory, ids, request_ids


@pytest.mark.asyncio
async def test_activation_is_provisional_idempotent_and_rejects_foreign_request(migrated_db):
    engine, factory, ids, request_ids = await _seed(migrated_db)
    try:
        async with factory() as session:
            first = await activate_deletion_hold(session, user_id=ids[0], request_id=request_ids[0])
            again = await activate_deletion_hold(session, user_id=ids[0], request_id=request_ids[0])
            assert first.id == again.id
            await session.commit()
        async with factory() as observer:
            visible = await inspect_deletion_hold(observer, user_id=ids[0])
            assert visible is not None and visible.request_id == request_ids[0]
            await observer.commit()
        foreign_request = uuid.uuid4()
        async with factory() as session:
            session.add(models.AccountDataRequest(
                id=foreign_request, user_id=ids[0], kind="deletion", status="processing",
            ))
            await session.commit()
        async with factory() as session:
            with pytest.raises(DeletionHoldActive, match="different_deletion_hold_active"):
                await activate_deletion_hold(session, user_id=ids[0], request_id=foreign_request)
            await session.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_admitted_writer_blocks_activation_until_commit_then_new_writer_is_denied(migrated_db):
    engine, factory, ids, request_ids = await _seed(migrated_db)
    try:
        writer = factory()
        activator = factory()
        try:
            await admit_user_write(writer, ids[0])
            started = asyncio.Event()

            async def activate():
                started.set()
                return await activate_deletion_hold(activator, user_id=ids[0], request_id=request_ids[0])

            task = asyncio.create_task(activate())
            await started.wait()
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=0.15)
            await writer.commit()
            hold = await asyncio.wait_for(task, timeout=2)
            assert hold.request_id == request_ids[0]
            await activator.commit()
        finally:
            await writer.close()
            await activator.close()
        async with factory() as blocked:
            with pytest.raises(DeletionHoldActive):
                await admit_user_write(blocked, ids[0])
            await blocked.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_canceled_activation_rolls_back_without_persisting_hold(migrated_db):
    engine, factory, ids, request_ids = await _seed(migrated_db)
    writer = factory()
    activator = factory()
    try:
        await admit_user_write(writer, ids[0])
        task = asyncio.create_task(
            activate_deletion_hold(activator, user_id=ids[0], request_id=request_ids[0])
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await activator.rollback()
        await writer.commit()
        async with factory() as observer:
            assert await inspect_deletion_hold(observer, user_id=ids[0]) is None
            await observer.commit()
    finally:
        await writer.close()
        await activator.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_activation_rejects_shared_lock_upgrade(migrated_db):
    engine, factory, ids, request_ids = await _seed(migrated_db)
    try:
        async with factory() as session:
            await admit_user_write(session, ids[0])
            with pytest.raises(DeletionHoldInvalid, match="cannot_upgrade_shared_hold"):
                await activate_deletion_hold(session, user_id=ids[0], request_id=request_ids[0])
            await session.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_same_account_activation_serializes_requests(migrated_db):
    engine, factory, ids, request_ids = await _seed(migrated_db)
    second_request = uuid.uuid4()
    try:
        async with factory() as seed:
            seed.add(models.AccountDataRequest(
                id=second_request, user_id=ids[0], kind="deletion", status="processing",
            ))
            await seed.commit()
        first = factory()
        second = factory()
        try:
            first_hold = await activate_deletion_hold(first, user_id=ids[0], request_id=request_ids[0])
            blocked = asyncio.create_task(
                activate_deletion_hold(second, user_id=ids[0], request_id=second_request)
            )
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(blocked), timeout=0.1)
            await first.commit()
            with pytest.raises(DeletionHoldActive, match="different_deletion_hold_active"):
                await asyncio.wait_for(blocked, timeout=2)
            assert first_hold.request_id == request_ids[0]
            await second.rollback()
        finally:
            await first.close()
            await second.close()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pending_transaction_gets_fresh_hold_and_rollback_releases_lock(migrated_db):
    engine, factory, ids, request_ids = await _seed(migrated_db, users=2)
    try:
        async with factory() as pending:
            await pending.execute(text("SELECT 1"))
            async with factory() as activator:
                await activate_deletion_hold(activator, user_id=ids[0], request_id=request_ids[0])
                await activator.commit()
            with pytest.raises(DeletionHoldActive):
                await admit_user_write(pending, ids[0])
            await pending.rollback()
            async with factory() as rollback_session:
                await activate_deletion_hold(rollback_session, user_id=ids[1], request_id=request_ids[1])
                await rollback_session.rollback()
        async with factory() as writer:
            await admit_user_write(writer, ids[1])
            await writer.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_different_accounts_do_not_block_and_isolation_fails_closed(migrated_db):
    engine, factory, ids, request_ids = await _seed(migrated_db, users=2)
    try:
        async with factory() as first:
            await activate_deletion_hold(first, user_id=ids[0], request_id=request_ids[0])
            async with factory() as other:
                await admit_user_write(other, ids[1])
                await other.commit()
            await first.rollback()
        async with engine.connect() as connection:
            await connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            async with AsyncSession(bind=connection, expire_on_commit=False, autoflush=False) as repeatable:
                with pytest.raises(DeletionHoldInvalid, match="read_committed_required"):
                    await admit_user_write(repeatable, ids[1])
                await repeatable.rollback()
    finally:
        await engine.dispose()
