"""PostgreSQL upgrade and fail-safe downgrade checks for migration 0046."""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.test_c436_external_operations_migration import (
    BACKEND_DIR,
    _admin_execute,
    _alembic,
    _database_of,
    _probe,
    _swap_database,
)


async def _fetch(url: str, sql: str, params: dict[str, object] | None = None):
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            return (await connection.execute(text(sql), params or {})).all()
    finally:
        await engine.dispose()


async def _migration_rows(url: str) -> tuple[str, int, int]:
    rows = await _fetch(url, "SELECT version_num FROM alembic_version")
    counts = await _fetch(url,
        "SELECT count(*) FILTER (WHERE kind LIKE 'media_%'), "
        "count(*) FILTER (WHERE kind = 'campus_verification_email') "
        "FROM account_external_operations",
    )
    return rows[0][0], counts[0][0], counts[0][1]


async def _table_acl(url: str):
    rows = await _fetch(url,
        "SELECT relacl::text FROM pg_class WHERE oid = 'account_external_operations'::regclass"
    )
    return rows[0][0]


async def _insert_rows(url: str, owner_id: uuid.UUID, operation_id: uuid.UUID) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            # This database is disposable. Seed a non-default ACL so preserving
            # privileges is tested against an actual grant, not NULL == NULL.
            await connection.execute(text(
                "GRANT SELECT ON account_external_operations TO PUBLIC"
            ))
            await connection.execute(text(
                "INSERT INTO users (id, firebase_uid, email, display_name, account_type) "
                "VALUES (:id, :uid, :email, 'Email migration owner', 'greek')"
            ), {"id": str(owner_id), "uid": f"uid-{owner_id}", "email": f"{owner_id}@example.edu"})
            await connection.execute(text(
                "INSERT INTO account_external_operations "
                "(id, actor_user_id, kind, effect_key, owner_token, state) "
                "VALUES (:id, :actor, 'media_finalize_post', :effect, :token, 'running')"
            ), {
                "id": str(operation_id), "actor": str(owner_id), "effect": "a" * 64,
                "token": str(uuid.uuid4()),
            })
    finally:
        await engine.dispose()


async def _insert_email_row(url: str, owner_id: uuid.UUID) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(
                "INSERT INTO account_external_operations "
                "(actor_user_id, kind, effect_key, owner_token, state) "
                "VALUES (:actor, 'campus_verification_email', :effect, :token, 'running')"
            ), {
                "actor": str(owner_id), "effect": "b" * 64,
                "token": str(uuid.uuid4()),
            })
    finally:
        await engine.dispose()


async def _delete_email_rows(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(
                "DELETE FROM account_external_operations WHERE kind = 'campus_verification_email'"
            ))
    finally:
        await engine.dispose()


@pytest.mark.parametrize("with_email_row", [False, True])
def test_migration_0046_widens_email_kind_and_downgrade_preserves_rows(with_email_row: bool) -> None:
    requested = os.environ.get(
        "TEST_DATABASE_URL", "postgresql+asyncpg://chirp:chirp@localhost:5432/chirp_test"
    )
    base = _database_of(requested)
    admin_url = _swap_database(requested, "postgres")
    db_name = f"{base}_c436email_{uuid.uuid4().hex[:8]}"
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
    owner_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    try:
        _alembic(url, "0045")
        assert asyncio.run(_migration_rows(url)) == ("0045", 0, 0)
        asyncio.run(_insert_rows(url, owner_id, operation_id))
        acl_before = asyncio.run(_table_acl(url))
        assert acl_before is not None and "=r/" in acl_before

        _alembic(url, "0046")
        assert asyncio.run(_table_acl(url)) == acl_before
        if with_email_row:
            asyncio.run(_insert_email_row(url, owner_id))
        assert asyncio.run(_migration_rows(url)) == (
            "0046", 1, int(with_email_row),
        )

        if with_email_row:
            # PostgreSQL and Alembic must roll back the failed narrower constraint
            # atomically; neither the version nor the retained registry data changes.
            with pytest.raises(DBAPIError, match="ck_account_external_operation_kind"):
                _alembic(url, "0045", down=True)
            assert asyncio.run(_migration_rows(url)) == ("0046", 1, 1)
            assert asyncio.run(_table_acl(url)) == acl_before
            asyncio.run(_delete_email_rows(url))

        _alembic(url, "0045", down=True)
        assert asyncio.run(_migration_rows(url)) == ("0045", 1, 0)
        _alembic(url, "0046")
        assert asyncio.run(_migration_rows(url)) == ("0046", 1, 0)
        assert asyncio.run(_table_acl(url)) == acl_before
        # Existing media journal data remains visible through both directions.
        assert asyncio.run(_fetch(url,
            "SELECT kind, effect_key FROM account_external_operations WHERE id = :id",
            {"id": str(operation_id)},
        )) == [("media_finalize_post", "a" * 64)]
    finally:
        if original is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original
        from app.config import get_settings

        get_settings.cache_clear()
        asyncio.run(_admin_execute(admin_url, [f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)']))
