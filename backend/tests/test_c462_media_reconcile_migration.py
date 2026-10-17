"""Migration 0051 widens the operation registry without losing prior evidence."""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from tests.test_c436_external_operations_migration import (
    _admin_execute,
    _alembic,
    _database_of,
    _probe,
    _swap_database,
)


async def _seed_prior_operation(url: str, *, owner_id: uuid.UUID, row_id: uuid.UUID) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO users (id, firebase_uid, email, display_name, account_type) "
                "VALUES (:id, :uid, :email, 'Migration owner', 'greek')"
            ), {"id": str(owner_id), "uid": f"uid-{owner_id}", "email": f"{owner_id}@example.edu"})
            await conn.execute(text(
                "INSERT INTO account_external_operations "
                "(id, actor_user_id, kind, effect_key, owner_token, state) "
                "VALUES (:id, :user_id, :kind, :effect, :token, 'running')"
            ), {
                "id": str(row_id), "user_id": str(owner_id), "kind": "payment_request",
                "effect": "a" * 64, "token": str(uuid.uuid4()),
            })
    finally:
        await engine.dispose()


async def _insert_media_operation(url: str, *, owner_id: uuid.UUID, row_id: uuid.UUID) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO account_external_operations "
                "(id, actor_user_id, kind, effect_key, owner_token, state) "
                "VALUES (:id, :user_id, 'media_reconcile_post', :effect, :token, 'running')"
            ), {
                "id": str(row_id), "user_id": str(owner_id),
                "effect": "b" * 64, "token": str(uuid.uuid4()),
            })
    finally:
        await engine.dispose()


async def _delete_media_operation(url: str, row_id: uuid.UUID) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(
                "DELETE FROM account_external_operations WHERE id = :id"
            ), {"id": str(row_id)})
    finally:
        await engine.dispose()


async def _rows(url: str):
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return list((await conn.execute(text(
                "SELECT kind FROM account_external_operations ORDER BY kind"
            ))).scalars())
    finally:
        await engine.dispose()


def test_0051_widens_kind_and_refuses_lossy_downgrade() -> None:
    requested = os.environ.get(
        "TEST_DATABASE_URL", "postgresql+asyncpg://chirp:chirp@localhost:5432/chirp_test"
    )
    database_name = f"{_database_of(requested)}_c462_{uuid.uuid4().hex[:8]}"
    database_url = _swap_database(requested, database_name)
    admin_url = _swap_database(requested, "postgres")
    try:
        asyncio.run(_probe(admin_url))
    except Exception:
        pytest.skip("postgres not available — local PostgreSQL required")

    prior_database_url = os.environ.get("DATABASE_URL")
    owner_id, prior_id, media_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    try:
        asyncio.run(_admin_execute(admin_url, [
            f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)',
            f'CREATE DATABASE "{database_name}" ENCODING \'UTF8\' TEMPLATE template0',
        ]))
        _alembic(database_url, "0050")
        asyncio.run(_seed_prior_operation(database_url, owner_id=owner_id, row_id=prior_id))
        _alembic(database_url, "0051")
        asyncio.run(_insert_media_operation(database_url, owner_id=owner_id, row_id=media_id))

        assert asyncio.run(_rows(database_url)) == ["media_reconcile_post", "payment_request"]
        with pytest.raises(DBAPIError, match="media_reconcile_operations_not_empty"):
            _alembic(database_url, "0050", down=True)
        assert asyncio.run(_rows(database_url)) == ["media_reconcile_post", "payment_request"]

        asyncio.run(_delete_media_operation(database_url, media_id))
        _alembic(database_url, "0050", down=True)
        assert asyncio.run(_rows(database_url)) == ["payment_request"]
        _alembic(database_url, "0051")
        assert asyncio.run(_rows(database_url)) == ["payment_request"]
    finally:
        if prior_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prior_database_url
        get_settings.cache_clear()
        asyncio.run(_admin_execute(admin_url, [
            f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)',
        ]))
