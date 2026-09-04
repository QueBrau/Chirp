"""The 0031 backfill itself, against a database that already holds a chapter.

WHY THIS FILE EXISTS, AND IT IS A REVIEW FINDING AGAINST MY OWN PR. The c308 suite
tested everything the backfill was FOR and nothing about the backfill. conftest builds
its schema with upgrade(head) on an empty database, so
`UPDATE chapters SET moderation_approved = true` runs against zero rows on every test
run — the statement executes, touches nothing, and reports success. chirps-23 proved it
the strong way: they inverted the backfill to `= false`, approving nothing at all, and
the entire suite stayed green 18/18.

That is the c237 vacuous-migration shape exactly, and I had walked past it while citing
c237 in the migration's own docstring. What I had done was run the check BY HAND on a
scratch database and write "proven on real rows" in the PR. It was proven — once, by me,
in a way no CI run repeats and no future refactor is answerable to.

The stakes make it worse than a missing test. The backfill IS the like-for-like promise:
get it wrong at deploy and every sitting officer of every real chapter silently loses
their report queue, which is precisely the outcome braul's ruling says must not happen,
and the moment it happens is the moment nobody is running the tests.

WHAT THIS PROVES AND WHAT IT DOES NOT, stated so the pair is not over-read the way the
factory docstring was. This file proves the migration produces the right VALUES: TRUE
for a chapter that predates it, FALSE for one created after. It does not exercise an
HTTP request — the app's session factory is bound to the suite's own database, not to
the throwaway one built here. The BEHAVIOUR of those values is what
test_c308_moderation_decoupled.py covers: TRUE reaches the queue, FALSE is refused.
Neither file is sufficient alone, and the chain only holds because both exist:

    migration writes TRUE for pre-existing chapters   (here)
    TRUE means the officer reaches the queue          (test_c308_moderation_decoupled)
    --------------------------------------------------------------------------
    therefore sitting officers keep exactly today's access across the deploy

Mechanics follow test_c279_block_provenance.py's seeded-database harness.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
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


async def _seed_pre_migration_chapter(url: str) -> str:
    """A campus and one chapter, created while the column does not exist yet.

    This is the row the whole file turns on: without it the backfill runs against an
    empty table and every assertion below would hold just as happily if the UPDATE were
    deleted outright.
    """
    engine = create_async_engine(url)
    campus_id, chapter_id = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO campuses (id, name, slug) VALUES (:id, :n, :s)"),
                {"id": campus_id, "n": "Backfill U", "s": f"backfill-{campus_id[:8]}"},
            )
            await conn.execute(
                text(
                    "INSERT INTO chapters (id, campus_id, org_name) "
                    "VALUES (:id, :campus, 'Chapter That Predates 0031')"
                ),
                {"id": chapter_id, "campus": campus_id},
            )
    finally:
        await engine.dispose()
    return chapter_id


async def _insert_chapter_post_migration(url: str) -> str:
    """A chapter created AFTER 0031, taking whatever the column's default gives it.

    Deliberately a raw INSERT that never names moderation_approved, because that is both
    the deploy-window case (old code, new schema) and the shape any future code path
    takes if it forgets the column. The default has to be what refuses, not the caller.
    """
    engine = create_async_engine(url)
    chapter_id = str(uuid.uuid4())
    try:
        async with engine.begin() as conn:
            campus_id = (
                await conn.execute(text("SELECT id FROM campuses LIMIT 1"))
            ).scalar_one()
            await conn.execute(
                text(
                    "INSERT INTO chapters (id, campus_id, org_name) "
                    "VALUES (:id, :campus, 'Chapter Founded After 0031')"
                ),
                {"id": chapter_id, "campus": campus_id},
            )
    finally:
        await engine.dispose()
    return chapter_id


async def _approved(url: str, chapter_id: str) -> bool:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT moderation_approved FROM chapters WHERE id = :id"),
                {"id": chapter_id},
            )
            return bool(result.scalar_one())
    finally:
        await engine.dispose()


async def _chapter_count(url: str) -> int:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT count(*) FROM chapters"))
            return int(result.scalar_one())
    finally:
        await engine.dispose()


async def _column_exists(url: str) -> bool:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'chapters' AND column_name = 'moderation_approved'"
                )
            )
            return result.first() is not None
    finally:
        await engine.dispose()


def test_0031_backfills_existing_chapters_and_refuses_new_ones() -> None:
    """up (pre-existing chapter approved, a later insert not), down (column gone), up
    again (idempotent) — against a table that already holds a chapter, never an empty one.
    """
    requested = os.environ.get(
        "TEST_DATABASE_URL", "postgresql+asyncpg://chirp:chirp@localhost:5432/chirp_test"
    )
    base = _database_of(requested)
    admin_url = _swap_database(requested, "postgres")
    db_name = f"{base}_c308backfill_{uuid.uuid4().hex[:8]}"
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
        _alembic(url, "0030")
        assert not asyncio.run(_column_exists(url)), "0030 must not already have the column"
        pre_existing = asyncio.run(_seed_pre_migration_chapter(url))

        # THE GUARD THAT MAKES THE REST MEAN ANYTHING (c237): the table must actually
        # hold the row before the backfill runs, or "it worked" is a statement about an
        # empty table — which is exactly how this gap survived review the first time.
        assert asyncio.run(_chapter_count(url)) == 1, (
            "seed failed: the backfill would be proven against an empty table"
        )

        _alembic(url, "0031")
        assert asyncio.run(_column_exists(url))

        # UP: the chapter that predates the migration is APPROVED. Invert the backfill
        # to `= false` and this line is the first thing that fails; before this file
        # existed, nothing did.
        assert asyncio.run(_approved(url, pre_existing)) is True, (
            "every chapter that existed at 0031 must be approved — this is the "
            "like-for-like promise, and its failure mode is every sitting officer "
            "silently losing the report queue at deploy"
        )

        # And the default refuses anything created afterwards, including by a raw INSERT
        # that never mentions the column — the deploy-window case.
        founded_after = asyncio.run(_insert_chapter_post_migration(url))
        assert asyncio.run(_approved(url, founded_after)) is False, (
            "a chapter created after 0031 must start unapproved, or founding still "
            "mints campus moderation and c308 bought nothing"
        )

        # DOWN: column cleanly gone, rows survive.
        _alembic(url, "0030", down=True)
        assert not asyncio.run(_column_exists(url))

        # UP AGAIN: idempotent, and the re-run re-approves BOTH rows, since by then both
        # predate the migration. That is correct rather than surprising — it is what
        # "everything that exists when this runs is established" means — but it is worth
        # pinning, because it is also why the ungating card cannot rely on this migration
        # being re-runnable to keep self-made chapters unapproved.
        _alembic(url, "0031")
        assert asyncio.run(_approved(url, pre_existing)) is True
        assert asyncio.run(_approved(url, founded_after)) is True
    finally:
        if original is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original
        from app.config import get_settings

        get_settings.cache_clear()
        # Swallow teardown failures on purpose. The first falsification run of this file
        # raised InsufficientPrivilegeError from DROP ... WITH (FORCE) — the chirp role
        # cannot signal another role's backends — and that error REPLACED the assertion
        # failure it was cleaning up after, so the run reported a privilege problem
        # instead of the backfill being wrong. A teardown that can mask the finding is
        # worse than a leaked throwaway database.
        try:
            asyncio.run(
                _admin_execute(admin_url, [f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'])
            )
        except Exception:
            pass
