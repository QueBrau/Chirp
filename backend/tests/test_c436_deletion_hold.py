"""Real PostgreSQL proofs for the isolated c436 deletion-hold primitive."""
import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app import models
from app.services.account_deletion_hold import (
    DeletionHoldActive,
    DeletionHoldInvalid,
    DeletionHoldView,
    activate_deletion_hold,
    admit_user_write,
    inspect_deletion_hold,
)


async def _observe_blocked(
    task: asyncio.Task[DeletionHoldView], engine: AsyncEngine, waiting_pid: int, blocking_pid: int,
) -> None:
    """Require PostgreSQL to observe the lock wait, not just a slow coroutine."""
    try:
        async with asyncio.timeout(5):
            async with engine.connect() as observer:
                while True:
                    assert not task.done(), "activation did not wait for the admitted transaction"
                    blockers = await observer.scalar(
                        text("SELECT pg_blocking_pids(:pid)"), {"pid": waiting_pid},
                    )
                    if blocking_pid in blockers:
                        return
                    await asyncio.sleep(0.01)
    except BaseException:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise


async def _seed(
    url: str, *, users: int = 1,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession], list[uuid.UUID], list[uuid.UUID]]:
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
async def test_activation_is_provisional_idempotent_and_rejects_foreign_request(migrated_db: str) -> None:
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
async def test_admitted_writer_blocks_activation_until_commit_then_new_writer_is_denied(migrated_db: str) -> None:
    engine, factory, ids, request_ids = await _seed(migrated_db)
    try:
        writer = factory()
        activator = factory()
        try:
            await admit_user_write(writer, ids[0])
            writer_pid = await writer.scalar(text("SELECT pg_backend_pid()"))
            activator_pid = await activator.scalar(text("SELECT pg_backend_pid()"))
            task = asyncio.create_task(
                activate_deletion_hold(activator, user_id=ids[0], request_id=request_ids[0])
            )
            await _observe_blocked(task, engine, activator_pid, writer_pid)
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
async def test_canceled_activation_rolls_back_without_persisting_hold(migrated_db: str) -> None:
    engine, factory, ids, request_ids = await _seed(migrated_db)
    writer = factory()
    activator = factory()
    try:
        await admit_user_write(writer, ids[0])
        writer_pid = await writer.scalar(text("SELECT pg_backend_pid()"))
        activator_pid = await activator.scalar(text("SELECT pg_backend_pid()"))
        task = asyncio.create_task(
            activate_deletion_hold(activator, user_id=ids[0], request_id=request_ids[0])
        )
        await _observe_blocked(task, engine, activator_pid, writer_pid)
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
async def test_activation_rejects_shared_lock_upgrade(migrated_db: str) -> None:
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
async def test_concurrent_same_account_activation_serializes_requests(migrated_db: str) -> None:
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
            first_pid = await first.scalar(text("SELECT pg_backend_pid()"))
            second_pid = await second.scalar(text("SELECT pg_backend_pid()"))
            blocked = asyncio.create_task(
                activate_deletion_hold(second, user_id=ids[0], request_id=second_request)
            )
            await _observe_blocked(blocked, engine, second_pid, first_pid)
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
async def test_pending_transaction_gets_fresh_hold_and_rollback_releases_lock(migrated_db: str) -> None:
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
async def test_different_accounts_do_not_block_and_isolation_fails_closed(migrated_db: str) -> None:
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


@pytest.mark.asyncio
async def test_foreign_owner_and_wrong_kind_cannot_acquire_hold(migrated_db: str) -> None:
    engine, factory, ids, requests = await _seed(migrated_db, users=2)
    try:
        async with factory() as session:
            with pytest.raises(DeletionHoldInvalid, match="deletion_request_invalid"):
                await activate_deletion_hold(session, user_id=ids[0], request_id=requests[1])
            await session.rollback()
            request = await session.get(models.AccountDataRequest, requests[0])
            request.kind = "export"
            await session.commit()
            with pytest.raises(DeletionHoldInvalid, match="deletion_request_invalid"):
                await activate_deletion_hold(session, user_id=ids[0], request_id=requests[0])
            await session.rollback()
            assert await inspect_deletion_hold(session, user_id=ids[0]) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["canceled", "failed", "completed"])
async def test_idempotent_activation_rechecks_cached_request_state(migrated_db: str, status: str) -> None:
    engine, factory, ids, requests = await _seed(migrated_db)
    try:
        async with factory() as session:
            await activate_deletion_hold(session, user_id=ids[0], request_id=requests[0])
            await session.commit()
            cached = await session.get(models.AccountDataRequest, requests[0])
            assert cached.status == "processing"
            async with factory() as other:
                request = await other.get(models.AccountDataRequest, requests[0])
                request.status = status
                await other.commit()
            assert cached.status == "processing"
            with pytest.raises(DeletionHoldInvalid, match="deletion_request_invalid"):
                await activate_deletion_hold(session, user_id=ids[0], request_id=requests[0])
            await session.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pending_changes_rejected_and_gate_recovers_after_rollback(migrated_db: str) -> None:
    engine, factory, ids, requests = await _seed(migrated_db)
    try:
        async with factory() as session:
            request = await session.get(models.AccountDataRequest, requests[0])
            request.failure_code = "pending_change"
            with pytest.raises(DeletionHoldInvalid, match="hold_gate_must_be_first_operation"):
                await admit_user_write(session, ids[0])
            await session.rollback()
            await admit_user_write(session, ids[0])
            await session.rollback()
            # The transaction-local upgrade guard must disappear after rollback.
            await activate_deletion_hold(session, user_id=ids[0], request_id=requests[0])
            await session.commit()
    finally:
        await engine.dispose()
