"""Real purge SQL under a disposable restricted role (c369).

The configured test administrator must be a superuser on a loopback, disposable
Postgres server (as in CI). No application role is elevated. SET ROLE at connection
startup tests effective SQL privileges, not production login/IAM/secret access.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

import app.db as app_db
from app.jobs.purge import run_purge_job
from tests.conftest import RUN_DB_MARKER
from tests.test_c347_key_material_retirement import _device, _kyber, _otk, _signed
from tests.test_purge import (
    _insert_chirp, _insert_chirp_vote, _insert_comment, _insert_post,
    _insert_post_like, _row_exists,
)

CONTENT = ("posts", "post_comments", "post_likes", "chirps", "chirp_votes")
PREKEYS = ("one_time_prekeys", "kyber_prekeys", "signed_prekeys")
UPDATES = {
    "posts": {"id"}, "post_comments": {"id"}, "chirps": {"id", "score"},
    **{table: {"id"} for table in PREKEYS},
}
NOW = datetime(2026, 9, 19, tzinfo=timezone.utc)
OLD_CONTENT = NOW - timedelta(days=31)
OLD_KEY = NOW - timedelta(days=8)
COUNTS = dict(posts=1, post_comments=1, chirps=1, post_likes=1,
              cascaded_post_comments=1, chirp_votes=1)
KEY_COUNTS = dict(retired_one_time_prekeys=1, retired_kyber_one_time_prekeys=1,
                  retired_signed_prekeys=1, retired_revoked_device_prekeys=3)


def _local_rehearsal_url(raw):
    message = "c369 requires an asyncpg loopback URL without query overrides"
    try:
        url = make_url(raw)
    except (ArgumentError, ValueError):
        raise ValueError(message) from None
    # asyncpg query parameters can override the URL's displayed host/database.
    # This guard protects this fixture's grants, not conftest's earlier DB setup.
    if (url.drivername != "postgresql+asyncpg"
            or url.host not in {"localhost", "127.0.0.1", "::1"} or url.query):
        raise ValueError(message)
    return url


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_rehearsal_url_accepts_literal_loopback_without_overrides(host):
    assert _local_rehearsal_url(f"postgresql+asyncpg://test@{host}:5432/test").database == "test"


@pytest.mark.parametrize("raw", [
    "postgresql+asyncpg://test@remote.example/test",
    "postgresql+asyncpg://test@127.0.0.1/test?host=remote.example",
    "postgresql+asyncpg://test@127.0.0.1/test?host=/cloudsql/private",
    "postgresql+asyncpg://test@127.0.0.1/test?database=other",
    "postgresql+asyncpg://test@127.0.0.1/test?port=5433",
    "sqlite:///test",
    "not a database URL",
])
def test_rehearsal_url_rejects_ambiguous_targets_without_echoing_input(raw):
    with pytest.raises(ValueError) as rejected:
        _local_rehearsal_url(raw)
    assert str(rejected.value) == "c369 requires an asyncpg loopback URL without query overrides"


@dataclass
class RestrictedPurge:
    role: str
    owner: AsyncEngine
    engine: AsyncEngine
    identities: list[str] = field(default_factory=list)

    async def grant(self, sql: str) -> None:
        # sql is test-owned text; role is generated hexadecimal, never operator input.
        async with self.owner.begin() as conn:
            await conn.execute(text(sql.format(role=self.role)))

    async def run(self, monkeypatch, *, apply=False, **limits):
        before = len(self.identities)
        with monkeypatch.context() as patch:
            patch.setattr(app_db, "get_session_factory", lambda: async_sessionmaker(
                self.engine, expire_on_commit=False,
            ))
            report = await run_purge_job(
                apply=apply, now=NOW, retention_days=30, key_retirement_grace_days=7,
                batch_size=limits.get("batch_size", 10),
                max_batches=limits.get("max_batches", 10), max_seconds=30,
            )
        assert len(self.identities) > before, "job never reached restricted SQL"
        return report


@pytest.fixture
async def restricted_purge(client, migrated_db):
    url = _local_rehearsal_url(migrated_db)
    owner = app_db.get_engine()
    role = f"c369_purge_{os.getpid()}_{uuid.uuid4().hex[:12]}"
    created = public_create = public_temp = False
    engine = None
    async with owner.connect() as conn:
        is_superuser, marker = (await conn.execute(text(
            "SELECT r.rolsuper, shobj_description(d.oid, 'pg_database') "
            "FROM pg_roles r CROSS JOIN pg_database d "
            "WHERE r.rolname = current_user AND d.datname = current_database()"
        ))).one()
    assert marker and marker.startswith(RUN_DB_MARKER + " "), (
        "c369 may modify grants only in the fixture-owned pytest database"
    )
    assert is_superuser, (
        "c369 needs a disposable local test administrator with superuser rights "
        "to create/drop its restricted role and restore schema ACLs; use an "
        "isolated PostgreSQL cluster or CI, never elevate the application role"
    )
    try:
        async with owner.begin() as conn:
            await conn.execute(text(
                f"CREATE ROLE {role} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOINHERIT NOREPLICATION NOBYPASSRLS"
            ))
            public_create = bool(await conn.scalar(text(
                "SELECT has_schema_privilege(:role, 'public', 'CREATE')"
            ), {"role": role}))
            # PG14's PUBLIC CREATE would otherwise make the DDL denial false.
            # This is only the marked per-run database; restore its ACL below.
            if public_create:
                await conn.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
            dbname = conn.dialect.identifier_preparer.quote(url.database)
            public_temp = bool(await conn.scalar(text(
                "SELECT has_database_privilege(:role, current_database(), 'TEMPORARY')"
            ), {"role": role}))
            if public_temp:
                await conn.execute(text(f"REVOKE TEMPORARY ON DATABASE {dbname} FROM PUBLIC"))
            await conn.execute(text(f"GRANT CONNECT ON DATABASE {dbname} TO {role}"))
            await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
            await conn.execute(text(
                f"GRANT SELECT, DELETE ON {', '.join(CONTENT + PREKEYS)} TO {role}"
            ))
            await conn.execute(text(f"GRANT SELECT (id, revoked_at) ON devices TO {role}"))
            for table, columns in UPDATES.items():
                await conn.execute(text(
                    f"GRANT UPDATE ({', '.join(sorted(columns))}) ON {table} TO {role}"
                ))
        created = True
        # Startup role survives commits/rollbacks without borrowing the owner's
        # engine or interfering with preview's first SET TRANSACTION statement.
        engine = create_async_engine(migrated_db, pool_size=1, max_overflow=0,
                                     connect_args={"server_settings": {"role": role}})
        harness = RestrictedPurge(role, owner, engine)

        @event.listens_for(engine.sync_engine, "after_cursor_execute")
        def check_every_job_transaction(conn, cursor, statement, parameters, context, many):
            if statement.startswith("SET LOCAL statement_timeout"):
                identity = conn.exec_driver_sql(
                    "SELECT current_user, session_user, rolsuper, rolcreaterole, "
                    "rolcreatedb, rolbypassrls FROM pg_roles WHERE rolname=current_user"
                ).one()
                assert identity[0] == role and identity[1] != role
                assert tuple(identity[2:]) == (False, False, False, False)
                harness.identities.append(identity[0])

        yield harness
    finally:
        # Return all restricted connections before dropping ONLY our unique role.
        if engine is not None:
            await engine.dispose()
        if created:
            async with owner.begin() as conn:
                await conn.execute(text(f"DROP OWNED BY {role}"))
                await conn.execute(text(f"DROP ROLE {role}"))
                if public_create:
                    await conn.execute(text("GRANT CREATE ON SCHEMA public TO PUBLIC"))
                if public_temp:
                    await conn.execute(text(f"GRANT TEMPORARY ON DATABASE {dbname} TO PUBLIC"))


@pytest.fixture
async def scene(make_chapter_with):
    setup = await make_chapter_with("president")
    author = setup.president.id
    post = await _insert_post(setup.chapter_id, author, deleted_at=OLD_CONTENT)
    comment = await _insert_comment(post, author, deleted_at=None)
    await _insert_post_like(post, author)
    live_post = await _insert_post(setup.chapter_id, author, deleted_at=None)
    old_comment = await _insert_comment(live_post, author, deleted_at=OLD_CONTENT)
    fresh_comment = await _insert_comment(live_post, author, deleted_at=NOW)
    async with app_db.get_session_factory()() as session:
        campus = str(await session.scalar(text(
            "SELECT campus_id FROM chapters WHERE id=:id"
        ), {"id": setup.chapter_id}))
    chirp = await _insert_chirp(campus, author, removed_at=OLD_CONTENT)
    await _insert_chirp_vote(chirp, author)
    boundary_chirp = await _insert_chirp(campus, author, removed_at=NOW-timedelta(days=30))
    active = await _device(author)
    revoked = await _device(author, revoked_at=OLD_KEY)
    deleted_keys = [
        ("one_time_prekeys", await _otk(active, consumed_at=OLD_KEY)),
        ("kyber_prekeys", await _kyber(active, consumed_at=OLD_KEY)),
        ("signed_prekeys", await _signed(active, created_at=OLD_KEY-timedelta(days=1))),
        ("one_time_prekeys", await _otk(revoked, consumed_at=None)),
        ("kyber_prekeys", await _kyber(revoked, consumed_at=None, is_last_resort=True)),
        ("signed_prekeys", await _signed(revoked, created_at=NOW)),
    ]
    survivors = [
        ("posts", live_post), ("post_comments", fresh_comment), ("chirps", boundary_chirp),
        ("devices", active), ("devices", revoked),
        ("signed_prekeys", await _signed(active, created_at=OLD_KEY, key_id=2)),
        ("one_time_prekeys", await _otk(active, consumed_at=None, key_id=2)),
        ("one_time_prekeys", await _otk(active, consumed_at=NOW-timedelta(days=7), key_id=3)),
        ("kyber_prekeys", await _kyber(active, consumed_at=OLD_KEY, is_last_resort=True, key_id=2)),
    ]
    return dict(deleted=[("posts", post), ("post_comments", comment),
                         ("post_comments", old_comment), ("chirps", chirp)],
                keys=deleted_keys, survivors=survivors, chirp=chirp, campus=campus,
                author=author)


async def _assert_rows(rows, *, present):
    for table, row_id in rows:
        assert await _row_exists(table, row_id) is present, table


async def test_actual_restricted_job_previews_applies_and_repeats(
    scene, restricted_purge, monkeypatch,
):
    preview = await restricted_purge.run(monkeypatch)
    assert preview["status"] == "preview" and preview["batches_committed"] == 0
    assert preview["counts"] == COUNTS
    assert preview["key_retirement_counts"] == KEY_COUNTS
    await _assert_rows(scene["deleted"] + scene["keys"] + scene["survivors"], present=True)

    applied = await restricted_purge.run(monkeypatch, apply=True)
    assert applied["status"] == "complete" and applied["batches_committed"] == 1
    assert applied["counts"] == COUNTS and applied["physical_rows"] == 6
    assert applied["key_retirement_counts"] == KEY_COUNTS
    await _assert_rows(scene["deleted"] + scene["keys"], present=False)
    await _assert_rows(scene["survivors"], present=True)

    repeated = await restricted_purge.run(monkeypatch, apply=True)
    assert repeated["status"] == "complete" and repeated["remaining"] is False
    assert repeated["physical_rows"] == 0
    assert repeated["key_retirement_counts"] == dict.fromkeys(KEY_COUNTS, 0)
    await _assert_rows(scene["survivors"], present=True)


async def test_vote_delete_runs_real_score_trigger_between_bounded_batches(
    scene, make_user, restricted_purge, monkeypatch,
):
    voter = await make_user()
    await _insert_chirp_vote(scene["chirp"], voter.id)
    async with app_db.get_session_factory()() as session:
        assert await session.scalar(text("SELECT score FROM chirps WHERE id=:id"),
                                    {"id": scene["chirp"]}) == 2
    first = await restricted_purge.run(monkeypatch, apply=True, batch_size=1, max_batches=1)
    assert first["status"] == "incomplete"
    assert first["counts"]["chirp_votes"] == 1 and first["counts"]["chirps"] == 0
    async with app_db.get_session_factory()() as session:
        assert await session.scalar(text("SELECT score FROM chirps WHERE id=:id"),
                                    {"id": scene["chirp"]}) == 1
    resumed = await restricted_purge.run(monkeypatch, apply=True, batch_size=1)
    assert resumed["status"] == "complete"
    assert resumed["counts"]["chirp_votes"] == 1 and resumed["counts"]["chirps"] == 1
    assert not await _row_exists("chirps", scene["chirp"])


@pytest.mark.parametrize("table,column", [("chirps", "score"), *[(t, "id") for t in PREKEYS]])
async def test_missing_trigger_or_prekey_lock_grant_fails_real_apply_and_can_resume(
    scene, restricted_purge, monkeypatch, table, column,
):
    await restricted_purge.grant(f"REVOKE UPDATE ({column}) ON {table} FROM {{role}}")
    preview = await restricted_purge.run(monkeypatch)
    assert preview["status"] == "preview"
    assert preview["counts"] == COUNTS and preview["key_retirement_counts"] == KEY_COUNTS
    failed = await restricted_purge.run(monkeypatch, apply=True)
    assert failed["status"] == "failed" and failed["commit_outcome_unknown"] is False
    assert failed["key_retirement_counts"] == dict.fromkeys(KEY_COUNTS, 0)
    await _assert_rows(scene["keys"], present=True)
    # Score failure rolls back content too. A key failure preserves the already
    # acknowledged content phase, but rolls back all key deletes in that batch.
    assert failed["physical_rows"] == (0 if table == "chirps" else 6)
    await _assert_rows(scene["deleted"], present=table == "chirps")
    await restricted_purge.grant(f"GRANT UPDATE ({column}) ON {table} TO {{role}}")
    resumed = await restricted_purge.run(monkeypatch, apply=True)
    assert resumed["status"] == "complete" and resumed["key_retirement_counts"] == KEY_COUNTS
    await _assert_rows(scene["deleted"] + scene["keys"], present=False)
    await _assert_rows(scene["survivors"], present=True)


async def test_role_denies_unrelated_access_and_every_ungranted_update_column(restricted_purge):
    harness = restricted_purge
    async with harness.owner.connect() as conn:
        columns = (await conn.execute(text(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema='public' ORDER BY table_name, ordinal_position"
        ))).all()
    assert {"users", "messages", "ledger_entries", "dues_payment_intents", "content_reports"} <= {
        table for table, _ in columns
    }
    async with harness.engine.connect() as conn:
        assert await conn.scalar(text("SELECT current_user")) == harness.role
        for table in sorted({table for table, _ in columns}):
            for privilege in ("SELECT", "INSERT", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
                observed = await conn.scalar(text(
                    "SELECT has_table_privilege(current_user, :table, :privilege)"
                ), {"table": table, "privilege": privilege})
                expected = table in CONTENT + PREKEYS and privilege in {"SELECT", "DELETE"}
                assert observed is expected, (table, privilege)
        for table, column in columns:
            observed = await conn.scalar(text(
                "SELECT has_column_privilege(current_user, :table, :column, 'UPDATE')"
            ), {"table": table, "column": column})
            assert observed is (column in UPDATES.get(table, set())), (table, column)
        await conn.rollback()
        unrelated = {table for table, _ in columns} - set(CONTENT + PREKEYS + ("devices",))
        statements = []
        for table in sorted(unrelated):
            # PostgreSQL checks permissions even with no matching rows. A wrongly
            # granted write therefore fails this test without modifying user data.
            first_column = next(column for name, column in columns if name == table)
            statements += [f"SELECT * FROM {table} LIMIT 0", f"DELETE FROM {table} WHERE false",
                           f"UPDATE {table} SET {first_column}={first_column} WHERE false",
                           f"INSERT INTO {table} DEFAULT VALUES"]
        statements += [
            "UPDATE posts SET body=body WHERE false",
            "UPDATE post_comments SET body=body WHERE false",
            "UPDATE chirps SET body=body WHERE false",
            "UPDATE messages SET ciphertext=ciphertext WHERE false",
            "SELECT identity_key FROM devices LIMIT 0",
            "UPDATE devices SET revoked_at=revoked_at WHERE false",
            "DELETE FROM devices WHERE false",
            "INSERT INTO devices DEFAULT VALUES",
            "CREATE TABLE public.c369_forbidden (id integer)",
            "CREATE TEMPORARY TABLE c369_forbidden_temp (id integer)",
        ]
        statements += [f"UPDATE {table} SET public_key=public_key WHERE false" for table in PREKEYS]
        statements += [f"INSERT INTO {table} SELECT * FROM {table} WHERE false" for table in CONTENT + PREKEYS]
        for statement in statements:
            with pytest.raises(DBAPIError) as denied:
                await conn.execute(text(statement))
            assert denied.value.orig.sqlstate == "42501", statement
            await conn.rollback()
