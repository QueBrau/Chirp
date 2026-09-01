"""Shared fixtures: postgres probe (skip without a DB), alembic schema, ASGI client, API helpers."""
from __future__ import annotations

import asyncio
import base64
import os
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_TEST_DATABASE_URL = "postgresql+asyncpg://chirp:chirp@localhost:5432/chirp_test"
# Connected to for CREATE/DROP DATABASE only. Always present on both the official
# postgres image and a Homebrew install.
MAINTENANCE_DB = "postgres"
# Written as a COMMENT ON DATABASE the moment a run database is created. The sweep
# drops ONLY databases carrying it, so a name that merely looks like ours is never
# touched. Second field is the creation time in epoch seconds.
RUN_DB_MARKER = "chirp-pytest-run"
# A run that outlives this is not running any more, whatever the pid says. Guards
# against pid reuse making an abandoned database permanently un-sweepable.
RUN_DB_MAX_AGE_SECONDS = 12 * 60 * 60


def _run_db_name(base: str) -> str:
    """Per-run database name: base + this process, plus the xdist worker if any.

    The pid is not decoration — _drop_stale_run_databases uses it to tell a
    database abandoned by a crashed run from one a live run is still using.
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER", "")
    suffix = f"_p{os.getpid()}" + (f"_{worker}" if worker else "")
    return f"{base}{suffix}"


def _swap_database(url: str, database: str) -> str:
    """Return `url` pointing at a different database on the same server.

    The query string is preserved. Dropping it would quietly discard sslmode,
    application_name and friends, which a hosted or staging Postgres may require —
    and the resulting connection failure would surface as "postgres not available"
    rather than as the misconfiguration it is.
    """
    head, _, tail = url.rpartition("/")
    _, sep, query = tail.partition("?")
    return f"{head}/{database}{sep}{query}"


def _database_of(url: str) -> str:
    return url.rpartition("/")[2].partition("?")[0]


# ---------------------------------------------------------------------------
# Database availability + schema
# ---------------------------------------------------------------------------


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Under CHIRP_REQUIRE_DB=1, refuse to exit green on a collapsed run (board card c103).

    The fail-closed probe below covers the cause we actually measured — an
    unreachable database. This covers the SHAPE of the failure regardless of
    cause: any drift that turns tests into skips instead of results.

    Zero is the right ceiling rather than a guess, because CI runs BOTH services
    and every skip in this suite is conditional on a missing one. The observed CI
    run for PR #21 was '179 passed' with nothing skipped. If a legitimate skip is
    ever added, raise CHIRP_MAX_SKIPS deliberately — a number someone had to
    change on purpose is the point, not a chore.
    """
    if os.environ.get("CHIRP_REQUIRE_DB") != "1":
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        return
    skipped = len(reporter.stats.get("skipped", []))
    allowed = int(os.environ.get("CHIRP_MAX_SKIPS", "0"))
    if skipped > allowed:
        reporter.write_line("")
        reporter.write_line(
            f"CHIRP_REQUIRE_DB=1 and {skipped} test(s) skipped, ceiling is {allowed}. "
            "A skip is not a pass: this run is being failed so it cannot be read "
            "as evidence. Find what went missing (a service, a fixture, a path) "
            "rather than raising the ceiling to make it quiet.",
            red=True,
            bold=True,
        )
        session.exitstatus = 1


async def _maintenance_execute(admin_url: str, statements: list[str]) -> None:
    """Run CREATE/DROP DATABASE, which cannot execute inside a transaction."""
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            for statement in statements:
                await conn.execute(text(statement))
    finally:
        await engine.dispose()


async def _drop_stale_run_databases(admin_url: str, base: str) -> None:
    """Drop abandoned run databases — and ONLY ones this fixture provably created.

    Without this, every crashed or killed run leaks a database on a long-lived
    machine like Jose's. But the sweep runs on the hot path of every `pytest`
    invocation and force-drops, which disconnects live sessions, so it has to be
    certain about what it is deleting. Two independent guards, both required:

    1. PROVENANCE. Only databases carrying the RUN_DB_MARKER comment are even
       considered. Matching the name shape is deliberately NOT enough: a human
       scratch database that happened to be called `chirp_test_p9` would parse as
       "pid 9, not running, therefore abandoned" and be destroyed underneath
       someone. Name shape is a hint; the comment is the proof.
    2. ABANDONMENT. Either the pid is gone, or the database is older than
       RUN_DB_MAX_AGE_SECONDS. The age check exists because pids are reused: a
       crashed run's pid can be reassigned to some long-lived process, and from
       then on `os.kill(pid, 0)` succeeds forever and the database would never be
       reclaimed. No test run lasts twelve hours, so age settles it.
    """
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT datname, shobj_description(oid, 'pg_database') "
                    "FROM pg_database WHERE datname LIKE :pattern"
                ),
                {"pattern": f"{base}\\_p%"},
            )
            candidates = [(row[0], row[1]) for row in result]
    finally:
        await engine.dispose()

    now = time.time()
    stale = []
    for name, comment in candidates:
        if not comment or not comment.startswith(RUN_DB_MARKER):
            continue  # not ours, whatever it is called
        created_at = comment[len(RUN_DB_MARKER) :].strip()
        if created_at.isdigit() and now - int(created_at) > RUN_DB_MAX_AGE_SECONDS:
            stale.append(name)
            continue
        pid_part = name[len(base) + 2 :].partition("_")[0]
        if not pid_part.isdigit():
            continue
        pid = int(pid_part)
        if pid == os.getpid():
            stale.append(name)  # our own leftover from a previous run, same pid
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            stale.append(name)
        except PermissionError:
            pass  # alive, owned by someone else

    for name in stale:
        try:
            await _maintenance_execute(admin_url, [f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'])
        except Exception:
            pass  # a concurrent run may have cleaned it up first; not our problem


@pytest.fixture(scope="session")
def database_url() -> AsyncIterator[str]:
    """A PRIVATE database for this test run, created here and dropped at the end.

    Board card c106. Every test truncates the whole table set (see the `client`
    fixture), and TRUNCATE takes an AccessExclusiveLock. Two pytest runs sharing
    one database therefore deadlock each other — one waiting on AccessExclusiveLock
    while the other holds RowShareLock and waits back. Observed on this machine
    with several sessions active: 72 failed, 65 passed, 39 errors, all of them
    `asyncpg.exceptions.DeadlockDetectedError` on the TRUNCATE. That wall of red
    looks exactly like a broken branch, which makes it the more expensive twin of
    c103's false green: someone "fixes" code that was never broken, or shrugs off
    a real failure as "probably just the collision".

    So each run gets `chirp_test_p<pid>` instead of sharing `chirp_test`. Runs
    stop being able to see each other at all, rather than being asked to take
    turns, and `-n auto` becomes available later for free.

    TEST_DATABASE_URL still selects the SERVER and the base name; only the
    database is swapped. Unreachable server skips locally and FAILS under
    CHIRP_REQUIRE_DB=1 (board card c103).
    """
    requested = os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
    base = _database_of(requested)
    admin_url = _swap_database(requested, MAINTENANCE_DB)
    run_db = _run_db_name(base)
    url = _swap_database(requested, run_db)

    async def _probe() -> None:
        engine = create_async_engine(admin_url)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        finally:
            await engine.dispose()

    try:
        asyncio.run(_probe())
    except Exception as exc:
        # Skipping is the right call on a laptop with no DB — that is why this
        # is here, and it stays. It is the WRONG call in CI, where nobody reads
        # the skip count: a postgres service that fails to come up would let the
        # entire DB-backed suite skip and still exit 0, so CI posts a green check
        # having proved nothing. Measured on main before this landed, with the
        # URL pointed at a dead port: 22 passed, 157 skipped, exit code 0. Every
        # readiness claim we have made ("168 pass", "176 pass") is exactly the
        # evidence that failure mode fabricates. Same family as c41 — the silent
        # skip that reads as a pass.
        if os.environ.get("CHIRP_REQUIRE_DB") == "1":
            pytest.fail(
                f"CHIRP_REQUIRE_DB=1 but the postgres server is unreachable at {admin_url}: "
                f"{type(exc).__name__}: {exc}. Refusing to skip the suite and "
                f"report success — fix the database service, do not unset the flag.",
                pytrace=False,
            )
        pytest.skip("postgres not available — docker compose up db")

    asyncio.run(_drop_stale_run_databases(admin_url, base))
    try:
        asyncio.run(
            _maintenance_execute(
                admin_url,
                [
                    f'DROP DATABASE IF EXISTS "{run_db}" WITH (FORCE)',
                    f'CREATE DATABASE "{run_db}"',
                    # Stamped immediately after creation so the sweep can tell this
                    # database apart from anything else sharing the name shape.
                    f"COMMENT ON DATABASE \"{run_db}\" IS '{RUN_DB_MARKER} {int(time.time())}'",
                ],
            )
        )
    except Exception as exc:
        # The probe above only proves the server accepts a CONNECT. Creating the
        # run database additionally needs CREATEDB, and a role that has one but
        # not the other would otherwise surface as a raw traceback out of a
        # fixture rather than as the configuration problem it is.
        message = (
            f"Connected to the postgres server but could not create the per-run test "
            f"database {run_db}: {type(exc).__name__}: {exc}. The role in "
            f"TEST_DATABASE_URL needs CREATEDB (board card c106)."
        )
        if os.environ.get("CHIRP_REQUIRE_DB") == "1":
            pytest.fail(message, pytrace=False)
        pytest.skip(message)
    try:
        yield url
    finally:
        # Best effort: a failed drop leaks one database, and the stale sweep above
        # reclaims it on the next run. Failing teardown here would turn a green
        # suite red for a housekeeping problem, which is not worth it.
        try:
            asyncio.run(
                _maintenance_execute(admin_url, [f'DROP DATABASE IF EXISTS "{run_db}" WITH (FORCE)'])
            )
        except Exception:
            pass


@pytest.fixture(scope="session")
def migrated_db(database_url: str) -> str:
    """Point app settings at this run's database and build its schema with alembic.

    Runs the real migration so the ledger_append_only trigger is installed
    (defense-in-depth under test, per SPEC §8.2).

    There is deliberately no DROP SCHEMA public / CREATE SCHEMA public here any
    more. That existed to reset a database shared across runs, and c106 made the
    database private and brand new, so it is redundant — and it could not work
    anyway: a database created by `chirp` inherits its public schema from
    template1, where the schema is owned by the bootstrap superuser, so the drop
    fails with `must be owner of schema public` for every non-superuser. That is
    exactly what it did on Jose's Homebrew PG14 (154 errors) while passing in CI,
    where the container's POSTGRES_USER is a superuser and owns everything — a
    works-in-CI-fails-locally split that would have cost somebody an afternoon.
    """
    os.environ["DATABASE_URL"] = database_url
    os.environ["AUTH_MODE"] = "emulated"

    from app.config import get_settings

    get_settings.cache_clear()

    from alembic import command
    from alembic.config import Config as AlembicConfig

    alembic_cfg = AlembicConfig(str(BACKEND_DIR / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(alembic_cfg, "head")
    return database_url


@pytest.fixture(autouse=True)
def _reset_rate_limits() -> None:
    """Clear the in-process rate-limit windows before every test (board c259).

    app.services.rate_limit keeps its local fallback windows in a MODULE-GLOBAL dict,
    and the suite runs with env == "local", so that fallback is the live limiter here.
    Without this, limits leak between tests in one process.

    Per-user limits would mostly survive that on their own — every test bootstraps
    users with fresh uuid4 uids, so their keys never collide. The per-IP limit on
    account creation would NOT: httpx's ASGITransport reports the same client address
    for every request in the run, so all ~750 tests share ONE bootstrap key and the
    suite would start 429ing partway through for reasons that have nothing to do with
    the test that failed.

    This is isolation of global state, not a relaxation of any limit: the tests that
    are ABOUT limits still drive them to the ceiling within a single test, which is
    where a limit should be proven anyway.
    """
    from app.services import rate_limit

    rate_limit._reset_all()


@pytest.fixture
async def client(migrated_db: str) -> AsyncIterator[AsyncClient]:
    """Fresh ASGI client per test: engine bound to this test's loop, all tables truncated."""
    import app.db as app_db
    from app import models  # noqa: F401  # imported so Base.metadata is fully populated
    from app.main import create_app

    app_db._engine = None
    app_db._session_factory = None
    engine = app_db.get_engine()
    table_names = ", ".join(t.name for t in app_db.Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        # Row-level triggers (ledger_append_only) do not fire on TRUNCATE.
        try:
            await conn.execute(text(f"TRUNCATE TABLE {table_names} CASCADE"))
        except Exception as exc:  # pragma: no cover - diagnostic path
            if "deadlock" not in str(exc).lower():
                raise
            # Cannot happen once every run has its own database (c106), but if
            # someone points two runs at one database again, this is the single
            # most misread error in the suite: it looks like a broken branch and
            # is not. Name the cause here rather than leave 39 identical
            # DeadlockDetectedError tracebacks to be interpreted.
            raise RuntimeError(
                f"Deadlock while truncating {_database_of(str(engine.url))}. This is almost "
                "certainly two pytest runs sharing one database, not a bug in the code under "
                "test: TRUNCATE takes an AccessExclusiveLock and the runs block each other. "
                "Check with `ps aux | grep [p]ytest`. Board card c106."
            ) from exc

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client

    await engine.dispose()
    app_db._engine = None
    app_db._session_factory = None


# ---------------------------------------------------------------------------
# API-driven helpers (bootstrap/create/invite/join flows, not raw SQL)
# ---------------------------------------------------------------------------


@dataclass
class ApiUser:
    """A bootstrapped user plus the auth headers that act as them (emulated mode)."""

    id: str
    firebase_uid: str
    email: str
    headers: dict[str, str]


@dataclass
class ChapterSetup:
    """A chapter with its president and one member holding the requested role."""

    chapter_id: str
    member: ApiUser
    president: ApiUser


MakeUser = Callable[..., Awaitable[ApiUser]]
MakeCampus = Callable[[], Awaitable[str]]
MakeChapterWith = Callable[..., Awaitable[ChapterSetup]]
RegisterDevice = Callable[..., Awaitable[dict[str, Any]]]


def b64(data: bytes) -> str:
    """Base64-encode opaque test bytes for *_b64 JSON fields."""
    return base64.b64encode(data).decode("ascii")


@pytest.fixture
def make_user(client: AsyncClient) -> MakeUser:
    """Factory: bootstrap a user with a unique firebase_uid via POST /auth/bootstrap."""

    async def _make_user(
        display_name: str = "Test Member", account_type: str = "greek"
    ) -> ApiUser:
        uid = f"uid-{uuid.uuid4().hex}"
        headers = {"X-Debug-Firebase-Uid": uid}
        email = f"{uid}@example.edu"
        response = await client.post(
            "/auth/bootstrap",
            json={
                "email": email,
                "display_name": display_name,
                "account_type": account_type,
            },
            headers=headers,
        )
        assert response.status_code == 201, response.text
        return ApiUser(
            id=response.json()["id"], firebase_uid=uid, email=email, headers=headers
        )

    return _make_user


@pytest.fixture
def make_campus() -> MakeCampus:
    """Factory: insert a campus row (no campus API exists yet) and return its id."""

    async def _make_campus() -> str:
        from app.db import get_session_factory

        async with get_session_factory()() as session:
            result = await session.execute(
                text("INSERT INTO campuses (name, slug) VALUES (:name, :slug) RETURNING id"),
                {"name": "Test Campus", "slug": f"campus-{uuid.uuid4().hex[:12]}"},
            )
            campus_id = str(result.scalar_one())
            await session.commit()
        return campus_id

    return _make_campus


async def _grant_platform_admin(user_id: str) -> None:
    """Flip is_platform_admin directly in the DB (no API grants it — board card c28)."""
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        await session.execute(
            text("UPDATE users SET is_platform_admin = true WHERE id = :id"),
            {"id": user_id},
        )
        await session.commit()


async def set_campus(user_id: str, campus_id: str, *, verified: bool = True) -> None:
    """Pin a user to a campus directly in the DB, verified by default.

    `verified=True` also stamps campus_verified_at, because since c88 a campus_id alone
    no longer opens the campus feed or Chirp — the gate keys on the verification
    timestamp. Almost every caller here is testing a campus FEATURE and wants a fully
    entitled user, so that is the default and those tests read unchanged.

    PASS verified=False TO BUILD THE USER THE GATE EXISTS TO REFUSE: someone with a
    campus derived from a chapter invite (c96) who has never proved an .edu. That is
    the exact shape of the c104/c105 bypass, and test_campus_gate.py uses it.

    Board c85: campus_id used to be accepted in the POST /auth/bootstrap body, and
    that was the ONLY way anything — including this suite — assigned a campus. It was
    also the hole: the value was written unchecked, and the campus feed's guard
    compares against it. Removing it from the schema broke 23 tests, which is the
    clearest possible evidence that the vulnerability WAS the mechanism.

    Same shape as _grant_platform_admin above and for the same reason: no API grants
    this today. The .edu redemption in c86 will be the real writer; until it exists,
    tests set the column the way they set is_platform_admin.
    """
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        await session.execute(
            text(
                "UPDATE users SET campus_id = :campus, "
                "campus_verified_at = CASE WHEN :verified THEN now() ELSE NULL END "
                "WHERE id = :id"
            ),
            {"campus": campus_id, "id": user_id, "verified": verified},
        )
        await session.commit()


async def share_verified_campus(*user_ids: str) -> str:
    """Put several users on ONE new campus, all verified, and return the campus id.

    Board c243. A conversation with no chapter_id used to accept ANY user ids; it now
    requires each named person to be reachable — a shared active chapter, or the caller's
    campus with the caller verified. Tests whose subject is something else entirely
    (pagination cursors, WS fan-out, the analytics payload, group leave) still have to
    hand the route a legitimately reachable pair before they can get a conversation at
    all. This is the one-line way to say "these people are campus peers" without standing
    up chapters those tests do not otherwise need.

    Note this is the opposite default from make_chapter_with, whose members arrive
    UNVERIFIED on purpose (see verify_campus below) — callers here want reachability, not
    a tripwire.
    """
    campus_id = None
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        result = await session.execute(
            text("INSERT INTO campuses (name, slug) VALUES (:name, :slug) RETURNING id"),
            {"name": "Test Campus", "slug": f"campus-{uuid.uuid4().hex[:12]}"},
        )
        campus_id = str(result.scalar_one())
        for user_id in user_ids:
            await session.execute(
                text(
                    "UPDATE users SET campus_id = :campus, campus_verified_at = now() "
                    "WHERE id = :id"
                ),
                {"campus": campus_id, "id": user_id},
            )
        await session.commit()
    return campus_id


async def verify_campus(user_id: str) -> None:
    """Stamp campus_verified_at without touching campus_id.

    For tests whose subject is campus feed CONTENT rather than the gate: since c88 a
    campus-audience post requires the author to have proved an .edu, and a user built by
    make_chapter_with has a campus derived from their chapter (c96) but no verification.
    That is the c104/c105 bypass shape by construction, so those authors are refused —
    correctly. Call this to make such an author a legitimate campus poster.

    Deliberately NOT folded into make_chapter_with: chapter members must keep arriving
    unverified by default, because that is the state test_campus_gate.py asserts against.
    Verifying there would quietly disarm the tripwire for every test in the suite.
    """
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        await session.execute(
            text("UPDATE users SET campus_verified_at = now() WHERE id = :id"),
            {"id": user_id},
        )
        await session.commit()


@pytest.fixture
def make_chapter_with(
    client: AsyncClient, make_user: MakeUser, make_campus: MakeCampus
) -> MakeChapterWith:
    """Factory: create a chapter through the API and add a member with the given role.

    The creator becomes president; other roles join via an e-board invite code
    (POST /chapters/{id}/invites then POST /chapters/join). The creator is granted
    is_platform_admin directly in the DB first, since POST /chapters is admin-only.
    """

    async def _make_chapter_with(role: str = "member") -> ChapterSetup:
        president = await make_user("Chapter President")
        await _grant_platform_admin(president.id)
        campus_id = await make_campus()
        created = await client.post(
            "/chapters",
            json={
                "campus_id": campus_id,
                "org_name": f"Sigma Test {uuid.uuid4().hex[:6]}",
                "chapter_name": "Alpha",
            },
            headers=president.headers,
        )
        assert created.status_code == 201, created.text
        chapter_id = created.json()["id"]

        if role == "president":
            return ChapterSetup(chapter_id=chapter_id, member=president, president=president)

        invite = await client.post(
            f"/chapters/{chapter_id}/invites",
            json={"role": role},
            headers=president.headers,
        )
        assert invite.status_code == 201, invite.text
        member = await make_user(f"Chapter {role.title()}")
        joined = await client.post(
            "/chapters/join",
            json={"code": invite.json()["code"]},
            headers=member.headers,
        )
        assert joined.status_code == 201, joined.text
        return ChapterSetup(chapter_id=chapter_id, member=member, president=president)

    return _make_chapter_with


@pytest.fixture
def register_device(client: AsyncClient) -> RegisterDevice:
    """Factory: register a device via POST /devices with N one-time prekeys."""

    async def _register_device(
        user: ApiUser, one_time_prekey_count: int = 2, registration_id: int = 4242
    ) -> dict[str, Any]:
        body = {
            "device_label": "pytest-device",
            "registration_id": registration_id,
            "identity_key_b64": b64(b"identity-" + uuid.uuid4().bytes),
            "signed_prekey": {
                "key_id": 1,
                "public_key_b64": b64(b"spk-" + uuid.uuid4().bytes),
                "signature_b64": b64(b"sig-" + uuid.uuid4().bytes),
            },
            "one_time_prekeys": [
                {"key_id": key_id, "public_key_b64": b64(f"otk-{key_id}".encode())}
                for key_id in range(1, one_time_prekey_count + 1)
            ],
        }
        response = await client.post("/devices", json=body, headers=user.headers)
        assert response.status_code == 201, response.text
        return response.json()

    return _register_device
