"""Migration 0039 (delivery_outbox) round-trips cleanly (board card c356).

Brand-new, empty table -- nothing pre-existing to backfill, unlike 0037/0344's
duplicate-DM fixture -- so this is a lighter upgrade/downgrade round trip using the
same own-scratch-database-and-manual-alembic-stepping harness as
tests/test_c344_dm_key_migration.py:_alembic (own comment there: the shared
session-scoped `migrated_db` fixture always migrates straight to head, so it cannot
observe the table's absence one revision earlier).
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.test_c344_dm_key_migration import (
    _admin_execute,
    _alembic,
    _database_of,
    _probe,
    _swap_database,
)


async def _drop_db_with_retry(admin_url: str, db_name: str) -> None:
    """DROP DATABASE ... WITH (FORCE) can transiently lose a race against Postgres's
    own autovacuum launcher opening a worker connection to a database that just went
    through heavy DDL churn (this test's create-table/drop-index/drop-table/create-table
    sequence, more churn than test_c344's single forward migration) -- autovacuum's
    worker runs as the bootstrap superuser, not this test's role, so FORCE cannot
    terminate it without pg_signal_backend membership this role does not have. A short
    retry is the same tolerance test_c344's harness gets implicitly by doing less DDL;
    it is not masking a c356 code defect, only Postgres's own background worker timing.
    """
    last: Exception | None = None
    for attempt in range(5):
        try:
            await _admin_execute(admin_url, [f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'])
            return
        except Exception as exc:  # noqa: BLE001 - retry any transient drop failure
            last = exc
            await asyncio.sleep(0.3 * (attempt + 1))
    if last is not None:
        raise last


async def _table_exists(url: str, table: str) -> bool:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT to_regclass(:t)"), {"t": table})
            return result.scalar_one() is not None
    finally:
        await engine.dispose()


async def _index_definition(url: str, index: str) -> str | None:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = :i"), {"i": index}
            )
            return result.scalar_one_or_none()
    finally:
        await engine.dispose()


async def _insert_kind(url: str, kind: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO delivery_outbox (kind, recipient_ids, payload) "
                    "VALUES (:kind, '{}', '{}'::jsonb)"
                ),
                {"kind": kind},
            )
    finally:
        await engine.dispose()


def test_migration_0039_upgrade_downgrade_round_trip() -> None:
    """upgrade to 0039 against a fresh 0037 database: table+CHECK+partial index all
    appear; a bogus kind is rejected while 'message'/'poll' are accepted; downgrade
    to 0037 removes the table entirely; upgrading again is idempotent.
    """
    requested = os.environ.get(
        "TEST_DATABASE_URL", "postgresql+asyncpg://chirp:chirp@localhost:5432/chirp_test"
    )
    base = _database_of(requested)
    admin_url = _swap_database(requested, "postgres")
    db_name = f"{base}_c356outbox_{uuid.uuid4().hex[:8]}"
    url = _swap_database(requested, db_name)

    try:
        asyncio.run(_probe(admin_url))
    except Exception:
        pytest.skip("postgres not available — docker compose up db")

    original = os.environ.get("DATABASE_URL")
    asyncio.run(
        _admin_execute(
            admin_url,
            [f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)', f'CREATE DATABASE "{db_name}"'],
        )
    )
    try:
        _alembic(url, "0037")
        assert asyncio.run(_table_exists(url, "delivery_outbox")) is False, (
            "delivery_outbox must not exist before 0039 runs"
        )

        # THE HEADLINE: upgrading past 0039 creates the table with its CHECK and
        # partial index, against a database that has never seen this table before.
        _alembic(url, "0039")

        assert asyncio.run(_table_exists(url, "delivery_outbox")) is True

        index_def = asyncio.run(_index_definition(url, "idx_delivery_outbox_pending"))
        assert index_def is not None, "idx_delivery_outbox_pending is missing"
        assert "delivered_at IS NULL" in index_def
        assert "dead_at IS NULL" in index_def

        # kind='message' and kind='poll' both succeed...
        asyncio.run(_insert_kind(url, "message"))
        asyncio.run(_insert_kind(url, "poll"))

        # ...but a bogus kind is rejected by the CHECK constraint, not silently accepted.
        with pytest.raises(DBAPIError, match="ck_delivery_outbox_kind"):
            asyncio.run(_insert_kind(url, "bogus"))

        # DOWN: the table is gone entirely (nothing to preserve, it is brand new).
        _alembic(url, "0037", down=True)
        assert asyncio.run(_table_exists(url, "delivery_outbox")) is False

        # UP AGAIN: idempotent round trip.
        _alembic(url, "0039")
        assert asyncio.run(_table_exists(url, "delivery_outbox")) is True
    finally:
        if original is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original
        from app.config import get_settings

        get_settings.cache_clear()
        asyncio.run(_drop_db_with_retry(admin_url, db_name))
