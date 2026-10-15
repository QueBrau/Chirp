"""Migration 0047 preserves unresolved socket evidence and enforces finite slots."""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.test_c436_email_operations_migration import _fetch, _insert_rows, _table_acl
from tests.test_c436_external_operations_migration import (
    _admin_execute, _alembic, _database_of, _probe, _swap_database,
)


async def _insert_ws(url: str, owner_id: uuid.UUID, *, slot: int = 0, state: str = "running"):
    connection_id, owner_token = uuid.uuid4(), uuid.uuid4()
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(
                "INSERT INTO account_ws_connections (id, user_id, slot, owner_token, state) "
                "VALUES (:id, :user_id, :slot, :owner_token, :state)"
            ), {"id": connection_id, "user_id": owner_id, "slot": slot,
                "owner_token": owner_token, "state": state})
    finally:
        await engine.dispose()
    return connection_id, owner_token


@pytest.mark.parametrize("state", ["running", "uncertain"])
def test_0047_bounds_slots_and_refuses_to_discard_unresolved_connections(state):
    requested = os.environ.get(
        "TEST_DATABASE_URL", "postgresql+asyncpg://chirp:chirp@localhost:5432/chirp_test"
    )
    admin_url = _swap_database(requested, "postgres")
    db_name = f"{_database_of(requested)}_c436ws_{uuid.uuid4().hex[:8]}"
    url = _swap_database(requested, db_name)
    try:
        asyncio.run(_probe(admin_url))
    except Exception:
        pytest.skip("local PostgreSQL required")

    original = os.environ.get("DATABASE_URL")
    asyncio.run(_admin_execute(admin_url, [
        f'CREATE DATABASE "{db_name}" ENCODING \'UTF8\' TEMPLATE template0',
    ]))
    owner_id, provider_operation_id = uuid.uuid4(), uuid.uuid4()
    try:
        _alembic(url, "0046")
        asyncio.run(_insert_rows(url, owner_id, provider_operation_id))
        prior_acl = asyncio.run(_table_acl(url))
        assert prior_acl is not None and "=r/" in prior_acl
        prior_provider = asyncio.run(_fetch(url,
            "SELECT id, actor_user_id, kind, effect_key, owner_token, state "
            "FROM account_external_operations"
        ))
        _alembic(url, "0047")
        assert asyncio.run(_table_acl(url)) == prior_acl
        assert asyncio.run(_fetch(url, "SELECT count(*) FROM account_ws_connections")) == [(0,)]
        connection_id, owner_token = asyncio.run(_insert_ws(url, owner_id, state=state))

        for bad_slot in (-1, 32):
            with pytest.raises(DBAPIError, match="ck_account_ws_connection_slot"):
                asyncio.run(_insert_ws(url, owner_id, slot=bad_slot))
        with pytest.raises(DBAPIError, match="uq_account_ws_connection_slot"):
            asyncio.run(_insert_ws(url, owner_id, slot=0))
        with pytest.raises(DBAPIError, match="ck_account_ws_connection_state"):
            asyncio.run(_insert_ws(url, owner_id, slot=1, state="finished"))

        with pytest.raises(DBAPIError, match="account_ws_connections_not_drained"):
            _alembic(url, "0046", down=True)
        assert asyncio.run(_fetch(url, "SELECT version_num FROM alembic_version")) == [("0047",)]
        assert asyncio.run(_fetch(url,
            "SELECT id, user_id, slot, owner_token, state FROM account_ws_connections"
        )) == [(connection_id, owner_id, 0, owner_token, state)]
        assert asyncio.run(_table_acl(url)) == prior_acl

        # Only this disposable test database is cleared. Production recovery must
        # independently establish that the exact connection cannot still run.
        async def clear_test_connection():
            engine = create_async_engine(url)
            try:
                async with engine.begin() as connection:
                    await connection.execute(text("DELETE FROM account_ws_connections"))
            finally:
                await engine.dispose()
        asyncio.run(clear_test_connection())
        _alembic(url, "0046", down=True)
        assert asyncio.run(_fetch(url, "SELECT to_regclass('account_ws_connections')")) == [(None,)]
        _alembic(url, "0047")
        assert asyncio.run(_fetch(url, "SELECT count(*) FROM account_ws_connections")) == [(0,)]
        assert asyncio.run(_fetch(url,
            "SELECT id, actor_user_id, kind, effect_key, owner_token, state "
            "FROM account_external_operations"
        )) == prior_provider
        assert asyncio.run(_table_acl(url)) == prior_acl
    finally:
        if original is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original
        from app.config import get_settings
        get_settings.cache_clear()
        asyncio.run(_admin_execute(admin_url, [f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)']))
