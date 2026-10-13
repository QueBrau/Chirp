"""PostgreSQL proofs for the per-session c436 account write guard."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import models
from app.services.account_deletion_hold import (
    DeletionHoldActive,
    DeletionHoldInvalid,
    activate_deletion_hold,
    advisory_key,
)
from app.services.account_write_guard import (
    bind_account_write_guard,
    is_account_write_guard_bound,
)


async def _seed(url: str) -> tuple[object, uuid.UUID, str]:
    engine = create_async_engine(url)
    user_id = uuid.uuid4()
    firebase_uid = f"guard-{user_id}"
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        session.add(models.User(
            id=user_id,
            firebase_uid=firebase_uid,
            email=f"{firebase_uid}@invalid.test",
            display_name="Guard test",
            account_type="alumni",
        ))
        await session.commit()
    return engine, user_id, firebase_uid


@pytest.mark.asyncio
async def test_guard_rechecks_after_commit_and_is_session_local(migrated_db: str) -> None:
    engine, user_id, firebase_uid = await _seed(migrated_db)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with factory() as guarded, factory() as unguarded:
            bind_account_write_guard(guarded, user_id=user_id, firebase_uid=firebase_uid)
            assert is_account_write_guard_bound(guarded, firebase_uid=firebase_uid)
            assert not is_account_write_guard_bound(unguarded, firebase_uid=firebase_uid)
            assert await guarded.scalar(text("SELECT 1")) == 1
            await guarded.commit()
            assert await guarded.scalar(text("SELECT 1")) == 1
            await guarded.rollback()
            assert await guarded.scalar(text("SELECT 1")) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_guard_rejects_active_hold_before_application_sql(migrated_db: str) -> None:
    engine, user_id, firebase_uid = await _seed(migrated_db)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with factory() as seed:
            request_id = uuid.uuid4()
            seed.add(models.AccountDataRequest(
                id=request_id, user_id=user_id, kind="deletion", status="processing",
            ))
            await seed.flush()
            seed.add(models.AccountDeletionHold(
                user_id=user_id,
                request_id=request_id,
                state="active",
                reason="account_deletion",
            ))
            await seed.commit()
        async with factory() as guarded:
            bind_account_write_guard(guarded, user_id=user_id, firebase_uid=firebase_uid)
            with pytest.raises(DeletionHoldActive, match="account_deletion_hold_active"):
                await guarded.scalar(text("SELECT 1"))
            await guarded.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_guard_requires_clean_session_and_rejects_rebind(migrated_db: str) -> None:
    engine, user_id, firebase_uid = await _seed(migrated_db)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with factory() as session:
            await session.scalar(text("SELECT 1"))
            with pytest.raises(DeletionHoldInvalid, match="before_transaction"):
                bind_account_write_guard(session, user_id=user_id, firebase_uid=firebase_uid)
            await session.rollback()
            bind_account_write_guard(session, user_id=user_id, firebase_uid=firebase_uid)
            bind_account_write_guard(session, user_id=user_id, firebase_uid=firebase_uid)
            with pytest.raises(DeletionHoldInvalid, match="rebind_identity"):
                bind_account_write_guard(session, user_id=uuid.uuid4(), firebase_uid=firebase_uid)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_guard_rejects_identity_change_on_next_transaction(migrated_db: str) -> None:
    engine, user_id, firebase_uid = await _seed(migrated_db)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with factory() as guarded, factory() as changer:
            bind_account_write_guard(guarded, user_id=user_id, firebase_uid=firebase_uid)
            assert await guarded.scalar(text("SELECT 1")) == 1
            await guarded.commit()
            await changer.execute(
                text("UPDATE users SET firebase_uid = :uid WHERE id = :id"),
                {"uid": f"changed-{user_id}", "id": user_id},
            )
            await changer.commit()
            with pytest.raises(DeletionHoldInvalid, match="identity_changed"):
                await guarded.scalar(text("SELECT 1"))
            await guarded.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_guard_does_not_attach_a_global_event_and_nested_savepoint_is_safe(
    migrated_db: str,
) -> None:
    engine, user_id, firebase_uid = await _seed(migrated_db)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    other_id = uuid.uuid4()
    try:
        async with factory() as guarded, factory() as other:
            bind_account_write_guard(guarded, user_id=user_id, firebase_uid=firebase_uid)
            assert await guarded.scalar(text("SELECT 1")) == 1
            nested = await guarded.begin_nested()
            await guarded.execute(text("SELECT 1"))
            await nested.rollback()
            await guarded.commit()
            await other.scalar(text("SELECT 1"))
            await other.rollback()
            assert other_id != user_id
    finally:
        await engine.dispose()

@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["commit", "rollback"])
async def test_hold_after_boundary_rejects_same_session_flush(
    migrated_db: str, boundary: str,
) -> None:
    engine, user_id, firebase_uid = await _seed(migrated_db)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with factory() as guarded, factory() as seed:
            bind_account_write_guard(guarded, user_id=user_id, firebase_uid=firebase_uid)
            assert await guarded.scalar(text("SELECT 1")) == 1
            if boundary == "commit":
                await guarded.commit()
            else:
                await guarded.rollback()
            request_id = uuid.uuid4()
            seed.add(models.AccountDataRequest(
                id=request_id, user_id=user_id, kind="deletion", status="processing",
            ))
            await seed.commit()
            await activate_deletion_hold(seed, user_id=user_id, request_id=request_id)
            await seed.commit()
            guarded.add(models.AccountDataRequest(
                id=uuid.uuid4(), user_id=user_id, kind="export", status="received",
            ))
            with pytest.raises(DeletionHoldActive, match="account_deletion_hold_active"):
                await guarded.flush()
            await guarded.rollback()
            saved = await seed.scalar(text(
                "SELECT count(*) FROM account_data_requests "
                "WHERE user_id = :id AND kind = 'export'"), {"id": user_id},
            )
            assert saved == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_nested_rollback_retains_outer_shared_advisory_lock(migrated_db: str) -> None:
    engine, user_id, firebase_uid = await _seed(migrated_db)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with factory() as guarded, factory() as other:
            bind_account_write_guard(guarded, user_id=user_id, firebase_uid=firebase_uid)
            await guarded.scalar(text("SELECT 1"))
            nested = await guarded.begin_nested()
            await nested.rollback()
            acquired = await other.scalar(
                text("SELECT pg_try_advisory_xact_lock(:key)"),
                {"key": advisory_key(user_id)},
            )
            assert acquired is False
            await other.rollback()
            await guarded.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_failed_begin_cannot_be_reused_without_rollback(migrated_db: str) -> None:
    engine, user_id, firebase_uid = await _seed(migrated_db)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with factory() as seed:
            request_id = uuid.uuid4()
            seed.add(models.AccountDataRequest(
                id=request_id, user_id=user_id, kind="deletion", status="processing",
            ))
            await seed.flush()
            seed.add(models.AccountDeletionHold(
                user_id=user_id, request_id=request_id, state="active", reason="account_deletion",
            ))
            await seed.commit()
        async with factory() as guarded:
            bind_account_write_guard(guarded, user_id=user_id, firebase_uid=firebase_uid)
            with pytest.raises(DeletionHoldActive):
                await guarded.scalar(text("SELECT 1"))
            with pytest.raises(DeletionHoldInvalid, match="rollback_required"):
                await guarded.scalar(text("SELECT 1"))
            guarded.add(models.AccountDataRequest(
                id=uuid.uuid4(), user_id=user_id, kind="export", status="received",
            ))
            with pytest.raises(DeletionHoldInvalid, match="rollback_required"):
                await guarded.flush()
            with pytest.raises(DeletionHoldInvalid, match="rollback_required"):
                await guarded.commit()
            await guarded.rollback()
            with pytest.raises(DeletionHoldActive, match="account_deletion_hold_active"):
                await guarded.scalar(text("SELECT 1"))
            await guarded.rollback()
        async with factory() as cleanup:
            await cleanup.execute(text("DELETE FROM account_deletion_holds WHERE user_id = :id"), {"id": user_id})
            await cleanup.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_repeatable_read_and_pending_session_are_rejected(migrated_db: str) -> None:
    engine, user_id, firebase_uid = await _seed(migrated_db)
    repeat_engine = create_async_engine(migrated_db, isolation_level="REPEATABLE READ")
    repeat_factory = async_sessionmaker(repeat_engine, expire_on_commit=False, autoflush=False)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with repeat_factory() as session:
            bind_account_write_guard(session, user_id=user_id, firebase_uid=firebase_uid)
            with pytest.raises(DeletionHoldInvalid, match="read_committed_required"):
                await session.scalar(text("SELECT 1"))
            await session.rollback()
        async with factory() as session:
            session.add(models.User(
                id=uuid.uuid4(), firebase_uid=f"pending-{user_id}",
                email=f"pending-{user_id}@invalid.test", display_name="Pending", account_type="alumni",
            ))
            with pytest.raises(DeletionHoldInvalid, match="before_transaction"):
                bind_account_write_guard(session, user_id=user_id, firebase_uid=firebase_uid)
            await session.rollback()
    finally:
        await repeat_engine.dispose()
        await engine.dispose()

@pytest.mark.asyncio
async def test_identity_race_before_first_guarded_sql_fails_closed(migrated_db: str) -> None:
    engine, user_id, firebase_uid = await _seed(migrated_db)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with factory() as guarded, factory() as changer:
            bind_account_write_guard(guarded, user_id=user_id, firebase_uid=firebase_uid)
            await changer.execute(
                text("UPDATE users SET firebase_uid = :uid WHERE id = :id"),
                {"uid": f"changed-before-{user_id}", "id": user_id},
            )
            await changer.commit()
            with pytest.raises(DeletionHoldInvalid, match="identity_changed"):
                await guarded.scalar(text("SELECT 1"))
            with pytest.raises(DeletionHoldInvalid, match="rollback_required"):
                await guarded.scalar(text("SELECT 1"))
            await guarded.rollback()
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_guard_lock_wait_is_bounded_and_retryable(
    migrated_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.account_write_guard as guard_module

    monkeypatch.setattr(guard_module, "GUARD_LOCK_TIMEOUT_MS", 25)
    engine, user_id, firebase_uid = await _seed(migrated_db)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with factory() as blocker, factory() as guarded:
            await blocker.scalar(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": advisory_key(user_id)},
            )
            bind_account_write_guard(guarded, user_id=user_id, firebase_uid=firebase_uid)
            with pytest.raises(DeletionHoldInvalid, match="write_guard_unavailable"):
                await guarded.scalar(text("SELECT 1"))
            await guarded.rollback()
            await blocker.rollback()
            assert await guarded.scalar(text("SELECT 1")) == 1
            await guarded.rollback()
    finally:
        await engine.dispose()
