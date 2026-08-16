"""Shared fixtures: postgres probe (skip without a DB), alembic schema, ASGI client, API helpers."""
from __future__ import annotations

import asyncio
import base64
import os
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


# ---------------------------------------------------------------------------
# Database availability + schema
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def database_url() -> str:
    """Test DB URL from TEST_DATABASE_URL (or the chirp_test default).

    Unreachable DB skips locally and FAILS under CHIRP_REQUIRE_DB=1 (board card c103).
    """
    url = os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)

    async def _probe() -> None:
        engine = create_async_engine(url)
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
                f"CHIRP_REQUIRE_DB=1 but the test database is unreachable at {url}: "
                f"{type(exc).__name__}: {exc}. Refusing to skip the suite and "
                f"report success — fix the database service, do not unset the flag.",
                pytrace=False,
            )
        pytest.skip("postgres not available — docker compose up db")
    return url


@pytest.fixture(scope="session")
def migrated_db(database_url: str) -> str:
    """Point app settings at the test DB and build a fresh schema via alembic upgrade head.

    Runs the real migration so the ledger_append_only trigger is installed
    (defense-in-depth under test, per SPEC §8.2).
    """
    os.environ["DATABASE_URL"] = database_url
    os.environ["AUTH_MODE"] = "emulated"

    from app.config import get_settings

    get_settings.cache_clear()

    async def _reset_schema() -> None:
        engine = create_async_engine(database_url, isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as conn:
                await conn.execute(text("DROP SCHEMA public CASCADE"))
                await conn.execute(text("CREATE SCHEMA public"))
        finally:
            await engine.dispose()

    asyncio.run(_reset_schema())

    from alembic import command
    from alembic.config import Config as AlembicConfig

    alembic_cfg = AlembicConfig(str(BACKEND_DIR / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(alembic_cfg, "head")
    return database_url


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
        await conn.execute(text(f"TRUNCATE TABLE {table_names} CASCADE"))

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


async def set_campus(user_id: str, campus_id: str) -> None:
    """Pin a user to a campus directly in the DB.

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
            text("UPDATE users SET campus_id = :campus WHERE id = :id"),
            {"campus": campus_id, "id": user_id},
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
