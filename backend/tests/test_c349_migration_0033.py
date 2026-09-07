"""Migration 0033 against a dues_payment_intents table that already holds a reservation.

The c237/c308 shape: conftest builds every suite database with upgrade(head) on EMPTY
tables, so "0033 backfills existing reservations from their cycle" is a statement no
ordinary test makes about real rows. This file walks a throwaway database to 0032,
SEEDS a reservation (asserted to exist before upgrading — the backfill-on-empty trap),
then runs 0033 and pins:

  * the seeded reservation's amount_cents equals ITS cycle's amount and currency is
    'usd' — the backfill wrote the right value through the FK join, not a constant;
  * both columns are NOT NULL afterwards, so a post-migration insert that omits them is
    refused (the snapshot cannot be skipped by any future code path);
  * stripe_settlement_quarantine exists with its reason CHECK in force (an undeclared
    reason is refused; a declared one inserts) and event_id unique;
  * downgrade removes the table and both columns and leaves the reservation intact;
    upgrade again is idempotent and re-backfills.

Harness verbatim from test_c308_migration_backfill.py, including the teardown that
must never mask an assertion failure.
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
CYCLE_AMOUNT = 12_345


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


async def _run(url: str, sql: str, params: dict | None = None, *, fetch: bool = False):
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(sql), params or {})
            return result.all() if fetch else None
    finally:
        await engine.dispose()


def _refused(coro) -> bool:
    try:
        asyncio.run(coro)
    except IntegrityError:
        return True
    except DBAPIError as exc:
        msg = str(exc).lower()
        return "check" in msg or "not-null" in msg or "null value" in msg or "unique" in msg
    return False


async def _seed_reservation(url: str) -> tuple[str, str, str, str]:
    """campus -> chapter -> user -> cycle (CYCLE_AMOUNT) -> reservation, at 0032, where
    the reservation has no amount/currency columns yet."""
    campus, chapter, user, cycle, reservation = (str(uuid.uuid4()) for _ in range(5))
    await _run(url, "INSERT INTO campuses (id, name, slug) VALUES (:id, 'Snapshot U', :slug)",
               {"id": campus, "slug": f"snap-{campus[:8]}"})
    await _run(url, "INSERT INTO chapters (id, campus_id, org_name) VALUES (:id, :campus, 'Pre-0033 Chapter')",
               {"id": chapter, "campus": campus})
    await _run(url, "INSERT INTO users (id, firebase_uid, email, display_name, account_type) "
                    "VALUES (:id, :uid, :email, 'Payer', 'greek')",
               {"id": user, "uid": f"uid-{user}", "email": f"{user}@example.edu"})
    await _run(url, "INSERT INTO dues_cycles (id, chapter_id, name, amount_cents, due_date) "
                    "VALUES (:id, :chapter, 'Fall', :amount, '2026-12-01')",
               {"id": cycle, "chapter": chapter, "amount": CYCLE_AMOUNT})
    await _run(url, "INSERT INTO dues_payment_intents (id, chapter_id, dues_cycle_id, user_id, rail, status, stripe_payment_intent_id) "
                    "VALUES (:id, :chapter, :cycle, :user, 'card', 'open', 'pi_pre0033')",
               {"id": reservation, "chapter": chapter, "cycle": cycle, "user": user})
    return chapter, user, cycle, reservation


async def _columns(url: str, table: str) -> dict[str, str]:
    rows = await _run(url, "SELECT column_name, is_nullable FROM information_schema.columns WHERE table_name = :t",
                      {"t": table}, fetch=True)
    return {r[0]: r[1] for r in rows}


async def _table_exists(url: str, table: str) -> bool:
    rows = await _run(url, "SELECT 1 FROM information_schema.tables WHERE table_name = :t", {"t": table}, fetch=True)
    return bool(rows)


def test_0033_snapshots_existing_reservations_and_adds_the_quarantine_table() -> None:
    requested = os.environ.get(
        "TEST_DATABASE_URL", "postgresql+asyncpg://chirp:chirp@localhost:5432/chirp_test"
    )
    base = _database_of(requested)
    admin_url = _swap_database(requested, "postgres")
    db_name = f"{base}_c349snap_{uuid.uuid4().hex[:8]}"
    url = _swap_database(requested, db_name)

    try:
        asyncio.run(_probe(admin_url))
    except Exception:
        pytest.skip("postgres not available")

    original = os.environ.get("DATABASE_URL")
    asyncio.run(_admin_execute(admin_url, [f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)', f'CREATE DATABASE "{db_name}"']))
    try:
        _alembic(url, "0032")
        cols = asyncio.run(_columns(url, "dues_payment_intents"))
        assert "amount_cents" not in cols and "currency" not in cols, "0032 must not have the snapshot yet"
        assert not asyncio.run(_table_exists(url, "stripe_settlement_quarantine"))

        chapter, user, cycle, reservation = asyncio.run(_seed_reservation(url))
        # THE GUARD (c237): the row exists before the backfill runs.
        assert asyncio.run(_run(url, "SELECT count(*) FROM dues_payment_intents", fetch=True))[0][0] == 1, (
            "seed failed: the backfill would be proven against an empty table"
        )

        _alembic(url, "0033")

        # Backfilled through the cycle join, not a constant: CYCLE_AMOUNT is deliberately
        # not a round number so a hard-coded default could not pass this by accident.
        row = asyncio.run(_run(url, "SELECT amount_cents, currency, status FROM dues_payment_intents WHERE id = :id",
                               {"id": reservation}, fetch=True))[0]
        assert row == (CYCLE_AMOUNT, "usd", "open"), row
        cols = asyncio.run(_columns(url, "dues_payment_intents"))
        assert cols["amount_cents"] == "NO" and cols["currency"] == "NO", "snapshot columns must be NOT NULL"
        assert _refused(_run(url, "INSERT INTO dues_payment_intents (chapter_id, dues_cycle_id, user_id, rail) "
                                  "VALUES (:c, :cy, :u, 'card')", {"c": chapter, "cy": cycle, "u": user})), (
            "a reservation without a snapshot inserted after 0033"
        )

        # Quarantine table: reason CHECK in force, event_id unique, FKs honoured.
        assert asyncio.run(_table_exists(url, "stripe_settlement_quarantine"))
        assert _refused(_run(url, "INSERT INTO stripe_settlement_quarantine (event_id, event_type, reason, expected, observed) "
                                  "VALUES ('evt_bad', 'payment_intent.succeeded', 'made_up_reason', '{}', '{}')")), (
            "an undeclared quarantine reason inserted"
        )
        asyncio.run(_run(url, "INSERT INTO stripe_settlement_quarantine "
                              "(event_id, event_type, stripe_payment_intent_id, reservation_id, chapter_id, reason, expected, observed) "
                              "VALUES ('evt_q1', 'payment_intent.succeeded', 'pi_pre0033', :r, :c, 'amount_mismatch', "
                              "'{\"amount_cents\": 12345}', '{\"amount_received\": 100}')",
                         {"r": reservation, "c": chapter}))
        assert _refused(_run(url, "INSERT INTO stripe_settlement_quarantine (event_id, event_type, reason, expected, observed) "
                                  "VALUES ('evt_q1', 'payment_intent.succeeded', 'no_reservation', '{}', '{}')")), (
            "a duplicate quarantine event_id inserted"
        )

        assert _refused(_run(url, "UPDATE stripe_settlement_quarantine SET reason='no_reservation' WHERE event_id='evt_q1'"))
        assert _refused(_run(url, "DELETE FROM stripe_settlement_quarantine WHERE event_id='evt_q1'"))
        assert asyncio.run(_run(url, "SELECT reason FROM stripe_settlement_quarantine WHERE event_id='evt_q1'", fetch=True)) == [("amount_mismatch",)]

        # DOWN: table and columns gone, the reservation survives.
        _alembic(url, "0032", down=True)
        assert not asyncio.run(_table_exists(url, "stripe_settlement_quarantine"))
        cols = asyncio.run(_columns(url, "dues_payment_intents"))
        assert "amount_cents" not in cols and "currency" not in cols
        assert asyncio.run(_run(url, "SELECT status FROM dues_payment_intents WHERE id = :id", {"id": reservation}, fetch=True)) == [("open",)]

        # UP AGAIN: idempotent, re-backfills.
        _alembic(url, "0033")
        row = asyncio.run(_run(url, "SELECT amount_cents, currency FROM dues_payment_intents WHERE id = :id",
                               {"id": reservation}, fetch=True))[0]
        assert row == (CYCLE_AMOUNT, "usd")
    finally:
        if original is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original
        from app.config import get_settings

        get_settings.cache_clear()
        try:
            asyncio.run(_admin_execute(admin_url, [f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)']))
        except Exception:
            pass
