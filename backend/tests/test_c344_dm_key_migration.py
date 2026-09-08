"""Migration 0037 must survive PRE-EXISTING duplicate DM rows (board c344).

This card's whole premise is that duplicate DMs already exist: before this card,
POST /conversations had no dedup at all, so any real database this migration runs
against may already hold more than one Conversation row for the same participant
pair. A migration that backfills dm_key on every existing dm row unconditionally,
THEN creates the unique index, throws a duplicate-key error the instant it reaches
the first such pair — passing cleanly on an empty test database while being
guaranteed to fail the real one. This test builds that exact fixture: two
pre-existing duplicate rows for one pair, plus one ordinary non-duplicate DM, and
proves migration 0037 completes and backfills correctly against all three.

Own scratch database and manual alembic stepping (test_c279_block_provenance.py's
pattern) rather than the shared session-scoped `migrated_db` fixture, because that
fixture always migrates straight to head — there is no way to seed data at 0036
through it before 0037 has already run.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

BACKEND_DIR = Path(__file__).resolve().parents[1]
NIL_UUID = "00000000-0000-0000-0000-000000000000"


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


async def _seed_user(conn, user_id: str, name: str) -> None:
    await conn.execute(
        text(
            "INSERT INTO users (id, firebase_uid, email, display_name, account_type) "
            "VALUES (:id, :uid, :email, :name, 'greek')"
        ),
        {"id": user_id, "uid": f"uid-{user_id}", "email": f"{user_id}@example.edu", "name": name},
    )


async def _seed_dm(conn, conversation_id: str, member_ids: list[str], created_at: datetime) -> None:
    await conn.execute(
        text(
            "INSERT INTO conversations (id, chapter_id, kind, created_at) "
            "VALUES (:id, NULL, 'dm', :created_at)"
        ),
        {"id": conversation_id, "created_at": created_at},
    )
    for member_id in member_ids:
        await conn.execute(
            text(
                "INSERT INTO conversation_members (conversation_id, user_id, joined_at) "
                "VALUES (:cid, :uid, :created_at)"
            ),
            {"cid": conversation_id, "uid": member_id, "created_at": created_at},
        )


async def _seed_fixture(url: str) -> dict[str, str]:
    """Two pre-existing duplicate DM rows for (u1, u2), one normal DM for (u1, u3)."""
    engine = create_async_engine(url)
    ids = {name: str(uuid.uuid4()) for name in ("u1", "u2", "u3", "dup_early", "dup_late", "normal")}
    now = datetime.now(timezone.utc)
    try:
        async with engine.begin() as conn:
            await _seed_user(conn, ids["u1"], "U1")
            await _seed_user(conn, ids["u2"], "U2")
            await _seed_user(conn, ids["u3"], "U3")
            # The earlier duplicate — this is the one that must keep a dm_key.
            await _seed_dm(conn, ids["dup_early"], [ids["u1"], ids["u2"]], now)
            # A later duplicate of the SAME pair — must end up with dm_key NULL.
            await _seed_dm(
                conn, ids["dup_late"], [ids["u1"], ids["u2"]], now + timedelta(seconds=5)
            )
            # An unrelated, non-duplicate DM — must still get a correct key.
            await _seed_dm(conn, ids["normal"], [ids["u1"], ids["u3"]], now)
    finally:
        await engine.dispose()
    return ids


async def _dm_key_of(url: str, conversation_id: str) -> str | None:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT dm_key FROM conversations WHERE id = :id"), {"id": conversation_id}
            )
            return result.scalar_one()
    finally:
        await engine.dispose()


async def _index_definitions(url: str, table: str) -> dict[str, str]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = :t"),
                {"t": table},
            )
            return {row[0]: row[1] for row in result}
    finally:
        await engine.dispose()


def test_dm_key_backfill_survives_preexisting_duplicates() -> None:
    """upgrade to 0037 against a database that ALREADY holds a duplicate DM pair:
    completes without raising, keys the earliest duplicate, leaves the later
    duplicate NULL, keys the unrelated normal DM correctly, and leaves the
    partial unique index in place.
    """
    requested = os.environ.get(
        "TEST_DATABASE_URL", "postgresql+asyncpg://chirp:chirp@localhost:5432/chirp_test"
    )
    base = _database_of(requested)
    admin_url = _swap_database(requested, "postgres")
    db_name = f"{base}_c344dmkey_{uuid.uuid4().hex[:8]}"
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
        _alembic(url, "0036")
        ids = asyncio.run(_seed_fixture(url))

        # THE HEADLINE: this must not raise. A naive "backfill everything then
        # CREATE UNIQUE INDEX" migration throws a duplicate-key error exactly here.
        _alembic(url, "0037")

        early_key = asyncio.run(_dm_key_of(url, ids["dup_early"]))
        late_key = asyncio.run(_dm_key_of(url, ids["dup_late"]))
        normal_key = asyncio.run(_dm_key_of(url, ids["normal"]))

        assert early_key is not None, "the earliest duplicate must keep a dm_key"
        assert late_key is None, (
            "a later duplicate of an already-keyed pair must be left NULL, or the "
            "unique index build above would have raised"
        )
        expected_pair = sorted([ids["u1"], ids["u2"]], key=str)
        assert early_key == f"{NIL_UUID}:{expected_pair[0]}:{expected_pair[1]}"

        expected_normal_pair = sorted([ids["u1"], ids["u3"]], key=str)
        assert normal_key == f"{NIL_UUID}:{expected_normal_pair[0]}:{expected_normal_pair[1]}"

        both_rows_exist = asyncio.run(
            _dm_key_of(url, ids["dup_early"])
        ) is not None and asyncio.run(_dm_key_of(url, ids["dup_late"])) == late_key
        assert both_rows_exist, "both duplicate rows must still exist — this is a key backfill, not a merge"

        indexes = asyncio.run(_index_definitions(url, "conversations"))
        definition = indexes.get("idx_conversations_dm_key")
        assert definition is not None, "idx_conversations_dm_key is missing"
        assert "UNIQUE" in definition.upper()
        assert "WHERE (dm_key IS NOT NULL)" in definition

        # DOWN: both the index and the column are gone; existing rows survive.
        _alembic(url, "0036", down=True)
        indexes_after_down = asyncio.run(_index_definitions(url, "conversations"))
        assert "idx_conversations_dm_key" not in indexes_after_down
        engine = create_async_engine(url)

        async def _column_gone() -> bool:
            async with engine.connect() as conn:
                result = await conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_name = 'conversations' AND column_name = 'dm_key'"
                    )
                )
                return result.first() is None

        try:
            assert asyncio.run(_column_gone())
        finally:
            asyncio.run(engine.dispose())

        # UP AGAIN: idempotent, and still survives the same duplicate fixture.
        _alembic(url, "0037")
        assert asyncio.run(_dm_key_of(url, ids["dup_early"])) is not None
    finally:
        if original is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original
        from app.config import get_settings

        get_settings.cache_clear()
        asyncio.run(
            _admin_execute(admin_url, [f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'])
        )
