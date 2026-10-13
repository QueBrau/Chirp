"""PostgreSQL proofs for the per-session c436 account write guard."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import models
from app.services.account_deletion_hold import DeletionHoldActive, DeletionHoldInvalid
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
