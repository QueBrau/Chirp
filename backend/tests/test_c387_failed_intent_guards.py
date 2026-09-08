"""Real PostgreSQL held-failed uniqueness, both races, and populated 0036 preflight."""
from __future__ import annotations

import asyncio
import runpy
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.errors import is_cross_table_dues_guard_conflict
from app.db import get_session_factory
from tests.test_c230_cross_table_dues_guard import _plan, _reservation
from tests.test_c349_settlement_binding import rows
from tests.test_payments import _create_dues_cycle


MIGRATION = Path(__file__).parents[1] / "alembic/versions/0036_retryable_failed_intent_reservations.py"


@pytest.mark.parametrize("first", ["failed", "plan"])
async def test_failed_intent_and_plan_race_has_one_winner(client, make_chapter_with, first):
    setup = await make_chapter_with(role="member")
    cycle = await _create_dues_cycle(client, setup, amount_cents=30_000)
    reservation = _reservation(setup.chapter_id, cycle, setup.member.id)
    reservation.status = "failed"
    plan = _plan(setup.chapter_id, cycle, setup.member.id, setup.president.id)
    factory = get_session_factory()
    task_b = None
    async with factory() as a, factory() as b, factory() as observer:
        try:
            pid_a = await a.scalar(text("SELECT pg_backend_pid()"))
            pid_b = await b.scalar(text("SELECT pg_backend_pid()"))
            # Both reads see no opposing record. The loser must be refused by
            # the database after its INSERT really waits for the first owner.
            assert await a.scalar(text("SELECT count(*) FROM dues_payment_intents")) == 0
            assert await b.scalar(text("SELECT count(*) FROM dues_payment_plans")) == 0
            a.add(reservation if first == "failed" else plan)
            await a.flush()
            b.add(plan if first == "failed" else reservation)
            task_b = asyncio.create_task(b.commit())
            async with asyncio.timeout(15):
                while pid_a not in await observer.scalar(
                    text("SELECT pg_blocking_pids(:pid)"), {"pid": pid_b},
                ):
                    if task_b.done():
                        await task_b  # surface an unexpected failure before waiting
                        pytest.fail("the losing insert did not contend for the held obligation")
                    await asyncio.sleep(0.01)
            await a.commit()
            with pytest.raises(DBAPIError) as caught:
                await task_b
            assert is_cross_table_dues_guard_conflict(caught.value)
            await b.rollback()
        finally:
            if task_b is not None and not task_b.done():
                task_b.cancel()
                await asyncio.gather(task_b, return_exceptions=True)
    assert len(await rows("SELECT id FROM dues_payment_intents")) == (first == "failed")
    assert len(await rows("SELECT id FROM dues_payment_plans")) == (first == "plan")


@pytest.mark.parametrize("second_status", ["open", "failed", "succeeded"])
async def test_failed_intent_holds_the_unique_obligation_slot(
    client, make_chapter_with, second_status,
):
    setup = await make_chapter_with(role="member")
    cycle = await _create_dues_cycle(client, setup)
    async with get_session_factory()() as session:
        held = _reservation(setup.chapter_id, cycle, setup.member.id)
        held.status = "failed"
        session.add(held)
        await session.commit()
        second = _reservation(setup.chapter_id, cycle, setup.member.id)
        second.status = second_status
        session.add(second)
        with pytest.raises(IntegrityError) as caught:
            await session.commit()
        assert "uq_dues_intent_live" in str(caught.value.orig)
        await session.rollback()
    assert await rows("SELECT status FROM dues_payment_intents") == [{"status": "failed"}]


def _state(connection: Connection) -> dict:
    return {
        "rows": {table: connection.execute(text(
            f"SELECT row_to_json(t) FROM {table} t ORDER BY id"
        )).scalars().all() for table in ("dues_payment_intents", "dues_payment_plans")},
        "index": connection.scalar(text("SELECT indexdef FROM pg_indexes WHERE indexname='uq_dues_intent_live'")),
        "guards": dict(connection.execute(text(
            "SELECT proname, prosrc FROM pg_proc WHERE proname IN "
            "('cross_table_dues_guard_intents_fn', 'cross_table_dues_guard_plans_fn')"
        )).tuples().all()),
    }


async def _seed_held(client, make_chapter_with):
    setup = await make_chapter_with(role="member")
    cycle = await _create_dues_cycle(client, setup)
    async with get_session_factory()() as session:
        held = _reservation(setup.chapter_id, cycle, setup.member.id)
        held.status = "failed"
        held.stripe_payment_intent_id = "pi_migration_failed"
        canceled = _reservation(setup.chapter_id, cycle, setup.member.id)
        canceled.status = "canceled"
        canceled.stripe_payment_intent_id = "pi_migration_canceled"
        old_plan = _plan(setup.chapter_id, cycle, setup.member.id, setup.president.id)
        old_plan.status = "canceled"
        session.add_all([held, canceled, old_plan])
        await session.commit()


async def test_0036_populated_downgrade_upgrade_preserves_financial_rows(
    migrated_db, client, make_chapter_with,
):
    await _seed_held(client, make_chapter_with)
    migration = runpy.run_path(str(MIGRATION))

    def verify(connection):
        before = _state(connection)
        assert "'failed'" in before["index"]
        assert len(before["guards"]) == 2
        assert all("'failed'" in function for function in before["guards"].values())
        with Operations.context(MigrationContext.configure(connection)):
            migration["downgrade"]()
            down = _state(connection)
            assert "'failed'" not in down["index"]
            assert all("'failed'" not in function for function in down["guards"].values())
            assert down["rows"] == before["rows"]
            migration["upgrade"]()
        assert _state(connection) == before

    engine = create_async_engine(migrated_db)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(verify)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("conflict", ["open", "failed", "succeeded", "plan"])
async def test_0036_conflict_preflight_aborts_without_choosing_or_changing_rows(
    migrated_db, client, make_chapter_with, conflict,
):
    await _seed_held(client, make_chapter_with)
    migration = runpy.run_path(str(MIGRATION))

    def verify(connection):
        with Operations.context(MigrationContext.configure(connection)):
            migration["downgrade"]()
            # These combinations are valid under 0035: failed wasn't held. This
            # is historical data, seeded without disabling any trigger/index.
            if conflict == "plan":
                connection.execute(text("UPDATE dues_payment_plans SET status='active'"))
            else:
                connection.execute(text(
                    "INSERT INTO dues_payment_intents "
                    "(chapter_id, dues_cycle_id, user_id, rail, status, amount_cents, currency) "
                    "SELECT chapter_id, dues_cycle_id, user_id, rail, :status, amount_cents, currency "
                    "FROM dues_payment_intents WHERE status='failed'"
                ), {"status": conflict})
            before = _state(connection)
            with connection.begin_nested() as savepoint:
                with pytest.raises(DBAPIError) as caught:
                    migration["upgrade"]()
                savepoint.rollback()
            message = str(caught.value.orig)
            assert "c387 reconciliation required" in message
            duplicate_count, plan_count = (0, 1) if conflict == "plan" else (1, 0)
            assert f"{duplicate_count} obligations have multiple held intents" in message
            assert f"{plan_count} obligations have an active plan" in message
            assert "Reconcile provider outcomes" in message
            assert "pi_migration" not in message
            assert _state(connection) == before

    engine = create_async_engine(migrated_db)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.run_sync(verify)
            finally:
                # Restore the fixture's head schema and original rows even when
                # this expected failure left a historical conflict in the scope.
                await transaction.rollback()
    finally:
        await engine.dispose()


async def test_0036_holds_writers_between_preflight_and_constraint_install(
    migrated_db, client, make_chapter_with,
):
    from sqlalchemy.util.concurrency import await_only

    await _seed_held(client, make_chapter_with)
    migration = runpy.run_path(str(MIGRATION))
    reached_install = asyncio.Event()
    release_install = asyncio.Event()
    original_index = migration["upgrade"].__globals__["_index"]

    async def pause_before_install():
        reached_install.set()
        await release_install.wait()

    def paused_index(held):
        # This hook executes after the real preflight, before its first DDL.
        await_only(pause_before_install())
        original_index(held)

    migration["upgrade"].__globals__["_index"] = paused_index

    def upgrade(connection):
        with Operations.context(MigrationContext.configure(connection)):
            migration["upgrade"]()

    engine = create_async_engine(migrated_db)
    migration_task = writer_task = None
    try:
        async with engine.connect() as installer, engine.connect() as writer, engine.connect() as observer:
            migration_pid = await installer.scalar(text("SELECT pg_backend_pid()"))
            writer_pid = await writer.scalar(text("SELECT pg_backend_pid()"))
            migration_task = asyncio.create_task(installer.run_sync(upgrade))
            async with asyncio.timeout(15):
                await reached_install.wait()
                writer_task = asyncio.create_task(writer.execute(text(
                    "UPDATE dues_payment_intents SET updated_at=updated_at WHERE status='failed'"
                )))
                while migration_pid not in await observer.scalar(
                    text("SELECT pg_blocking_pids(:pid)"), {"pid": writer_pid},
                ):
                    if writer_task.done():
                        await writer_task
                        pytest.fail("a writer entered between preflight and index installation")
                    await asyncio.sleep(0.01)
                release_install.set()
                await migration_task
                assert not writer_task.done(), "migration locks must last until commit"
                await installer.commit()
                await writer_task
                await writer.rollback()
    finally:
        release_install.set()
        for task in (migration_task, writer_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (migration_task, writer_task) if task is not None),
                             return_exceptions=True)
        await engine.dispose()
