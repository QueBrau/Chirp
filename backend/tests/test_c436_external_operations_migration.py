"""Migration 0045 creates a constrained, downgradeable external-operation journal."""
from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine


BACKEND_DIR = Path(__file__).resolve().parents[1]


def _database_of(url: str) -> str:
    return url.rpartition("/")[2].partition("?")[0]


def _swap_database(url: str, database: str) -> str:
    head, _, tail = url.rpartition("/")
    _, sep, query = tail.partition("?")
    return f"{head}/{database}{sep}{query}"


async def _admin_execute(admin_url: str, statements: list[str]) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            for statement in statements:
                await conn.execute(text(statement))
    finally:
        await engine.dispose()


async def _probe(admin_url: str) -> None:
    engine = create_async_engine(admin_url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    finally:
        await engine.dispose()


def _alembic(url: str, revision: str, *, down: bool = False) -> None:
    os.environ["DATABASE_URL"] = url
    from app.config import get_settings

    get_settings.cache_clear()
    from alembic import command
    from alembic.config import Config as AlembicConfig

    cfg = AlembicConfig(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    (command.downgrade if down else command.upgrade)(cfg, revision)


async def _table_exists(url: str, table: str) -> bool:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text("SELECT to_regclass(:name)"), {"name": table})).scalar_one() is not None
    finally:
        await engine.dispose()


async def _assert_rejected(url: str, sql: str, params: dict[str, object], constraint: str) -> None:
    engine = create_async_engine(url)
    try:
        with pytest.raises(DBAPIError, match=constraint):
            async with engine.begin() as conn:
                await conn.execute(text(sql), params)
    finally:
        await engine.dispose()


async def _constraint_round_trip(url: str) -> None:
    user_id = str(uuid.uuid4())
    op_id = str(uuid.uuid4())
    owner_token = str(uuid.uuid4())
    effect = "a" * 64
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO users (id, firebase_uid, email, display_name, account_type) "
                "VALUES (:id, :uid, :email, 'Journal user', 'greek')"
            ), {"id": user_id, "uid": f"uid-{user_id}", "email": f"{user_id}@example.edu"})
            await conn.execute(text(
                "INSERT INTO account_external_operations "
                "(id, actor_user_id, kind, effect_key, owner_token, state) "
                "VALUES (:id, :user_id, 'media_finalize_post', :effect, :token, 'running')"
            ), {"id": op_id, "user_id": user_id, "effect": effect, "token": owner_token})
            await conn.execute(text(
                "INSERT INTO account_external_operation_scopes (operation_id, user_id) "
                "VALUES (:operation_id, :user_id)"
            ), {"operation_id": op_id, "user_id": user_id})
        await _assert_rejected(url,
            "INSERT INTO account_external_operations (actor_user_id, kind, effect_key, owner_token) "
            "VALUES (:uid, 'media_finalize_post', :effect, :token)",
            {"uid": user_id, "effect": effect, "token": str(uuid.uuid4())},
            "uq_account_external_operation_effect")
        await _assert_rejected(url,
            "INSERT INTO account_external_operations (actor_user_id, kind, effect_key, owner_token) "
            "VALUES (:uid, 'unknown', :effect, :token)",
            {"uid": user_id, "effect": "b" * 64, "token": str(uuid.uuid4())},
            "ck_account_external_operation_kind")
        await _assert_rejected(url,
            "INSERT INTO account_external_operations (actor_user_id, kind, effect_key, owner_token) "
            "VALUES (:uid, 'media_finalize_avatar', 'not-a-sha256', :token)",
            {"uid": user_id, "token": str(uuid.uuid4())},
            "ck_account_external_operation_effect_key")
        await _assert_rejected(url,
            "INSERT INTO account_external_operations "
            "(actor_user_id, kind, effect_key, owner_token, state) "
            "VALUES (:uid, 'media_finalize_avatar', :effect, :token, 'finished')",
            {"uid": user_id, "effect": "c" * 64, "token": str(uuid.uuid4())},
            "ck_account_external_operation_completion_time")
    finally:
        await engine.dispose()


def test_migration_0045_external_operation_journal_round_trip() -> None:
    requested = os.environ.get(
        "TEST_DATABASE_URL", "postgresql+asyncpg://chirp:chirp@localhost:5432/chirp_test"
    )
    base = _database_of(requested)
    admin_url = _swap_database(requested, "postgres")
    db_name = f"{base}_c436ops_{uuid.uuid4().hex[:8]}"
    url = _swap_database(requested, db_name)
    try:
        asyncio.run(_probe(admin_url))
    except Exception:
        pytest.skip("postgres not available — local PostgreSQL required")

    original = os.environ.get("DATABASE_URL")
    asyncio.run(_admin_execute(admin_url, [
        f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)',
        f'CREATE DATABASE "{db_name}" ENCODING \'UTF8\' TEMPLATE template0',
    ]))
    try:
        _alembic(url, "0044")
        assert asyncio.run(_table_exists(url, "account_external_operations")) is False
        _alembic(url, "0045")
        assert asyncio.run(_table_exists(url, "account_external_operations")) is True
        assert asyncio.run(_table_exists(url, "account_external_operation_scopes")) is True
        asyncio.run(_constraint_round_trip(url))
        _alembic(url, "0044", down=True)
        assert asyncio.run(_table_exists(url, "account_external_operation_scopes")) is False
        assert asyncio.run(_table_exists(url, "account_external_operations")) is False
        _alembic(url, "0045")
        assert asyncio.run(_table_exists(url, "account_external_operations")) is True
    finally:
        if original is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original
        from app.config import get_settings

        get_settings.cache_clear()
        asyncio.run(_admin_execute(admin_url, [f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)']))
