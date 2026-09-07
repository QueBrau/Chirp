"""Migration 0032 against a moderation_actions table that already holds rows.

The c237/c308 shape, applied to a CHECK widening: conftest builds every suite database
with upgrade(head) on an EMPTY table, so "0032 preserves existing audit rows" and
"downgrade refuses while widened rows exist" are statements no ordinary test ever makes
about real data. This file builds a throwaway database, walks it to 0031, SEEDS an
old-shape row, and only then runs 0032 — the seeded row is asserted to exist before the
upgrade so that every later assertion is about a populated table.

WHAT IT PINS, in order
  1. At 0031 the constraint NAMES are the ones the database actually has —
     moderation_actions_action_check (auto-named by 0011, kept by 0017) and
     ck_moderation_actions_target_type (recreated by 0022). 0017's docstring records how a
     DROP ... IF EXISTS against the wrong name "succeeds" while dropping nothing; pinning
     the names here means a future rename that breaks 0032 fails HERE, not in prod.
  2. At 0031 a chapter-target row is REFUSED. Without this, "it inserts after 0032"
     would also hold if the constraints had never existed.
  3. After 0032: the old row survives byte-for-byte, a chapter-target row inserts, the
     constraint names are unchanged (no orphaned duplicate left behind), and an invalid
     action is still refused (the re-added constraint is actually in force).
  4. Downgrade with a widened row present REFUSES — loudly, before any DDL — and leaves
     rows and constraints exactly as they were. Deleting audit rows is not something a
     rollback may do silently on this table (0011: append-only by design).
  5. Downgrade with only old-shape rows succeeds, the old row survives, and the chapter
     target is refused again. Upgrade a second time is idempotent.

Mechanics follow test_c308_migration_backfill.py's scratch-database harness verbatim,
including the teardown that must never mask an assertion failure.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

BACKEND_DIR = Path(__file__).resolve().parents[1]

EXPECTED_CONSTRAINTS = {"moderation_actions_action_check", "ck_moderation_actions_target_type"}


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


async def _seed_actor_and_old_row(url: str) -> tuple[str, str]:
    """One user (the FK moderation_actions.actor_id needs) and one OLD-shape audit row:
    a resolve_report against a report — the exact shape 0017 introduced, so it is a row
    that has legitimately existed in prod since then."""
    engine = create_async_engine(url)
    actor_id, row_id = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO users (id, firebase_uid, email, display_name, account_type) "
                    "VALUES (:id, :uid, :email, 'Old Moderator', 'greek')"
                ),
                {"id": actor_id, "uid": f"uid-{actor_id}", "email": f"{actor_id}@example.edu"},
            )
            await conn.execute(
                text(
                    "INSERT INTO moderation_actions (id, actor_id, action, target_type, target_id, reason) "
                    "VALUES (:id, :actor, 'resolve_report', 'report', :target, 'seeded before 0032')"
                ),
                {"id": row_id, "actor": actor_id, "target": str(uuid.uuid4())},
            )
    finally:
        await engine.dispose()
    return actor_id, row_id


async def _insert_row(url: str, actor_id: str, action: str, target_type: str) -> str:
    engine = create_async_engine(url)
    row_id = str(uuid.uuid4())
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO moderation_actions (id, actor_id, action, target_type, target_id, reason) "
                    "VALUES (:id, :actor, :action, :tt, :target, 'c337 probe')"
                ),
                {"id": row_id, "actor": actor_id, "action": action, "tt": target_type, "target": str(uuid.uuid4())},
            )
    finally:
        await engine.dispose()
    return row_id


async def _rows(url: str) -> list[tuple[str, str, str, str]]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT id::text, action, target_type, reason FROM moderation_actions ORDER BY created_at, id")
            )
            return [tuple(r) for r in result.all()]
    finally:
        await engine.dispose()


async def _constraint_names(url: str) -> set[str]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = 'moderation_actions'::regclass AND contype = 'c'"
                )
            )
            return {r[0] for r in result.all()}
    finally:
        await engine.dispose()


async def _delete_rows(url: str, ids: list[str]) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            for row_id in ids:
                await conn.execute(text("DELETE FROM moderation_actions WHERE id = :id"), {"id": row_id})
    finally:
        await engine.dispose()


def _refused(coro) -> bool:
    try:
        asyncio.run(coro)
    except IntegrityError:
        return True
    except DBAPIError as exc:  # asyncpg surfaces CHECK violations through this too
        return "check" in str(exc).lower()
    return False


def test_0032_widens_the_audit_constraints_around_existing_rows() -> None:
    requested = os.environ.get(
        "TEST_DATABASE_URL", "postgresql+asyncpg://chirp:chirp@localhost:5432/chirp_test"
    )
    base = _database_of(requested)
    admin_url = _swap_database(requested, "postgres")
    db_name = f"{base}_c337widen_{uuid.uuid4().hex[:8]}"
    url = _swap_database(requested, db_name)

    try:
        asyncio.run(_probe(admin_url))
    except Exception:
        pytest.skip("postgres not available")

    original = os.environ.get("DATABASE_URL")
    asyncio.run(
        _admin_execute(
            admin_url,
            [f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)', f'CREATE DATABASE "{db_name}"'],
        )
    )
    try:
        _alembic(url, "0031")

        # 1. The names 0032 will DROP are the names the database actually has.
        assert asyncio.run(_constraint_names(url)) == EXPECTED_CONSTRAINTS, (
            "constraint names at 0031 drifted from what 0032 drops — 0017's trap"
        )

        actor_id, old_row = asyncio.run(_seed_actor_and_old_row(url))
        # THE GUARD (c237): the table holds a row before the migration runs.
        assert [r[0] for r in asyncio.run(_rows(url))] == [old_row], (
            "seed failed: 0032 would be proven against an empty table"
        )

        # 2. The pre-state is discriminating: a chapter target is refused at 0031.
        assert _refused(_insert_row(url, actor_id, "approve_chapter", "chapter")), (
            "a chapter-target row inserted at 0031 — the constraints this migration "
            "widens are not in force, so nothing below would prove anything"
        )

        _alembic(url, "0032")

        # 3. Old row intact, new shape admitted, names unchanged, constraint in force.
        assert asyncio.run(_rows(url)) == [(old_row, "resolve_report", "report", "seeded before 0032")]
        approve_row = asyncio.run(_insert_row(url, actor_id, "approve_chapter", "chapter"))
        revoke_row = asyncio.run(_insert_row(url, actor_id, "revoke_chapter", "chapter"))
        assert [r[0] for r in asyncio.run(_rows(url))] == [old_row, approve_row, revoke_row]
        assert asyncio.run(_constraint_names(url)) == EXPECTED_CONSTRAINTS, (
            "0032 left a differently-named duplicate or dropped a constraint outright"
        )
        assert _refused(_insert_row(url, actor_id, "bless_chapter", "chapter")), (
            "an unknown action inserted after 0032 — the re-added CHECK is not in force"
        )
        assert _refused(_insert_row(url, actor_id, "approve_chapter", "campus")), (
            "an unknown target_type inserted after 0032 — the re-added CHECK is not in force"
        )

        # 4. Downgrade refuses while widened rows exist, and changes nothing.
        with pytest.raises(Exception, match="c337"):
            _alembic(url, "0031", down=True)
        assert [r[0] for r in asyncio.run(_rows(url))] == [old_row, approve_row, revoke_row], (
            "the refused downgrade deleted audit rows"
        )
        still_widened = asyncio.run(_insert_row(url, actor_id, "approve_chapter", "chapter"))
        assert asyncio.run(_constraint_names(url)) == EXPECTED_CONSTRAINTS

        # 5. With only old-shape rows left, the downgrade narrows cleanly.
        asyncio.run(_delete_rows(url, [approve_row, revoke_row, still_widened]))
        _alembic(url, "0031", down=True)
        assert asyncio.run(_rows(url)) == [(old_row, "resolve_report", "report", "seeded before 0032")]
        assert asyncio.run(_constraint_names(url)) == EXPECTED_CONSTRAINTS
        # Three probes, because the first alone is NOT discriminating: (approve_chapter,
        # chapter) is refused by EITHER narrowed constraint, so a downgrade that narrowed
        # only one half would still pass it (manager review finding on #233). The next
        # two each use an OLD value on one column so only the OTHER constraint can be the
        # refuser — delete either narrowing half of downgrade() and exactly one fails.
        assert _refused(_insert_row(url, actor_id, "approve_chapter", "chapter")), (
            "downgrade did not narrow target_type/action back"
        )
        assert _refused(_insert_row(url, actor_id, "resolve_report", "chapter")), (
            "downgrade did not narrow target_type back"
        )
        assert _refused(_insert_row(url, actor_id, "approve_chapter", "report")), (
            "downgrade did not narrow action back"
        )

        # Idempotent re-run.
        _alembic(url, "0032")
        asyncio.run(_insert_row(url, actor_id, "revoke_chapter", "chapter"))
    finally:
        if original is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original
        from app.config import get_settings

        get_settings.cache_clear()
        try:
            asyncio.run(
                _admin_execute(admin_url, [f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'])
            )
        except Exception:
            pass
