"""Pins the conftest contract (board card c399): the per-run scratch database
conftest creates must be UTF8 regardless of what template1 happens to be on
this machine, so a local run matches CI (postgres:16, UTF8) instead of
inheriting Jose's SQL_ASCII template1.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


async def test_run_database_is_utf8(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT pg_encoding_to_char(encoding) FROM pg_database "
                     "WHERE datname = current_database()")
            )
            encoding = result.scalar_one()
    finally:
        await engine.dispose()
    assert encoding == "UTF8", (
        f"the per-run test database reports encoding {encoding!r}; conftest's "
        "CREATE DATABASE for this run database is missing ENCODING 'UTF8' "
        "TEMPLATE template0 (board card c399)"
    )
