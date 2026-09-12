"""backend/scripts/seed_prod_search_fixtures.py: the prod-safe QA search-fixture
seeder/teardown tool (board c380, manager ruling chirps-17 Sep 11).

Imports the module directly off backend/scripts/ (not a package) the same way
seed_dev_accounts.py's own callers would, rather than assuming backend/scripts is
importable as `backend.scripts.seed_prod_search_fixtures`.

R5 (manager ruling, required): the integration tests below seed the real 8-fixture
roster onto a constructed campus and drive the REAL GET /users/search through the
test client as a campus-verified caller, proving the fixtures are reachable through
the actual reachability rule (app.core.reachability) rather than merely present in
the users table. Every precondition search enforces (the caller's own campus
verification; the fixtures' campus_id) is constructed and asserted to exist before
the search assertions rely on it.

R6 (manager ruling, required): a fixture must never be able to authenticate. Real
Firebase uids are opaque, library-generated ~28-character base62 strings a client
never chooses (see the module's own docstring); the human-typed qa-fixture-<slug>
marker this script writes can never collide with that shape, and the tests below
pin both the marker's presence and that non-collision structurally.
"""
from __future__ import annotations

import re
import sys
import uuid
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from tests.conftest import ApiUser, MakeCampus, MakeUser, share_verified_campus

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR / "scripts"))
import seed_prod_search_fixtures as fx  # noqa: E402


# ---------------------------------------------------------------------------
# DB-facing helpers (raw SQL / direct session use, same style as conftest's
# _set_ghost / _suspend and test_c322_people_search.py's own helpers).
# ---------------------------------------------------------------------------


async def _fixture_rows_for_campus(campus_id: str) -> list[dict]:
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, email, display_name, firebase_uid, campus_id, "
                    "campus_verified_at, is_platform_admin FROM users "
                    "WHERE campus_id = :campus_id AND email LIKE :pattern ORDER BY email"
                ),
                {"campus_id": campus_id, "pattern": f"%@{fx.FIXTURE_EMAIL_DOMAIN}"},
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def _campus_verified_at(user_id: str):
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        row = (
            await session.execute(
                text("SELECT campus_verified_at FROM users WHERE id = :id"), {"id": user_id}
            )
        ).first()
    return row[0] if row else None


async def _insert_real_looking_user(campus_id: str, display_name: str, email: str) -> str:
    """A row this suite must NEVER touch: an ordinary user, unmarked, same campus."""
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        result = await session.execute(
            text(
                "INSERT INTO users (firebase_uid, email, display_name, account_type, "
                "campus_id, campus_verified_at) VALUES (:uid, :email, :name, 'non_greek', "
                ":campus_id, now()) RETURNING id"
            ),
            {
                "uid": f"real-{uuid.uuid4().hex}",
                "email": email,
                "name": display_name,
                "campus_id": campus_id,
            },
        )
        user_id = str(result.scalar_one())
        await session.commit()
    return user_id


async def _user_exists(user_id: str) -> dict | None:
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        row = (
            await session.execute(
                text("SELECT id, display_name FROM users WHERE id = :id"), {"id": user_id}
            )
        ).mappings().first()
    return dict(row) if row else None


async def _search(client: AsyncClient, caller: ApiUser, q: str) -> list[dict]:
    response = await client.get("/users/search", params={"q": q}, headers=caller.headers)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Pure unit tests: no DB.
# ---------------------------------------------------------------------------


def test_assert_via_proxy_accepts_only_localhost_5433(
    capsys: pytest.CaptureFixture,
) -> None:
    good = "postgresql+asyncpg://chirp:secret@localhost:5433/chirp"
    dev_port = "postgresql+asyncpg://chirp:secret@localhost:5432/chirp"
    remote_host = "postgresql+asyncpg://chirp:secret@db.example.com:5433/chirp"
    socket_form = "postgresql+asyncpg://chirp:secret@/chirp?host=/cloudsql/proj:region:inst"

    fx._assert_via_proxy(good)  # must not raise

    for bad in (dev_port, remote_host, socket_form):
        with pytest.raises(SystemExit) as exc:
            fx._assert_via_proxy(bad)
        assert exc.value.code == 2
        # 127.0.0.1 also allowed alongside localhost, so the good case above is not the
        # only host this proves; port 5433 is what the message must name.
        assert "5433" in capsys.readouterr().err


def test_assert_via_proxy_never_echoes_the_url_it_refused(
    capsys: pytest.CaptureFixture,
) -> None:
    """R7: never print any part of DATABASE_URL, even in a refusal."""
    secret_marker = "s3cr3t-marker-do-not-leak"
    bad = f"postgresql+asyncpg://chirp:{secret_marker}@evil.example.com:9999/chirp"
    with pytest.raises(SystemExit) as exc:
        fx._assert_via_proxy(bad)
    assert exc.value.code == 2
    stderr = capsys.readouterr().err
    assert secret_marker not in stderr
    assert "evil.example.com" not in stderr
    assert "9999" not in stderr


def test_main_refuses_missing_database_url_before_any_db_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    def forbidden_factory():
        pytest.fail("main() opened a database connection before checking DATABASE_URL")

    monkeypatch.setattr("app.db.get_session_factory", forbidden_factory)
    with pytest.raises(SystemExit) as exc:
        fx.main(["--campus-slug", "uncg"])
    assert exc.value.code == 2
    assert "DATABASE_URL" in capsys.readouterr().err


def test_fixture_specs_carry_the_never_issued_uid_marker() -> None:
    """R6 structural half: the marker is present, and it can never look like a real
    Firebase uid (opaque, library-generated, 28-char base62 with no hyphens)."""
    specs = fx._fixture_specs("uncg")
    assert len(specs) == 8
    firebase_shape = re.compile(r"^[A-Za-z0-9]{28}$")
    for spec in specs:
        assert spec["firebase_uid"].startswith(fx.FIXTURE_UID_PREFIX)
        assert spec["email"].endswith(f"@{fx.FIXTURE_EMAIL_DOMAIN}")
        assert spec["display_name"].startswith(fx.FIXTURE_NAME_PREFIX)
        assert not firebase_shape.fullmatch(spec["firebase_uid"])


def test_fixture_specs_are_campus_scoped_and_never_collide_across_campuses() -> None:
    """The bug this test guards: emails/uids used to be keyed on the fixture slug
    alone, so two campuses' rosters collided on users.email / users.firebase_uid
    (both globally UNIQUE). Folding the target campus's own slug in must make
    every campus's 8 specs textually distinct from every other campus's."""
    specs_a = fx._fixture_specs("campus-a")
    specs_b = fx._fixture_specs("campus-b")
    emails_a = {spec["email"] for spec in specs_a}
    emails_b = {spec["email"] for spec in specs_b}
    uids_a = {spec["firebase_uid"] for spec in specs_a}
    uids_b = {spec["firebase_uid"] for spec in specs_b}
    assert emails_a.isdisjoint(emails_b)
    assert uids_a.isdisjoint(uids_b)


# ---------------------------------------------------------------------------
# Job-function tests: against the scratch DB.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_apply_creates_marked_rows(client: AsyncClient, make_campus: MakeCampus) -> None:
    campus_id = await make_campus()

    result = await fx.run_seed_job(campus_id=campus_id, apply=True)

    assert result == {
        "mode": "apply", "action": "seed", "campus_id": campus_id, "created": 8, "updated": 0,
    }
    rows = await _fixture_rows_for_campus(campus_id)
    assert len(rows) == 8
    for row in rows:
        assert row["email"].endswith(f"@{fx.FIXTURE_EMAIL_DOMAIN}")
        assert row["display_name"].startswith(fx.FIXTURE_NAME_PREFIX)


@pytest.mark.asyncio
async def test_seed_is_idempotent(client: AsyncClient, make_campus: MakeCampus) -> None:
    campus_id = await make_campus()
    first = await fx.run_seed_job(campus_id=campus_id, apply=True)
    assert first["created"] == 8

    second = await fx.run_seed_job(campus_id=campus_id, apply=True)

    assert second == {
        "mode": "apply", "action": "seed", "campus_id": campus_id, "created": 0, "updated": 8,
    }
    rows = await _fixture_rows_for_campus(campus_id)
    assert len(rows) == 8, "a second apply must not duplicate rows"


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(client: AsyncClient, make_campus: MakeCampus) -> None:
    campus_id = await make_campus()
    before = await _fixture_rows_for_campus(campus_id)
    assert before == [], "constructed starting state: a fresh campus has zero fixture rows"

    result = await fx.run_seed_job(campus_id=campus_id, apply=False)

    assert result["mode"] == "dry_run"
    assert result["would_create"] == 8
    assert result["would_update"] == 0
    after = await _fixture_rows_for_campus(campus_id)
    assert after == [], "a dry run must write nothing, not merely report a plan"


@pytest.mark.asyncio
async def test_teardown_deletes_exactly_marked_rows_and_nothing_else(
    client: AsyncClient, make_campus: MakeCampus,
) -> None:
    campus_id = await make_campus()
    real_user_id = await _insert_real_looking_user(
        campus_id, "Jamie Rivera", "jamie.rivera@uncg.edu"
    )
    seeded = await fx.run_seed_job(campus_id=campus_id, apply=True)
    assert seeded["created"] == 8

    result = await fx.run_teardown_job(campus_id=campus_id, apply=True)

    assert result == {
        "mode": "apply", "action": "teardown", "campus_id": campus_id, "deleted": 8,
    }
    assert await _fixture_rows_for_campus(campus_id) == []
    survivor = await _user_exists(real_user_id)
    assert survivor is not None, "teardown must not touch an unmarked real-looking user"
    assert survivor["display_name"] == "Jamie Rivera"


@pytest.mark.asyncio
def test_safe_campus_slug_regex_rejects_what_cannot_go_in_an_email_local_part() -> None:
    """The charset guard must actually reject, or it is decoration.

    Each bad string is a slug shape that would build a malformed fixture email
    (an "@", a space, a "+", which is this tool's own fixture/campus separator)
    or read as a LIKE metacharacter; an ordinary slug must still pass.
    """
    assert fx.SAFE_CAMPUS_SLUG_RE.match("state-university")
    assert fx.SAFE_CAMPUS_SLUG_RE.match("u.of.x_2")
    for bad in ("evil@campus", "two words", "pct%slug", "UPPER", "", "sl/ash", "plus+slug"):
        assert not fx.SAFE_CAMPUS_SLUG_RE.match(bad), bad


@pytest.mark.asyncio
async def test_seed_refuses_a_campus_whose_slug_is_not_email_safe(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    """A campus whose slug carries an "@" is refused before any write.

    The discriminating condition is constructed, not incidental: an ordinary
    campus is created and its slug is then set to an unsafe value, and that row
    is read back and asserted before the refusal is exercised.
    """
    from app.db import get_session_factory

    campus_id = await make_campus()
    async with get_session_factory()() as session:
        await session.execute(
            text("UPDATE campuses SET slug = :slug WHERE id = :id"),
            {"slug": "evil@campus", "id": campus_id},
        )
        await session.commit()
        slug = (
            await session.execute(
                text("SELECT slug FROM campuses WHERE id = :id"), {"id": campus_id}
            )
        ).scalar_one()
    assert slug == "evil@campus", "the unsafe-slug precondition must exist before the assertion"

    with pytest.raises(fx.CampusNotFound) as caught:
        await fx.run_seed_job(campus_id=campus_id, apply=True)
    assert "not safe to fold" in str(caught.value)
    assert await _fixture_rows_for_campus(campus_id) == [], "a refused seed must write nothing"


async def test_seeding_a_second_campus_does_not_relocate_the_first_campus_fixtures(
    client: AsyncClient, make_campus: MakeCampus,
) -> None:
    """The bug found in review: seeding campus B used to look up "existing"
    fixtures by email alone, find campus A's 8 rows (created first), and UPDATE
    them onto campus B's campus_id — leaving campus A with zero fixtures and no
    error. Construct exactly that sequence (seed A, then seed a DIFFERENT
    campus B) and assert BOTH campuses independently end up with their own 8
    fixture rows, and that campus A's original fixture ids are unchanged."""
    campus_a = await make_campus()
    campus_b = await make_campus()

    seeded_a = await fx.run_seed_job(campus_id=campus_a, apply=True)
    assert seeded_a == {
        "mode": "apply", "action": "seed", "campus_id": campus_a, "created": 8, "updated": 0,
    }
    rows_a_before = await _fixture_rows_for_campus(campus_a)
    assert len(rows_a_before) == 8
    ids_a_before = {row["id"] for row in rows_a_before}

    seeded_b = await fx.run_seed_job(campus_id=campus_b, apply=True)

    # The bug's exact signature was created=0, updated=8 here (it "found" campus
    # A's rows via the email-only lookup and relocated them). The fix must
    # create a genuinely new 8 for campus B instead.
    assert seeded_b == {
        "mode": "apply", "action": "seed", "campus_id": campus_b, "created": 8, "updated": 0,
    }

    rows_a_after = await _fixture_rows_for_campus(campus_a)
    rows_b_after = await _fixture_rows_for_campus(campus_b)
    assert len(rows_a_after) == 8, "campus A must still have its own 8 fixtures"
    assert len(rows_b_after) == 8, "campus B must have its own independent 8 fixtures"
    assert {row["id"] for row in rows_a_after} == ids_a_before, (
        "campus A's original fixture rows must be untouched, not relocated"
    )
    emails_a = {row["email"] for row in rows_a_after}
    emails_b = {row["email"] for row in rows_b_after}
    assert emails_a.isdisjoint(emails_b), "the two campuses' fixture emails must never collide"


@pytest.mark.asyncio
async def test_teardown_dry_run_deletes_nothing(client: AsyncClient, make_campus: MakeCampus) -> None:
    campus_id = await make_campus()
    seeded = await fx.run_seed_job(campus_id=campus_id, apply=True)
    assert seeded["created"] == 8

    result = await fx.run_teardown_job(campus_id=campus_id, apply=False)

    assert result["mode"] == "dry_run"
    assert result["would_delete"] == 8
    assert len(await _fixture_rows_for_campus(campus_id)) == 8, (
        "a teardown dry run must delete nothing"
    )


@pytest.mark.asyncio
async def test_refuses_unresolvable_campus(client: AsyncClient) -> None:
    from app.db import get_session_factory
    from sqlalchemy import select

    from app import models

    bogus_campus_id = str(uuid.uuid4())
    async with get_session_factory()() as session:
        existing = (
            await session.execute(
                select(models.Campus).where(models.Campus.id == uuid.UUID(bogus_campus_id))
            )
        ).scalar_one_or_none()
    assert existing is None, "constructed precondition: this campus id must not exist"

    with pytest.raises(fx.CampusNotFound):
        await fx.run_seed_job(campus_id=bogus_campus_id, apply=True)

    async with get_session_factory()() as session:
        leftover = (
            await session.execute(
                text("SELECT count(*) FROM users WHERE email LIKE :pattern"),
                {"pattern": f"%@{fx.FIXTURE_EMAIL_DOMAIN}"},
            )
        ).scalar_one()
    assert leftover == 0, "a checked refusal must happen before any INSERT is attempted"


@pytest.mark.asyncio
async def test_seeded_fixture_rows_have_no_authenticatable_identity(
    client: AsyncClient, make_campus: MakeCampus,
) -> None:
    """R6 DB half: every seeded row carries the never-issued uid marker and no
    campus verification, is_platform_admin, or suspension state of its own."""
    campus_id = await make_campus()
    result = await fx.run_seed_job(campus_id=campus_id, apply=True)
    assert result["created"] == 8

    rows = await _fixture_rows_for_campus(campus_id)
    assert len(rows) == 8
    for row in rows:
        assert row["firebase_uid"].startswith(fx.FIXTURE_UID_PREFIX)
        assert row["campus_verified_at"] is None
        assert row["is_platform_admin"] is False


# ---------------------------------------------------------------------------
# R5 (required): the real GET /users/search endpoint, through the test client,
# as a campus-verified caller.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_finds_the_one_fixture_matching_a_narrow_query(
    client: AsyncClient, make_user: MakeUser
) -> None:
    caller = await make_user("Searching Caller")
    campus_id = await share_verified_campus(caller.id)
    # Construct + assert the precondition search enforces before relying on it:
    # the caller must actually be campus-verified (app.core.campus_access).
    assert await _campus_verified_at(caller.id) is not None

    seeded = await fx.run_seed_job(campus_id=campus_id, apply=True)
    assert seeded["created"] == 8
    # And the fixtures must actually carry the campus_id reachability keys off of.
    rows = await _fixture_rows_for_campus(campus_id)
    assert {row["campus_id"] for row in rows} == {uuid.UUID(campus_id)}

    results = await _search(client, caller, "Avery")

    assert {r["display_name"] for r in results} == {"QA Fixture: Avery Chen"}


@pytest.mark.asyncio
async def test_search_finds_all_fixtures_and_never_the_caller_even_when_name_matches(
    client: AsyncClient, make_user: MakeUser
) -> None:
    # The caller's own display_name deliberately ALSO matches the "QA Fixture" query,
    # so self-exclusion is proven against this exact fixture set (a query that would
    # return 9 rows if self-exclusion were broken), not assumed from an unrelated case.
    caller = await make_user("QA Fixture: Self Caller")
    campus_id = await share_verified_campus(caller.id)
    assert await _campus_verified_at(caller.id) is not None

    seeded = await fx.run_seed_job(campus_id=campus_id, apply=True)
    assert seeded["created"] == 8

    results = await _search(client, caller, "QA Fixture")

    result_ids = {r["id"] for r in results}
    assert caller.id not in result_ids
    expected_names = {f"{fx.FIXTURE_NAME_PREFIX}{name}" for _slug, name in fx.ROSTER}
    assert {r["display_name"] for r in results} == expected_names
    assert len(results) == 8


@pytest.mark.asyncio
async def test_search_on_a_different_campus_finds_no_fixtures(
    client: AsyncClient, make_campus: MakeCampus, make_user: MakeUser
) -> None:
    fixtures_campus_id = await make_campus()
    seeded = await fx.run_seed_job(campus_id=fixtures_campus_id, apply=True)
    assert seeded["created"] == 8

    caller = await make_user("Away Campus Caller")
    away_campus_id = await share_verified_campus(caller.id)
    assert away_campus_id != fixtures_campus_id
    assert await _campus_verified_at(caller.id) is not None

    results = await _search(client, caller, "QA Fixture")

    assert results == []
