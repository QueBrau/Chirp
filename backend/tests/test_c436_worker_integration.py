"""Focused tests for the explicit, default-off deferred-cleanup operator job."""
from __future__ import annotations

import uuid
import json

import pytest
from sqlalchemy import text

from app.db import get_session_factory
from app.jobs.account_deferred_cleanup import (
    CleanupBinding,
    CleanupOperatorError,
    build_media_reader,
    inspect_one,
    main,
    run_one_batch,
)
import app.jobs.account_deferred_cleanup as cleanup_job
from app.services.account_post_deletion_cleanup import DeferredCleanupReceipt


@pytest.mark.asyncio
async def test_inspect_fails_closed_before_claim_on_unprepared_schema(monkeypatch) -> None:
    # CI and the local shared database are normally already at 0050.  Simulate
    # an operator compiled against the pre-worker 0049 head without mutating
    # the shared migration state.
    monkeypatch.setattr(cleanup_job, "EXPECTED_SCHEMA", "0049")
    async with get_session_factory()() as session:
        identity = (await session.execute(text(
            "SELECT current_database(), current_user"
        ))).one()
        binding = CleanupBinding(
            uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "temporary_media", "0" * 64,
        )
        with pytest.raises(CleanupOperatorError, match="schema_head_mismatch"):
            await inspect_one(
                session, binding,
                expected_role=str(identity[1]), expected_database=str(identity[0]),
            )


def test_apply_requires_explicit_provider_factory_and_does_not_connect(monkeypatch, capsys) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://invalid.invalid/nope")
    result = main([
        "--obligation-id", str(uuid.uuid4()), "--request-id", str(uuid.uuid4()),
        "--expected-user-id", str(uuid.uuid4()), "--category", "temporary_media",
        "--scope-sha256", "0" * 64, "--expected-role", "chirp_api",
        "--expected-database", "chirp", "--apply-one",
    ])
    assert result == 2
    assert capsys.readouterr().out == '{"status": "blocked", "error": "cleanup_preflight_failed"}\n'


def test_apply_manifest_is_blocked_by_default_before_provider_construction(tmp_path, monkeypatch, capsys) -> None:
    obligation_id, request_id, user_id = (str(uuid.uuid4()) for _ in range(3))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "schema": "0050", "obligation_id": obligation_id,
        "request_id": request_id, "user_id": user_id,
        "category": "temporary_media", "scope_sha256": "0" * 64,
        "media": {"project": "chirps-prod", "bucket": "chirp-media",
                   "use_adc": True, "versioning_enabled": False,
                   "soft_delete_seconds": 0},
    }))
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://invalid.invalid/nope")
    result = cleanup_job.main([
        "--obligation-id", obligation_id, "--request-id", request_id,
        "--expected-user-id", user_id, "--category", "temporary_media",
        "--scope-sha256", "0" * 64, "--expected-role", "chirp_api",
        "--expected-database", "chirp", "--apply-one",
        "--provider-manifest", str(manifest),
    ])
    assert result == 2
    assert capsys.readouterr().out == '{"status": "blocked", "error": "cleanup_preflight_failed"}\n'


def test_media_factory_requires_explicit_project_and_selection() -> None:
    with pytest.raises(CleanupOperatorError, match="media_project_invalid"):
        build_media_reader(bucket="chirp-media", project="wrong", use_adc=True)
    with pytest.raises(CleanupOperatorError, match="media_credentials_selection_invalid"):
        build_media_reader(bucket="chirp-media", project="chirps-prod")


@pytest.mark.asyncio
async def test_positive_pg_dispatch_updates_only_bound_obligation(monkeypatch) -> None:
    """Create a disposable 0050 DB and run the real least-privilege path."""
    import asyncio
    import os
    from tests.test_c436_external_operations_migration import _admin_execute, _alembic, _swap_database

    requested = os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://chirp:chirp@localhost:5432/chirp_test")
    # The worker role must be non-owner, while the disposable role/database
    # cleanup needs a local administrator.  CI may provide its own admin URL;
    # the repository's local PostgreSQL service uses joseperdomo.
    admin_requested = os.environ.get(
        "C436_TEST_ADMIN_URL", "postgresql+asyncpg://joseperdomo@localhost:5432/postgres"
    )
    admin_url = _swap_database(admin_requested, "postgres")
    db_name = f"chirp_c436_worker_{uuid.uuid4().hex[:10]}"
    url = _swap_database(requested, db_name)
    role = f"c436w_{uuid.uuid4().hex[:10]}"
    password = "worker-test-only"
    original_db = os.environ.get("DATABASE_URL")
    original_flag = os.environ.get("ACCOUNT_DELETION_DEFERRED_CLEANUP_ENABLED")
    try:
        await _admin_execute(admin_url, [f'CREATE DATABASE "{db_name}"'])
        await asyncio.to_thread(_alembic, url, "0050")
        await _admin_execute(admin_url, [
            f"CREATE ROLE {role} LOGIN PASSWORD '{password}'",
        ])
        await _admin_execute(url, [
            f"GRANT CONNECT ON DATABASE {db_name} TO {role}",
            f"GRANT USAGE ON SCHEMA public TO {role}",
            f"GRANT SELECT ON TABLE alembic_version, account_post_deletion_cleanup, account_data_requests, users TO {role}",
            f"GRANT UPDATE (state, owner_token, lease_expires_at, attempt_count, updated_at, last_pass_digest, remaining_count, last_error, next_attempt_at, completed_at) ON TABLE account_post_deletion_cleanup TO {role}",
        ])
        import hashlib
        scope = {"bucket": "chirp-media", "tmp_prefix": "", "initial_snapshot_digest": "0" * 64}
        user_id, request_id, obligation_id, foreign_id = (uuid.uuid4() for _ in range(4))
        scope["tmp_prefix"] = f"tmp/{user_id}/"
        scope_digest = hashlib.sha256(json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        foreign_scope = {**scope, "tmp_prefix": f"tmp/{uuid.uuid4()}/"}
        foreign_digest = hashlib.sha256(json.dumps(foreign_scope, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        async def seed():
            from sqlalchemy.ext.asyncio import create_async_engine
            engine = create_async_engine(url)
            async with engine.begin() as conn:
                for uid in (user_id,):
                    await conn.execute(text("INSERT INTO users(id,firebase_uid,email,display_name,account_type,is_ghost) VALUES (:id,:f,:e,'Deleted','greek',true)"), {"id": uid, "f": f"deleted:{uid}", "e": f"deleted+{uid}@invalid.chirp"})
                await conn.execute(text("INSERT INTO users(id,firebase_uid,email,display_name,account_type,is_ghost) VALUES (:id,:f,:e,'Deleted foreign','greek',true)"), {"id": foreign_id, "f": f"deleted:foreign-{foreign_id}", "e": f"foreign-{foreign_id}@invalid.chirp"})
                foreign_request_id = uuid.uuid4()
                for rid, uid, oid, sc, dg in ((request_id, user_id, obligation_id, scope, scope_digest), (foreign_request_id, foreign_id, uuid.uuid4(), foreign_scope, foreign_digest)):
                    await conn.execute(text("INSERT INTO account_data_requests(id,user_id,kind,status,scope,excluded,retention_reasons,provider_steps,completed_at) VALUES (:id,:uid,'deletion','completed',ARRAY[]::text[],ARRAY[]::text[],ARRAY[]::text[],'{}'::jsonb,now())"), {"id": rid, "uid": uid})
                    await conn.execute(text("INSERT INTO account_post_deletion_cleanup(id,request_id,user_id,category,state,scope,scope_sha256,last_pass_digest,next_attempt_at) VALUES (:oid,:rid,:uid,'temporary_media','pending',:scope,:dg,:pass,now())"), {"oid": oid, "rid": rid, "uid": uid, "scope": json.dumps(sc), "dg": dg, "pass": "0" * 64})
            await engine.dispose()
        await seed()
        monkeypatch.setenv("DATABASE_URL", f"postgresql+asyncpg://{role}:{password}@localhost:5432/{db_name}")
        monkeypatch.setenv("ACCOUNT_DELETION_DEFERRED_CLEANUP_ENABLED", "true")
        from app.config import get_settings
        get_settings.cache_clear()
        import app.db as db_module
        # The preceding async inspection test may have initialized the lazy
        # shared engine against the shared database.  Reset the lazy handles
        # after switching DATABASE_URL so this test's session is bound to the
        # disposable database and current event loop.
        db_module._engine = None
        db_module._session_factory = None
        binding = CleanupBinding(obligation_id, request_id, user_id, "temporary_media", scope_digest)
        calls: list[uuid.UUID] = []
        async def provider(claim):
            calls.append(claim.id)
            return DeferredCleanupReceipt(category="temporary_media", provider="media_storage", subject=str(claim.user_id), request_id=str(claim.request_id), scope=scope, scope_sha256=claim.scope_sha256, pass_digest="1" * 64, readback=True, retained_with_reason=(), remaining_count=1)
        counts = await run_one_batch(get_session_factory(), provider, binding, expected_role=role, expected_database=db_name, timeout_seconds=10)
        assert counts == {"claimed": 1, "rescheduled": 1, "completed": 0, "uncertain": 0}
        assert calls == [obligation_id]
        async def read_states():
            from sqlalchemy.ext.asyncio import create_async_engine
            engine = create_async_engine(url)
            try:
                async with engine.connect() as conn:
                    return (await conn.execute(text(
                        "SELECT id, user_id, state, attempt_count FROM account_post_deletion_cleanup "
                        "ORDER BY id"
                    ))).mappings().all()
            finally:
                await engine.dispose()
        states = await read_states()
        target = next(row for row in states if row["id"] == obligation_id)
        foreign = next(row for row in states if row["user_id"] == foreign_id)
        assert target["state"] == "pending" and target["attempt_count"] == 1
        assert foreign["state"] == "pending" and foreign["attempt_count"] == 0
    finally:
        if original_db is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original_db
        if original_flag is None:
            os.environ.pop("ACCOUNT_DELETION_DEFERRED_CLEANUP_ENABLED", None)
        else:
            os.environ["ACCOUNT_DELETION_DEFERRED_CLEANUP_ENABLED"] = original_flag
        from app.config import get_settings
        get_settings.cache_clear()
        import app.db as db_module
        if db_module._engine is not None:
            await db_module._engine.dispose()
        db_module._engine = None
        db_module._session_factory = None
        await _admin_execute(admin_url, [
            f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)',
            (
                "DO $$ BEGIN "
                f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "
                f"EXECUTE 'DROP OWNED BY ' || quote_ident('{role}'); END IF; "
                "END $$"
            ),
            f"DROP ROLE IF EXISTS {role}",
        ])
