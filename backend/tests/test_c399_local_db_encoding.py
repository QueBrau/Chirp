"""Integration tests for scripts/local_db_encoding.py against a REAL local Postgres
server (board card c399). These never touch chirp, chirp_test or template1 — only
throwaway c399_scratch_<hex> databases this file creates and drops itself.

Skips cleanly when no local server is reachable, or when the TEST_DATABASE_URL
role lacks CREATEDB. On Jose's Mac (Homebrew Postgres 14.20, SQL_ASCII cluster)
these run for real and exercise the apply path end to end.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = BACKEND_DIR.parent / "scripts"
DEFAULT_TEST_DATABASE_URL = "postgresql+asyncpg://chirp:chirp@localhost:5432/chirp_test"

_spec = importlib.util.spec_from_file_location("local_db_encoding", SCRIPTS_DIR / "local_db_encoding.py")
led = importlib.util.module_from_spec(_spec)
sys.modules["local_db_encoding"] = led
_spec.loader.exec_module(led)

INVALID_UTF8_BYTES = bytes([0xC3, 0x28])  # a well-known invalid two-byte sequence
VALID_NONASCII_TEXT = "cafe 界 \U0001D11E"


def _admin_url() -> str:
    requested = os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
    parsed = led.parse_database_url(requested)
    return led.build_url(parsed, "postgres")


async def _create_sql_ascii_db(admin_url: str, name: str) -> None:
    conn = await asyncpg.connect(admin_url)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        await conn.execute(f"CREATE DATABASE \"{name}\" TEMPLATE template0 ENCODING 'SQL_ASCII'")
    finally:
        await conn.close()


async def _drop_scratch_family(admin_url: str, scratch_name: str) -> None:
    """Drop every database a test run against scratch_name could have left
    behind: the scratch name itself, its never-renamed-in _utf8 copy (only
    present after a failed apply), and its rename-away *_sqlascii_<stamp>
    backup (only present after a successful apply -- the stamp makes the
    exact name unpredictable from here)."""
    conn = await asyncpg.connect(admin_url)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{scratch_name}"')
        await conn.execute(f'DROP DATABASE IF EXISTS "{scratch_name}_utf8"')
        backups = await conn.fetch(
            "SELECT datname FROM pg_database WHERE datname LIKE $1",
            f"{scratch_name}\\_sqlascii\\_%",
        )
        for row in backups:
            await conn.execute(f'DROP DATABASE IF EXISTS "{row["datname"]}"')
    finally:
        await conn.close()


async def _encoding_of(admin_url: str, name: str):
    conn = await asyncpg.connect(admin_url)
    try:
        return await conn.fetchval(
            "SELECT pg_encoding_to_char(encoding) FROM pg_database WHERE datname = $1", name
        )
    finally:
        await conn.close()


async def _wait_for_no_activity(admin_url: str, name: str, timeout: float = 3.0) -> None:
    """A just-closed asyncpg connection's backend row can briefly outlive the
    client-side close() under load (observed on this machine while several
    pytest runs were active) -- poll pg_stat_activity instead of assuming the
    seed connection above is already gone by the time the plan is built.
    Tests that deliberately keep a connection open never call this."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while True:
        conn = await asyncpg.connect(admin_url)
        try:
            rows = await conn.fetch(
                "SELECT pid FROM pg_stat_activity WHERE datname = $1 AND pid <> pg_backend_pid()", name
            )
        finally:
            await conn.close()
        if not rows or loop.time() >= deadline:
            return
        await asyncio.sleep(0.05)


async def _make_plan(admin_url: str, name: str):
    executor = led.Executor()
    db_rows, activity_rows, is_superuser = await led.gather_state(executor, admin_url, [name])
    activity_by_name = led.group_activity_by_name(activity_rows)
    entries = led.build_plan(db_rows, activity_by_name, is_superuser)
    return executor, entries[0]


@pytest.fixture(scope="module")
def local_admin_url() -> str:
    url = _admin_url()

    async def _probe() -> bool:
        conn = await asyncpg.connect(url)
        try:
            return bool(await conn.fetchval(
                "SELECT rolcreatedb OR rolsuper FROM pg_roles WHERE rolname = current_user"
            ))
        finally:
            await conn.close()

    try:
        can_create = asyncio.run(_probe())
    except Exception as exc:
        pytest.skip(f"local postgres not reachable at {led.mask_url(url)}: {type(exc).__name__}: {exc}")
    if not can_create:
        pytest.skip("TEST_DATABASE_URL role lacks CREATEDB — c399 integration needs a local server "
                    "the role can create and drop databases on")
    return url


@pytest.fixture
def scratch_name() -> str:
    return f"c399_scratch_{uuid.uuid4().hex[:12]}"


class TestApplySwapsSqlAsciiToUtf8:
    """chirps-17 acceptance: apply swaps a throwaway SQL_ASCII database to UTF8
    and keeps a backup (manager rulings 1, 3, 5)."""

    def test_swap_preserves_data_and_keeps_backup(self, local_admin_url, scratch_name, tmp_path):
        parsed = led.parse_database_url(local_admin_url)
        asyncio.run(_create_sql_ascii_db(local_admin_url, scratch_name))
        try:
            # Vacuous-test guard: prove the pre-condition before relying on it.
            before = asyncio.run(_encoding_of(local_admin_url, scratch_name))
            assert before == "SQL_ASCII"

            async def _seed():
                c = await asyncpg.connect(led.build_url(parsed, scratch_name))
                try:
                    await c.execute("CREATE TABLE t (id serial primary key, col text)")
                    # A SQL_ASCII server cannot convert a UTF8-client-encoded
                    # non-ASCII parameter (that is the c399 symptom itself), so
                    # the seed value goes in as raw bytes reinterpreted with the
                    # SQL_ASCII "encoding", which never raises.
                    await c.execute(
                        "INSERT INTO t (col) VALUES (convert_from($1::bytea, 'SQL_ASCII'))",
                        VALID_NONASCII_TEXT.encode("utf-8"),
                    )
                    count = await c.fetchval("SELECT count(*) FROM t")
                    assert count == 1
                finally:
                    await c.close()

            asyncio.run(_seed())
            asyncio.run(_wait_for_no_activity(local_admin_url, scratch_name))

            executor, entry = asyncio.run(_make_plan(local_admin_url, scratch_name))
            assert entry.action == "recreate", entry
            result = asyncio.run(led.apply_ordinary(
                executor, parsed, entry, str(tmp_path), terminate_connections=False
            ))
            assert result.ok, result.reason
            assert result.backup_name is not None
            assert result.dump_path is not None
            assert Path(result.dump_path).exists()

            after = asyncio.run(_encoding_of(local_admin_url, scratch_name))
            assert after == "UTF8"
            backup_encoding = asyncio.run(_encoding_of(local_admin_url, result.backup_name))
            assert backup_encoding == "SQL_ASCII"

            async def _read_back():
                c = await asyncpg.connect(led.build_url(parsed, scratch_name))
                try:
                    return await c.fetchval("SELECT col FROM t")
                finally:
                    await c.close()

            value = asyncio.run(_read_back())
            assert value == VALID_NONASCII_TEXT
        finally:
            asyncio.run(_drop_scratch_family(local_admin_url, scratch_name))


class TestLiveConnectionBlocksSwap:
    """chirps-17 acceptance + manager ruling 3: a held connection is reported
    and refused."""

    def test_held_connection_is_named_and_refused_then_terminated(self, local_admin_url, scratch_name, tmp_path):
        parsed = led.parse_database_url(local_admin_url)
        asyncio.run(_create_sql_ascii_db(local_admin_url, scratch_name))
        holder = None
        try:
            before = asyncio.run(_encoding_of(local_admin_url, scratch_name))
            assert before == "SQL_ASCII"

            async def _hold():
                return await asyncpg.connect(
                    led.build_url(parsed, scratch_name), server_settings={"application_name": "c399-holder"}
                )

            holder = asyncio.run(_hold())

            executor, entry = asyncio.run(_make_plan(local_admin_url, scratch_name))
            assert entry.action == "blocked", entry
            names = [b.get("application_name") for b in entry.blockers]
            assert "c399-holder" in names, entry.blockers

            refused = asyncio.run(led.apply_ordinary(
                executor, parsed, entry, str(tmp_path), terminate_connections=False
            ))
            assert refused.ok is False
            assert refused.exit_code == 3
            assert str(entry.blockers[0]["pid"]) in refused.reason

            still_ascii = asyncio.run(_encoding_of(local_admin_url, scratch_name))
            assert still_ascii == "SQL_ASCII", "a refused apply must never touch the database"

            terminated = asyncio.run(led.apply_ordinary(
                executor, parsed, entry, str(tmp_path), terminate_connections=True
            ))
            assert terminated.ok, terminated.reason
            after = asyncio.run(_encoding_of(local_admin_url, scratch_name))
            assert after == "UTF8"
        finally:
            if holder is not None:
                try:
                    asyncio.run(holder.close())
                except Exception:
                    pass
            asyncio.run(_drop_scratch_family(local_admin_url, scratch_name))


class TestInvalidBytesNeverSwap:
    """chirps-17 acceptance + manager ruling 4: invalid UTF-8 bytes never swap
    and are counted."""

    def test_invalid_bytes_block_the_swap_and_are_counted(self, local_admin_url, scratch_name, tmp_path):
        parsed = led.parse_database_url(local_admin_url)
        asyncio.run(_create_sql_ascii_db(local_admin_url, scratch_name))
        try:
            before = asyncio.run(_encoding_of(local_admin_url, scratch_name))
            assert before == "SQL_ASCII"

            async def _seed():
                c = await asyncpg.connect(led.build_url(parsed, scratch_name))
                try:
                    await c.execute("CREATE TABLE t (id serial primary key, col text)")
                    await c.execute(
                        "INSERT INTO t (col) VALUES (convert_from($1::bytea, 'SQL_ASCII'))",
                        INVALID_UTF8_BYTES,
                    )
                    count = await c.fetchval("SELECT count(*) FROM t")
                    assert count == 1
                finally:
                    await c.close()

            asyncio.run(_seed())
            asyncio.run(_wait_for_no_activity(local_admin_url, scratch_name))

            executor, entry = asyncio.run(_make_plan(local_admin_url, scratch_name))
            assert entry.action == "recreate", entry
            result = asyncio.run(led.apply_ordinary(
                executor, parsed, entry, str(tmp_path), terminate_connections=False
            ))
            assert result.ok is False
            assert result.exit_code == 5
            assert result.dump_path is not None
            assert Path(result.dump_path).exists()

            offenders = {(o.table, o.column): o.count for o in result.offenders}
            assert offenders.get(("t", "col")) == 1, result.offenders

            still_ascii = asyncio.run(_encoding_of(local_admin_url, scratch_name))
            assert still_ascii == "SQL_ASCII", "a failed restore must never swap the original"
            utf8_leftover = asyncio.run(_encoding_of(local_admin_url, f"{scratch_name}_utf8"))
            assert utf8_leftover is None, "the failed _utf8 copy must be dropped"

            async def _row_still_present():
                c = await asyncpg.connect(led.build_url(parsed, scratch_name))
                try:
                    return await c.fetchval("SELECT count(*) FROM t")
                finally:
                    await c.close()

            assert asyncio.run(_row_still_present()) == 1
        finally:
            asyncio.run(_drop_scratch_family(local_admin_url, scratch_name))
