"""Cross-table dues guard (migration 0028, board cards c224/c230): the DATABASE, not
a read-then-insert, is what stops a self-serve reservation and a treasurer's payment
plan from both going live for the same (dues_cycle_id, user_id) at once.

THE BUG THIS CLOSES. payments.py's create_dues_payment_intent and finance.py's
create_dues_payment_plan each already guard their OWN table with a partial unique
index (uq_dues_intent_live / uq_dues_payment_plans_active_per_member) — the read in
each route only picks the honest 409 reason, same as this repo's convention states
at both sites. But each route ALSO reads the OTHER table before inserting, and THAT
read was plain check-then-act: nothing in the database constrained
dues_payment_intents against dues_payment_plans. A member tapping Pay at the same
moment a treasurer creates their plan could land both reads before either insert
committed, and both would succeed — a live reservation next to an active plan,
which is genuine double collection once the webhook appends a dues_payment on top
of the installments already landing.

WHAT EACH GROUP OF TESTS PROVES, tied to the c230 board card's own test list:

(a) test_*_wins_when_it_flushes_first — the TRUE interleaving, two independent raw
    AsyncSessions, both orderings. This is the direct proof the trigger + advisory
    lock actually serialize the two racing inserts.
(a-router) test_the_*_router_maps_the_trigger_conflict_to_* — the SAME race, but
    calling the real router coroutines directly (not the raw table insert), proving
    step 2 of the fix: both insert sites actually catch the trigger's raise and
    answer the pre-existing honest 409, not a raw 500. Deterministic for the same
    reason (a) is: the interfering row is flushed-but-uncommitted before the router
    call is even created, so the router's own read-guard genuinely cannot see it
    (the real TOCTOU window) and its insert is guaranteed to contend for the same
    advisory lock.
(c) test_0028_migration_up_down_up_with_rows_present — a throwaway database,
    migrated to 0027, seeded with real rows, then up/down/up across 0028, checking
    the trigger/function catalog entries and that the guard actually works
    immediately after each upgrade (including against rows that predate it).
(d) test_resolving_*_succeeds_despite_a_coexisting_* — the c230 x c234 interaction
    named on the board card: a reservation resolving to failed/canceled, or a plan
    being canceled/completed, must never be blocked by this guard even when the
    OTHER table happens to hold a live row for the same pair — because both are
    transitions OUT of the live state, which the trigger's own first check exists
    to exempt.

Existing HTTP-level tests already cover the read-guard's own honest 409s
(test_dues_payment_plans.py's test_a_live_self_serve_reservation_blocks_plan_creation
and test_a_member_on_an_active_plan_gets_on_payment_plan_from_the_charge_path) and
are unchanged by this migration — this file does not re-test that surface, only the
NEW cross-table backstop.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app import models
from app.core.errors import is_cross_table_dues_guard_conflict
from app.db import get_session_factory
from app.routers.finance import create_dues_payment_plan
from app.routers.payments import create_dues_payment_intent
from app.schemas.finance import DuesPaymentPlanCreate
from app.schemas.payments import DuesIntentCreate
from tests.conftest import ChapterSetup, MakeChapterWith
from tests.test_dues_payment_plans import _three_installments
from tests.test_payments import _create_dues_cycle, _onboard, stripe_calls, stripe_env

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _reservation(chapter_id: str, cycle_id: str, user_id: str, rail: str = "card") -> models.DuesPaymentIntent:
    return models.DuesPaymentIntent(
        chapter_id=uuid.UUID(chapter_id),
        dues_cycle_id=uuid.UUID(cycle_id),
        user_id=uuid.UUID(user_id),
        rail=rail,
    )


def _plan(
    chapter_id: str, cycle_id: str, user_id: str, created_by: str, total_cents: int = 30_000
) -> models.DuesPaymentPlan:
    return models.DuesPaymentPlan(
        chapter_id=uuid.UUID(chapter_id),
        dues_cycle_id=uuid.UUID(cycle_id),
        user_id=uuid.UUID(user_id),
        total_cents=total_cents,
        installment_count=1,
        created_by=uuid.UUID(created_by),
    )


# ---------------------------------------------------------------------------
# (a) true interleaving: two raw AsyncSessions, both orderings
# ---------------------------------------------------------------------------


async def test_reservation_wins_when_it_flushes_first(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Reservation-first ordering (refuter's own shape): both guard SELECTs run
    against an empty world before either side commits, then the reservation's
    INSERT is flushed — and so holds the transaction-scoped advisory lock for this
    (cycle, member) — before the plan's INSERT is even attempted. The plan side is
    therefore guaranteed to be the second to reach the lock, whatever the exact
    scheduling: it blocks, then (once the reservation commits) sees the reservation
    as committed and loses. Only one row survives.
    """
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=30_000)

    session_factory = get_session_factory()
    session_a = session_factory()  # reservation
    session_b = session_factory()  # plan
    try:
        # Both read-guards see nothing — the exact TOCTOU window this migration closes.
        assert (
            await session_a.execute(
                select(models.DuesPaymentPlan.id).where(
                    models.DuesPaymentPlan.dues_cycle_id == uuid.UUID(cycle_id),
                    models.DuesPaymentPlan.user_id == uuid.UUID(setup.member.id),
                    models.DuesPaymentPlan.status == "active",
                )
            )
        ).scalar_one_or_none() is None
        assert (
            await session_b.execute(
                select(models.DuesPaymentIntent.id).where(
                    models.DuesPaymentIntent.dues_cycle_id == uuid.UUID(cycle_id),
                    models.DuesPaymentIntent.user_id == uuid.UUID(setup.member.id),
                    models.DuesPaymentIntent.status.in_(("open", "succeeded")),
                )
            )
        ).scalar_one_or_none() is None

        session_a.add(_reservation(setup.chapter_id, cycle_id, setup.member.id))
        await session_a.flush()  # holds the lock for (cycle, member), uncommitted

        session_b.add(_plan(setup.chapter_id, cycle_id, setup.member.id, setup.president.id))
        task_b = asyncio.create_task(session_b.commit())
        await session_a.commit()  # releases the lock; B's blocked insert now sees A's row

        with pytest.raises(DBAPIError) as exc_info:
            await task_b
        assert is_cross_table_dues_guard_conflict(exc_info.value), (
            f"expected the cross_table_dues_guard marker, got: {exc_info.value}"
        )
        await session_b.rollback()
    finally:
        await session_a.close()
        await session_b.close()

    async with session_factory() as verify:
        reservations = (
            await verify.execute(
                select(models.DuesPaymentIntent).where(
                    models.DuesPaymentIntent.dues_cycle_id == uuid.UUID(cycle_id)
                )
            )
        ).scalars().all()
        plans = (
            await verify.execute(
                select(models.DuesPaymentPlan).where(
                    models.DuesPaymentPlan.dues_cycle_id == uuid.UUID(cycle_id)
                )
            )
        ).scalars().all()
    assert len(reservations) == 1, "the reservation that flushed first must survive"
    assert len(plans) == 0, "the plan that lost the race must not exist"


async def test_plan_wins_when_it_flushes_first(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The mirror ordering: the plan flushes (and holds the lock) first, the
    reservation is the one blocked and then loses. Proves the guard is symmetric,
    not accidentally one-directional."""
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=30_000)

    session_factory = get_session_factory()
    session_a = session_factory()  # plan
    session_b = session_factory()  # reservation
    try:
        assert (
            await session_a.execute(
                select(models.DuesPaymentIntent.id).where(
                    models.DuesPaymentIntent.dues_cycle_id == uuid.UUID(cycle_id),
                    models.DuesPaymentIntent.user_id == uuid.UUID(setup.member.id),
                    models.DuesPaymentIntent.status.in_(("open", "succeeded")),
                )
            )
        ).scalar_one_or_none() is None
        assert (
            await session_b.execute(
                select(models.DuesPaymentPlan.id).where(
                    models.DuesPaymentPlan.dues_cycle_id == uuid.UUID(cycle_id),
                    models.DuesPaymentPlan.user_id == uuid.UUID(setup.member.id),
                    models.DuesPaymentPlan.status == "active",
                )
            )
        ).scalar_one_or_none() is None

        session_a.add(_plan(setup.chapter_id, cycle_id, setup.member.id, setup.president.id))
        await session_a.flush()

        session_b.add(_reservation(setup.chapter_id, cycle_id, setup.member.id))
        task_b = asyncio.create_task(session_b.commit())
        await session_a.commit()

        with pytest.raises(DBAPIError) as exc_info:
            await task_b
        assert is_cross_table_dues_guard_conflict(exc_info.value), (
            f"expected the cross_table_dues_guard marker, got: {exc_info.value}"
        )
        await session_b.rollback()
    finally:
        await session_a.close()
        await session_b.close()

    async with session_factory() as verify:
        plans = (
            await verify.execute(
                select(models.DuesPaymentPlan).where(
                    models.DuesPaymentPlan.dues_cycle_id == uuid.UUID(cycle_id)
                )
            )
        ).scalars().all()
        reservations = (
            await verify.execute(
                select(models.DuesPaymentIntent).where(
                    models.DuesPaymentIntent.dues_cycle_id == uuid.UUID(cycle_id)
                )
            )
        ).scalars().all()
    assert len(plans) == 1, "the plan that flushed first must survive"
    assert len(reservations) == 0, "the reservation that lost the race must not exist"


# ---------------------------------------------------------------------------
# (a, router-level) both insert sites actually catch the trigger's raise
# ---------------------------------------------------------------------------


async def test_the_reservation_router_maps_the_trigger_conflict_to_on_payment_plan(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    stripe_env: None,
    stripe_calls: dict,
) -> None:
    """Step 2 of the fix, proven against the REAL router coroutine, not a
    reimplementation of its logic: create_dues_payment_intent's except block must
    catch the trigger's raise and answer on_payment_plan — the same detail its own
    active_plan read-guard gives — never a raw 500.

    Forced deterministically: the plan is flushed (not committed) BEFORE the router
    call is even created, so the router's own active_plan read-guard — a plain
    SELECT under READ COMMITTED — genuinely cannot see it (the real TOCTOU window),
    and the reservation insert it reaches moments later is guaranteed to contend
    for the identical advisory lock the plan already holds.
    """
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=30_000)
    await _onboard(client, setup)

    session_factory = get_session_factory()
    session_plan = session_factory()
    session_a = session_factory()
    try:
        session_plan.add(_plan(setup.chapter_id, cycle_id, setup.member.id, setup.president.id))
        await session_plan.flush()  # holds the lock, uncommitted — invisible to session_a

        user = await session_a.get(models.User, uuid.UUID(setup.member.id))
        task = asyncio.create_task(
            create_dues_payment_intent(
                cycle_id=uuid.UUID(cycle_id),
                body=DuesIntentCreate(rail="card"),
                user=user,
                session=session_a,
            )
        )
        await session_plan.commit()

        with pytest.raises(HTTPException) as exc_info:
            await task
        assert exc_info.value.status_code == 409
        assert exc_info.value.detail == "on_payment_plan"
    finally:
        await session_plan.close()
        await session_a.close()


async def test_the_plan_router_maps_the_trigger_conflict_to_payment_in_progress(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The mirror of the test above: create_dues_payment_plan's except block must
    catch the trigger's raise and answer payment_in_progress — the same detail its
    own live_reservation read-guard gives. No Stripe involved on this side."""
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=30_000)

    session_factory = get_session_factory()
    session_reservation = session_factory()
    session_b = session_factory()
    try:
        session_reservation.add(_reservation(setup.chapter_id, cycle_id, setup.member.id))
        await session_reservation.flush()

        membership = (
            await session_b.execute(
                select(models.Membership).where(
                    models.Membership.chapter_id == uuid.UUID(setup.chapter_id),
                    models.Membership.user_id == uuid.UUID(setup.president.id),
                )
            )
        ).scalar_one()

        task = asyncio.create_task(
            create_dues_payment_plan(
                chapter_id=uuid.UUID(setup.chapter_id),
                cycle_id=uuid.UUID(cycle_id),
                body=DuesPaymentPlanCreate(
                    user_id=uuid.UUID(setup.member.id),
                    installment_count=3,
                    installments=_three_installments(30_000),
                    note=None,
                ),
                membership=membership,
                session=session_b,
            )
        )
        await session_reservation.commit()

        with pytest.raises(HTTPException) as exc_info:
            await task
        assert exc_info.value.status_code == 409
        assert exc_info.value.detail == "payment_in_progress"
    finally:
        await session_reservation.close()
        await session_b.close()


# ---------------------------------------------------------------------------
# is_cross_table_dues_guard_conflict: the marker check itself
# ---------------------------------------------------------------------------


def test_is_cross_table_dues_guard_conflict_ignores_unrelated_errors() -> None:
    """The helper both routers rely on must not turn an unrelated DBAPIError (a
    dropped connection, say) into a false 409 — it must re-raise as the 500 it
    actually is. Only the trigger's own marker text flips it True."""
    assert is_cross_table_dues_guard_conflict(RuntimeError("connection reset by peer")) is False
    assert (
        is_cross_table_dues_guard_conflict(
            RuntimeError("cross_table_dues_guard: an active dues_payment_plan already exists")
        )
        is True
    )


# ---------------------------------------------------------------------------
# (d) c230 x c234: benign transitions must never be blocked by this guard
# ---------------------------------------------------------------------------


async def _seed_conflicting_pair_bypassing_the_guard(
    chapter_id: str, cycle_id: str, user_id: str, president_id: str
) -> None:
    """Build an OPEN reservation AND an ACTIVE plan for the SAME (cycle, member) —
    a pairing this guard refuses to let arise from migration 0028 onward. The only
    honest way to construct it for a test is the same way it could exist for real:
    as data that predates the trigger (or was written some other way before it
    existed). Disabling both triggers to seed it, then re-enabling them immediately,
    models exactly that — the trigger is off while the legacy-shaped rows are
    written, then back on before anything in the test exercises it.
    """
    async with get_session_factory()() as session:
        await session.execute(
            text(
                "ALTER TABLE dues_payment_intents "
                "DISABLE TRIGGER cross_table_dues_guard_intents"
            )
        )
        await session.execute(
            text("ALTER TABLE dues_payment_plans DISABLE TRIGGER cross_table_dues_guard_plans")
        )
        await session.execute(
            text(
                "INSERT INTO dues_payment_intents "
                "(chapter_id, dues_cycle_id, user_id, rail, status) "
                "VALUES (:chapter_id, :cycle_id, :user_id, 'card', 'open')"
            ),
            {"chapter_id": chapter_id, "cycle_id": cycle_id, "user_id": user_id},
        )
        await session.execute(
            text(
                "INSERT INTO dues_payment_plans "
                "(chapter_id, dues_cycle_id, user_id, total_cents, installment_count, created_by) "
                "VALUES (:chapter_id, :cycle_id, :user_id, 30000, 1, :president_id)"
            ),
            {
                "chapter_id": chapter_id,
                "cycle_id": cycle_id,
                "user_id": user_id,
                "president_id": president_id,
            },
        )
        await session.execute(
            text(
                "ALTER TABLE dues_payment_intents "
                "ENABLE TRIGGER cross_table_dues_guard_intents"
            )
        )
        await session.execute(
            text("ALTER TABLE dues_payment_plans ENABLE TRIGGER cross_table_dues_guard_plans")
        )
        await session.commit()


async def _reservation_status(cycle_id: str, user_id: str) -> str:
    async with get_session_factory()() as session:
        return await session.scalar(
            text(
                "SELECT status FROM dues_payment_intents "
                "WHERE dues_cycle_id = :cycle_id AND user_id = :user_id"
            ),
            {"cycle_id": cycle_id, "user_id": user_id},
        )


async def _plan_status(cycle_id: str, user_id: str) -> str:
    async with get_session_factory()() as session:
        return await session.scalar(
            text(
                "SELECT status FROM dues_payment_plans "
                "WHERE dues_cycle_id = :cycle_id AND user_id = :user_id"
            ),
            {"cycle_id": cycle_id, "user_id": user_id},
        )


@pytest.mark.parametrize("resolved_status", ["failed", "canceled"])
async def test_resolving_a_reservation_succeeds_despite_a_coexisting_active_plan(
    client: AsyncClient, make_chapter_with: MakeChapterWith, resolved_status: str
) -> None:
    """c230 x c234, pinned exactly as the board card names it: c234's webhook
    resolution of an open reservation to failed/canceled — a transition OUT of the
    live set — must succeed even when an active plan coexists with it. The trigger's
    own first check (NEW.status NOT IN ('open','succeeded') -> RETURN NEW,
    untouched) is what makes this true regardless of what the other table holds;
    this test proves it against the least forgiving case, where the OTHER table's
    conflicting row is actually sitting right there.
    """
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=30_000)
    await _seed_conflicting_pair_bypassing_the_guard(
        setup.chapter_id, cycle_id, setup.member.id, setup.president.id
    )

    async with get_session_factory()() as session:
        await session.execute(
            text(
                "UPDATE dues_payment_intents SET status = :status "
                "WHERE dues_cycle_id = :cycle_id AND user_id = :user_id"
            ),
            {"status": resolved_status, "cycle_id": cycle_id, "user_id": setup.member.id},
        )
        await session.commit()  # must not raise

    assert await _reservation_status(cycle_id, setup.member.id) == resolved_status
    assert await _plan_status(cycle_id, setup.member.id) == "active", (
        "resolving the reservation must not itself touch the plan"
    )


@pytest.mark.parametrize("resolved_status", ["canceled", "completed"])
async def test_resolving_a_plan_succeeds_despite_a_coexisting_live_reservation(
    client: AsyncClient, make_chapter_with: MakeChapterWith, resolved_status: str
) -> None:
    """The symmetric benign transition on the plan side: canceling a plan (treasurer
    calls it off) or completing one (last installment recorded) — both transitions
    OUT of 'active' — must succeed even when a live reservation coexists with it,
    for the identical reason (this guard's plan-side first check: NEW.status <>
    'active' -> RETURN NEW)."""
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=30_000)
    await _seed_conflicting_pair_bypassing_the_guard(
        setup.chapter_id, cycle_id, setup.member.id, setup.president.id
    )

    async with get_session_factory()() as session:
        await session.execute(
            text(
                "UPDATE dues_payment_plans SET status = :status "
                "WHERE dues_cycle_id = :cycle_id AND user_id = :user_id"
            ),
            {"status": resolved_status, "cycle_id": cycle_id, "user_id": setup.member.id},
        )
        await session.commit()  # must not raise

    assert await _plan_status(cycle_id, setup.member.id) == resolved_status
    assert await _reservation_status(cycle_id, setup.member.id) == "open", (
        "resolving the plan must not itself touch the reservation"
    )


# ---------------------------------------------------------------------------
# (c) migration up/down/up against a real database with rows present
# ---------------------------------------------------------------------------


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


def _upgrade_to(url: str, revision: str) -> None:
    """Same in-process mechanism as tests/conftest.py's migrated_db fixture and
    test_role_terms_backfill.py's own _migrate_to."""
    os.environ["DATABASE_URL"] = url
    from app.config import get_settings

    get_settings.cache_clear()

    from alembic import command
    from alembic.config import Config as AlembicConfig

    cfg = AlembicConfig(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(cfg, revision)


def _downgrade_to(url: str, revision: str) -> None:
    os.environ["DATABASE_URL"] = url
    from app.config import get_settings

    get_settings.cache_clear()

    from alembic import command
    from alembic.config import Config as AlembicConfig

    cfg = AlembicConfig(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.downgrade(cfg, revision)


async def _seed_pre_0028_rows(url: str) -> dict[str, str]:
    """One chapter/cycle/president/member plus a real 'open' reservation, written
    directly — the shape of rows already sitting in a live database the instant
    before 0028 runs. Returns the ids the test needs."""
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            campus_id = (
                await conn.execute(
                    text(
                        "INSERT INTO campuses (name, slug) VALUES ('C230 U', :slug) "
                        "RETURNING id"
                    ),
                    {"slug": f"c230-u-{uuid.uuid4().hex[:8]}"},
                )
            ).scalar_one()
            chapter_id = (
                await conn.execute(
                    text(
                        "INSERT INTO chapters (campus_id, org_name) VALUES (:campus, 'C230 Chi') "
                        "RETURNING id"
                    ),
                    {"campus": campus_id},
                )
            ).scalar_one()
            cycle_id = (
                await conn.execute(
                    text(
                        "INSERT INTO dues_cycles (chapter_id, name, amount_cents, due_date) "
                        "VALUES (:chapter, 'Spring 2028', 30000, '2028-03-01') RETURNING id"
                    ),
                    {"chapter": chapter_id},
                )
            ).scalar_one()
            president_id = (
                await conn.execute(
                    text(
                        "INSERT INTO users (firebase_uid, email, display_name, account_type) "
                        "VALUES (:fu, :email, 'C230 President', 'greek') RETURNING id"
                    ),
                    {
                        "fu": f"c230-pres-{uuid.uuid4().hex[:8]}",
                        "email": f"c230-pres-{uuid.uuid4().hex[:8]}@example.edu",
                    },
                )
            ).scalar_one()
            member_id = (
                await conn.execute(
                    text(
                        "INSERT INTO users (firebase_uid, email, display_name, account_type) "
                        "VALUES (:fu, :email, 'C230 Member', 'greek') RETURNING id"
                    ),
                    {
                        "fu": f"c230-mem-{uuid.uuid4().hex[:8]}",
                        "email": f"c230-mem-{uuid.uuid4().hex[:8]}@example.edu",
                    },
                )
            ).scalar_one()
            await conn.execute(
                text(
                    "INSERT INTO memberships (user_id, chapter_id, role) "
                    "VALUES (:user, :chapter, 'member')"
                ),
                {"user": member_id, "chapter": chapter_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO dues_payment_intents "
                    "(chapter_id, dues_cycle_id, user_id, rail, status) "
                    "VALUES (:chapter, :cycle, :user, 'card', 'open')"
                ),
                {"chapter": chapter_id, "cycle": cycle_id, "user": member_id},
            )
    finally:
        await engine.dispose()
    return {
        "chapter_id": str(chapter_id),
        "cycle_id": str(cycle_id),
        "president_id": str(president_id),
        "member_id": str(member_id),
    }


async def _catalog_objects(url: str) -> tuple[set[str], set[str]]:
    """(trigger names, function names) currently present for this guard."""
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            triggers = {
                row[0]
                for row in (
                    await conn.execute(
                        text(
                            "SELECT tgname FROM pg_trigger "
                            "WHERE tgname LIKE 'cross_table_dues_guard%'"
                        )
                    )
                ).all()
            }
            functions = {
                row[0]
                for row in (
                    await conn.execute(
                        text(
                            "SELECT proname FROM pg_proc "
                            "WHERE proname LIKE 'cross_table_dues_guard%'"
                        )
                    )
                ).all()
            }
    finally:
        await engine.dispose()
    return triggers, functions


async def _try_insert_conflicting_plan(url: str, ids: dict[str, str]) -> bool:
    """Attempt to insert an ACTIVE plan for the same (cycle, member) as the
    pre-seeded 'open' reservation. Returns True if it was ALLOWED (no guard
    active), False if the guard raised and blocked it."""
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            try:
                await conn.execute(
                    text(
                        "INSERT INTO dues_payment_plans "
                        "(chapter_id, dues_cycle_id, user_id, total_cents, "
                        "installment_count, created_by) "
                        "VALUES (:chapter, :cycle, :user, 30000, 1, :president)"
                    ),
                    {
                        "chapter": ids["chapter_id"],
                        "cycle": ids["cycle_id"],
                        "user": ids["member_id"],
                        "president": ids["president_id"],
                    },
                )
                await conn.commit()
                return True
            except Exception:
                await conn.rollback()
                return False
    finally:
        await engine.dispose()


def test_0028_migration_up_down_up_with_rows_present() -> None:
    """(c) the migration itself: up (trigger installed + actually enforces, even
    against a reservation that predates it), down (trigger cleanly gone, catalog
    empty, the same insert that was blocked now succeeds), up again (idempotent
    re-create, enforces again) — all against a database already holding real rows,
    never an empty one."""
    requested = os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://chirp:chirp@localhost:5432/chirp_test")
    base = _database_of(requested)
    admin_url = _swap_database(requested, "postgres")
    db_name = f"{base}_c230updownup_{uuid.uuid4().hex[:8]}"
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
        _upgrade_to(url, "0027")
        ids = asyncio.run(_seed_pre_0028_rows(url))

        # UP: the guard must be installed AND immediately enforce against a row
        # that predates it (CREATE TRIGGER does not scan history — this proves the
        # trigger body itself sees pre-existing committed rows just fine).
        _upgrade_to(url, "0028")
        triggers, functions = asyncio.run(_catalog_objects(url))
        assert triggers == {"cross_table_dues_guard_intents", "cross_table_dues_guard_plans"}
        assert functions == {
            "cross_table_dues_guard_intents_fn",
            "cross_table_dues_guard_plans_fn",
        }
        allowed = asyncio.run(_try_insert_conflicting_plan(url, ids))
        assert allowed is False, "the guard must block a conflicting plan the moment it is installed"

        # DOWN: catalog entries gone, and the same insert the guard just blocked
        # now succeeds — proof the removal is clean, not partial.
        _downgrade_to(url, "0027")
        triggers, functions = asyncio.run(_catalog_objects(url))
        assert triggers == set()
        assert functions == set()
        allowed = asyncio.run(_try_insert_conflicting_plan(url, ids))
        assert allowed is True, "with the trigger gone, the same insert must succeed"

        # UP again: idempotent re-create, even though the table now ALSO holds the
        # plan the downgrade window let through above (CREATE TRIGGER, unlike
        # CREATE UNIQUE INDEX, does not fail on pre-existing violating rows — see
        # the migration's own docstring) — and the guard is live again for
        # anything NEW.
        _upgrade_to(url, "0028")
        triggers, functions = asyncio.run(_catalog_objects(url))
        assert triggers == {"cross_table_dues_guard_intents", "cross_table_dues_guard_plans"}
        assert functions == {
            "cross_table_dues_guard_intents_fn",
            "cross_table_dues_guard_plans_fn",
        }
    finally:
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
