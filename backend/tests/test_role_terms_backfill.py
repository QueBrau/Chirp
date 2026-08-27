"""Migration 0021's backfill: one open role_term per pre-existing membership.

(d) of board card c83's falsify-first test list. This needs a database with
memberships already present BEFORE 0021 runs — the shared, session-scoped
`migrated_db` fixture used everywhere else in this suite has already fast-forwarded
straight to head before any test's data exists, so it can never observe the backfill
actually happening (role_terms is created empty and stays that way for memberships
created afterward through the API, which get their initial term from
app.services.role_term_service.open_initial_term instead — see test_role_terms.py).

So this file builds its OWN throwaway database: migrate to 0019 (the revision
immediately before 0021), insert memberships directly via raw SQL — bypassing the
API entirely, the way real rows looked the instant before this migration ran on a
live database — then upgrade to 0021 and assert the backfill produced exactly what
the card asked for.

PLAIN (non-async) test function, deliberately: alembic's `command.upgrade` calls
`asyncio.run(...)` internally (see alembic/env.py), which raises
"asyncio.run() cannot be called from a running event loop" if invoked from inside
one of pytest-asyncio's auto-wrapped async test coroutines. Each `asyncio.run(...)`
call below opens and closes its own loop instead, sequentially, exactly the way
tests/conftest.py's (also synchronous) `database_url`/`migrated_db` fixtures do it.

Deliberately does not use any fixture from conftest.py: it manages its own database
lifecycle end to end (including restoring DATABASE_URL / the settings cache
afterward) so it cannot leak state into the session-scoped `migrated_db` fixture
every other test file in this suite depends on.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_TEST_DATABASE_URL = "postgresql+asyncpg://chirp:chirp@localhost:5432/chirp_test"


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


async def _seed_pre_migration_memberships(url: str) -> dict[str, str]:
    """Insert one membership per role directly via raw SQL, bypassing the API
    entirely — this is what real rows looked like the instant before 0021 ran on a
    live database. Returns {str(membership_id): role}."""
    engine = create_async_engine(url)
    membership_ids: dict[str, str] = {}
    try:
        async with engine.begin() as conn:
            campus_id = (
                await conn.execute(
                    text(
                        "INSERT INTO campuses (name, slug) VALUES ('Backfill U', :slug) "
                        "RETURNING id"
                    ),
                    {"slug": f"backfill-u-{uuid.uuid4().hex[:8]}"},
                )
            ).scalar_one()
            chapter_id = (
                await conn.execute(
                    text(
                        "INSERT INTO chapters (campus_id, org_name) VALUES (:campus, 'Backfill Chi') "
                        "RETURNING id"
                    ),
                    {"campus": campus_id},
                )
            ).scalar_one()
            for i, role in enumerate(["president", "treasurer", "member"]):
                user_id = (
                    await conn.execute(
                        text(
                            "INSERT INTO users (firebase_uid, email, display_name, account_type) "
                            "VALUES (:fu, :email, :dn, 'greek') RETURNING id"
                        ),
                        {
                            "fu": f"backfill-uid-{i}-{uuid.uuid4().hex[:8]}",
                            "email": f"backfill{i}-{uuid.uuid4().hex[:8]}@example.edu",
                            "dn": f"Backfill {role.title()}",
                        },
                    )
                ).scalar_one()
                membership_id = (
                    await conn.execute(
                        text(
                            "INSERT INTO memberships (user_id, chapter_id, role) "
                            "VALUES (:user, :chapter, :role) RETURNING id"
                        ),
                        {"user": user_id, "chapter": chapter_id, "role": role},
                    )
                ).scalar_one()
                membership_ids[str(membership_id)] = role
    finally:
        await engine.dispose()
    return membership_ids


async def _fetch_role_terms(url: str) -> list[object]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            # This database has nothing else in it, so every role_terms row is one
            # of ours — no filtering needed to isolate the backfill's output.
            result = await conn.execute(
                text(
                    "SELECT membership_id, role, started_at, ended_at, changed_by "
                    "FROM role_terms"
                )
            )
            return result.all()
    finally:
        await engine.dispose()


def _migrate_to(url: str, revision: str) -> None:
    """Point app settings at `url` and run alembic up to `revision` (in-process,
    same mechanism tests/conftest.py's migrated_db fixture uses)."""
    os.environ["DATABASE_URL"] = url
    from app.config import get_settings

    get_settings.cache_clear()

    from alembic import command
    from alembic.config import Config as AlembicConfig

    cfg = AlembicConfig(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(cfg, revision)


def test_0021_backfills_exactly_one_open_term_per_existing_membership() -> None:
    requested = os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
    base = _database_of(requested)
    admin_url = _swap_database(requested, "postgres")
    db_name = f"{base}_c83backfill_{uuid.uuid4().hex[:8]}"
    url = _swap_database(requested, db_name)

    try:
        asyncio.run(_probe(admin_url))
    except Exception:
        pytest.skip("postgres not available — docker compose up db")

    original_database_url = os.environ.get("DATABASE_URL")

    asyncio.run(
        _admin_execute(
            admin_url,
            [f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)', f'CREATE DATABASE "{db_name}"'],
        )
    )
    try:
        # Pre-0021 state: memberships exist, role_terms does not.
        _migrate_to(url, "0019")

        membership_ids = asyncio.run(_seed_pre_migration_memberships(url))

        before_migration = datetime.now(timezone.utc)
        _migrate_to(url, "0021")
        after_migration = datetime.now(timezone.utc)

        rows = asyncio.run(_fetch_role_terms(url))

        assert len(rows) == len(membership_ids), (
            "exactly one backfilled row per pre-existing membership, no more, no fewer"
        )
        seen = {str(row.membership_id) for row in rows}
        assert seen == set(membership_ids), "every membership got a row, and no stray rows appeared"
        for row in rows:
            expected_role = membership_ids[str(row.membership_id)]
            assert row.role == expected_role, "backfilled role matches memberships.role"
            assert row.ended_at is None, "backfilled term is OPEN"
            assert row.changed_by is None, "no acting user for a data migration"
            assert before_migration <= row.started_at <= after_migration, (
                f"started_at ({row.started_at}) must be stamped at MIGRATION time, "
                "not backdated to joined_at or any other value"
            )
    finally:
        # Best effort, matching tests/conftest.py's own database_url fixture teardown
        # (same comment, same reasoning): WITH (FORCE) terminates other backends still
        # connected to this database, which needs pg_signal_backend/superuser UNLESS
        # every such backend belongs to this same role. Observed flaking here when
        # autovacuum briefly attaches to the freshly-created throwaway database right
        # after the DDL-heavy migrate-seed-migrate sequence above — that backend is
        # not one plain `chirp` can terminate. A failed drop leaks one throwaway
        # database (named distinctively enough, `_c83backfill_<hex>`, to find and
        # reap by hand); failing THIS TEST for a housekeeping race would misreport a
        # passing backfill as broken, which is worse.
        try:
            asyncio.run(
                _admin_execute(admin_url, [f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'])
            )
        except Exception:
            pass
        if original_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original_database_url
        from app.config import get_settings

        get_settings.cache_clear()
