"""Seeded upgrade/downgrade coverage for c342's independent block intentions."""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.test_c279_block_provenance import _admin_execute, _alembic, _swap_database


async def _seed_legacy_rows(url: str) -> tuple[str, str, str]:
    engine = create_async_engine(url)
    blocker, named, anonymous = (str(uuid.uuid4()) for _ in range(3))
    try:
        async with engine.begin() as conn:
            for user_id in (blocker, named, anonymous):
                await conn.execute(
                    text(
                        "INSERT INTO users (id, firebase_uid, email, display_name, account_type) "
                        "VALUES (:id, :uid, :email, 'Migration fixture', 'greek')"
                    ),
                    {"id": user_id, "uid": user_id, "email": f"{user_id}@example.edu"},
                )
            for target, source in ((named, "named"), (anonymous, "by_chirp")):
                await conn.execute(
                    text(
                        "INSERT INTO user_blocks (blocker_id, blocked_id, source, created_at) "
                        "VALUES (:blocker, :target, :source, '2026-09-01T10:00:00Z')"
                    ),
                    {"blocker": blocker, "target": target, "source": source},
                )
    finally:
        await engine.dispose()
    return blocker, named, anonymous


async def _check_upgrade(url: str, blocker: str, named: str, anonymous: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            rows = (await conn.execute(text(
                "SELECT source, created_at = anonymous_created_at AS preserved "
                "FROM user_blocks ORDER BY source"
            ))).all()
            assert rows == [("by_chirp", True), ("named", True)]

            # A still-running old revision omits the marker; preserve its existing
            # Chirps-hide behavior. A new named-only write explicitly supplies NULL.
            for explicit_null, left, right in (
                (False, named, blocker), (True, anonymous, blocker),
            ):
                column = ", anonymous_created_at" if explicit_null else ""
                value = ", NULL" if explicit_null else ""
                row = (await conn.execute(
                    text(
                        "INSERT INTO user_blocks (blocker_id, blocked_id, source"
                        f"{column}) VALUES (:left, :right, 'named'{value}) "
                        "RETURNING anonymous_created_at"
                    ),
                    {"left": left, "right": right},
                )).first()
                assert row is not None
                assert (row[0] is None) == explicit_null

        with pytest.raises(IntegrityError, match="ck_user_blocks_anonymous_intent"):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE user_blocks SET anonymous_created_at = NULL "
                        "WHERE source = 'by_chirp'"
                    )
                )
        with pytest.raises(IntegrityError, match="ck_user_blocks_source"):
            async with engine.begin() as conn:
                await conn.execute(text("UPDATE user_blocks SET source = 'by-chirp'"))
    finally:
        await engine.dispose()


async def _check_downgrade(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(text(
                "SELECT source, count(*) FROM user_blocks GROUP BY source ORDER BY source"
            ))).all()
            assert rows == [("by_chirp", 1), ("named", 3)]
            assert (await conn.execute(text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'user_blocks' AND column_name = 'anonymous_created_at'"
            ))).scalar_one() == 0
    finally:
        await engine.dispose()


async def _check_second_upgrade(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            assert (await conn.execute(text(
                "SELECT count(*) FROM user_blocks WHERE anonymous_created_at IS NOT NULL"
            ))).scalar_one() == 4
    finally:
        await engine.dispose()


def test_0034_up_down_up_preserves_legacy_hides_and_independent_intent() -> None:
    requested = os.environ.get(
        "TEST_DATABASE_URL", "postgresql+asyncpg://chirp:chirp@localhost:5432/chirp_test"
    )
    admin_url = _swap_database(requested, "postgres")
    database = f"chirp_test_c342_{uuid.uuid4().hex[:12]}"
    url = _swap_database(requested, database)
    original = os.environ.get("DATABASE_URL")
    asyncio.run(_admin_execute(admin_url, [f'CREATE DATABASE "{database}"']))
    try:
        _alembic(url, "0033")
        blocker, named, anonymous = asyncio.run(_seed_legacy_rows(url))
        _alembic(url, "0034")
        asyncio.run(_check_upgrade(url, blocker, named, anonymous))
        _alembic(url, "0033", down=True)
        asyncio.run(_check_downgrade(url))
        _alembic(url, "0034")
        asyncio.run(_check_second_upgrade(url))
    finally:
        if original is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original
        from app.config import get_settings

        get_settings.cache_clear()
        # Engines above are disposed in finally. Avoid FORCE/pg_terminate_backend:
        # the local chirp role is not a superuser, and cleanup must not mask a failed
        # privacy assertion if another backend temporarily holds this private DB.
        try:
            asyncio.run(_admin_execute(admin_url, [f'DROP DATABASE "{database}"']))
        except Exception:
            pass
