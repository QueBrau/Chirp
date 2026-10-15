"""0048 preserves queue data/ACLs and refuses to discard unresolved ownership."""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app import models

from tests.test_c436_email_operations_migration import _fetch, _insert_rows, _table_acl
from tests.test_c436_external_operations_migration import (
    _admin_execute, _alembic, _database_of, _probe, _swap_database,
)
from tests.test_c436_ws_connections_migration import _insert_ws


async def _execute(url, sql, params=None):
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(sql), params or {})
    finally:
        await engine.dispose()


async def _attempt(url, owner, source, *, slot=0, state="running"):
    row_id, token = uuid.uuid4(), uuid.uuid4()
    await _execute(url,
        "INSERT INTO account_delivery_attempts (id,user_id,slot,outbox_id,owner_token,state) "
        "VALUES (:id,:user_id,:slot,:outbox_id,:owner_token,:state)",
        {"id": row_id, "user_id": owner, "slot": slot, "outbox_id": source,
         "owner_token": token, "state": state},
    )
    return row_id, token


QUEUE_COLUMNS = "id,kind,recipient_ids,payload,attempts,next_attempt_at,created_at,dead_at,last_error"


async def _queue_acl(url):
    return await _fetch(url,
        "SELECT c.relacl::text, a.attacl::text FROM pg_class c JOIN pg_attribute a "
        "ON a.attrelid=c.oid WHERE c.oid='delivery_outbox'::regclass AND a.attname='attempts'",
    )


async def _legacy_orm_roundtrip(url, owner):
    """Exercise current ORM against old schema, without the dormant column."""
    engine = create_async_engine(url)
    row_id = uuid.uuid4()
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            row = models.DeliveryOutbox(
                id=row_id, kind="poll", recipient_ids=[owner], payload={"type": "poll"},
            )
            session.add(row)
            await session.commit()
        async with AsyncSession(engine) as session:
            row = await session.get(models.DeliveryOutbox, row_id)
            assert row is not None and row.recipient_ids == [owner]
            row.attempts += 1
            await session.commit()
            loaded = await session.scalar(select(models.DeliveryOutbox).where(
                models.DeliveryOutbox.id == row_id,
            ))
            assert loaded.attempts == 1
            await session.delete(loaded)
            await session.commit()
    finally:
        await engine.dispose()


@pytest.mark.parametrize("blocking", ["running", "uncertain", "claim_only"])
def test_0048_preserves_prior_data_permissions_and_blocking_evidence(blocking):
    requested = os.environ.get(
        "TEST_DATABASE_URL", "postgresql+asyncpg://chirp:chirp@localhost:5432/chirp_test"
    )
    admin = _swap_database(requested, "postgres")
    database = f"{_database_of(requested)}_c436delivery_{uuid.uuid4().hex[:8]}"
    url = _swap_database(requested, database)
    try:
        asyncio.run(_probe(admin))
    except Exception:
        pytest.skip("local PostgreSQL required")
    original = os.environ.get("DATABASE_URL")
    asyncio.run(_admin_execute(admin, [
        f'CREATE DATABASE "{database}" ENCODING \'UTF8\' TEMPLATE template0',
    ]))
    owner, provider, source, coalesced = (uuid.uuid4() for _ in range(4))
    try:
        _alembic(url, "0047")
        asyncio.run(_insert_rows(url, owner, provider))
        asyncio.run(_legacy_orm_roundtrip(url, owner))
        asyncio.run(_insert_ws(url, owner, state="uncertain"))
        asyncio.run(_execute(url,
            "INSERT INTO delivery_outbox (id,kind,recipient_ids,payload,attempts,last_error) "
            "VALUES (:id,'poll',ARRAY[CAST(:owner AS uuid)],'{\"type\":\"poll\"}'::jsonb,2,'publish_failed')",
            {"id": source, "owner": owner},
        ))
        # Non-default ACLs in a disposable database make privilege preservation
        # observable. The migration itself must issue no grants.
        asyncio.run(_execute(url, "GRANT SELECT ON delivery_outbox TO PUBLIC"))
        asyncio.run(_execute(url, "GRANT UPDATE (attempts) ON delivery_outbox TO PUBLIC"))
        prior_acl = asyncio.run(_queue_acl(url))
        assert prior_acl[0][0] and prior_acl[0][1]
        provider_acl = asyncio.run(_table_acl(url))
        prior_queue = asyncio.run(_fetch(url, f"SELECT {QUEUE_COLUMNS} FROM delivery_outbox"))
        prior_ws = asyncio.run(_fetch(url, "SELECT * FROM account_ws_connections"))
        prior_provider = asyncio.run(_fetch(url, "SELECT * FROM account_external_operations"))

        _alembic(url, "0048")
        assert asyncio.run(_queue_acl(url)) == prior_acl
        assert asyncio.run(_table_acl(url)) == provider_acl
        assert asyncio.run(_fetch(url, f"SELECT {QUEUE_COLUMNS} FROM delivery_outbox")) == prior_queue
        assert asyncio.run(_fetch(url, "SELECT claim_token FROM delivery_outbox")) == [(None,)]
        assert asyncio.run(_fetch(url, "SELECT count(*) FROM account_delivery_attempts")) == [(0,)]

        token = uuid.uuid4()
        asyncio.run(_execute(url, "UPDATE delivery_outbox SET claim_token=:token WHERE id=:id",
                             {"token": token, "id": source}))
        if blocking != "claim_only":
            asyncio.run(_execute(url,
                "INSERT INTO delivery_outbox (id,kind,recipient_ids,payload) "
                "VALUES (:id,'poll',ARRAY[CAST(:owner AS uuid)],'{}'::jsonb)",
                {"id": coalesced, "owner": owner},
            ))
            asyncio.run(_attempt(url, owner, coalesced, state=blocking))
            # Actual source removal cannot cascade away the attempt.
            asyncio.run(_execute(url, "DELETE FROM delivery_outbox WHERE id=:id", {"id": coalesced}))
            for invalid in (-1, 32):
                with pytest.raises(DBAPIError, match="ck_account_delivery_attempt_slot"):
                    asyncio.run(_attempt(url, owner, uuid.uuid4(), slot=invalid))
            with pytest.raises(DBAPIError, match="uq_account_delivery_attempt_slot"):
                asyncio.run(_attempt(url, owner, uuid.uuid4(), slot=0))
            with pytest.raises(DBAPIError, match="uq_account_delivery_attempt_recipient"):
                asyncio.run(_attempt(url, owner, coalesced, slot=1))
            with pytest.raises(DBAPIError, match="ck_account_delivery_attempt_state"):
                asyncio.run(_attempt(url, owner, uuid.uuid4(), slot=1, state="finished"))
            with pytest.raises(DBAPIError, match="foreign key"):
                asyncio.run(_attempt(url, uuid.uuid4(), uuid.uuid4()))
        attempts = asyncio.run(_fetch(url, "SELECT * FROM account_delivery_attempts"))
        assert len(attempts) == (0 if blocking == "claim_only" else 1)

        with pytest.raises(DBAPIError, match="account_delivery_attempts_not_drained"):
            _alembic(url, "0047", down=True)
        assert asyncio.run(_fetch(url, "SELECT version_num FROM alembic_version")) == [("0048",)]
        assert asyncio.run(_fetch(url, "SELECT * FROM account_delivery_attempts")) == attempts
        assert asyncio.run(_fetch(url, "SELECT claim_token FROM delivery_outbox")) == [(token,)]
        assert asyncio.run(_queue_acl(url)) == prior_acl

        # Clear only this disposable test's evidence; source exposes no general
        # expiry, force-release or downgrade cleanup for live attempts.
        asyncio.run(_execute(url, "DELETE FROM account_delivery_attempts"))
        asyncio.run(_execute(url, "UPDATE delivery_outbox SET claim_token=NULL"))
        _alembic(url, "0047", down=True)
        asyncio.run(_legacy_orm_roundtrip(url, owner))
        assert asyncio.run(_fetch(url, "SELECT to_regclass('account_delivery_attempts')")) == [(None,)]
        assert asyncio.run(_fetch(url,
            "SELECT count(*) FROM information_schema.columns WHERE table_name='delivery_outbox' "
            "AND column_name='claim_token'",
        )) == [(0,)]
        _alembic(url, "0048")
        assert asyncio.run(_queue_acl(url)) == prior_acl
        assert asyncio.run(_table_acl(url)) == provider_acl
        assert asyncio.run(_fetch(url, f"SELECT {QUEUE_COLUMNS} FROM delivery_outbox")) == prior_queue
        assert asyncio.run(_fetch(url, "SELECT * FROM account_ws_connections")) == prior_ws
        assert asyncio.run(_fetch(url, "SELECT * FROM account_external_operations")) == prior_provider
    finally:
        if original is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original
        from app.config import get_settings
        get_settings.cache_clear()
        asyncio.run(_admin_execute(admin, [f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)']))
