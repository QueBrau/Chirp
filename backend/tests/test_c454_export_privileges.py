"""Real PostgreSQL coverage for the account-export privilege preflight."""

from __future__ import annotations

from datetime import datetime, timezone
import os
import uuid

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.sql.selectable import Join

from app import models
from app.db import get_engine, get_session_factory
from app.jobs.account_data import (
    REQUEST_OPERATION_MODELS,
    main as account_data_main,
    run_export_privilege_preflight,
)
from app.services import account_data
from tests.test_c369_purge_privileges import _local_rehearsal_url


def _all_models() -> tuple[type, ...]:
    result: list[type] = []
    for model in (*account_data.export_relation_models(), *(model for model, _ in REQUEST_OPERATION_MODELS)):
        if model not in result:
            result.append(model)
    return tuple(result)


def _from_tables(from_clause: object) -> set[str]:
    if isinstance(from_clause, Join):
        return _from_tables(from_clause.left) | _from_tables(from_clause.right)
    name = getattr(from_clause, "name", None)
    return {name} if isinstance(name, str) else set()


@pytest.fixture
async def restricted_export_engine(migrated_db: str) -> AsyncEngine:
    _local_rehearsal_url(migrated_db)
    owner = get_engine()
    role = f"c454_export_{os.getpid()}_{uuid.uuid4().hex[:10]}"
    engine: AsyncEngine | None = None
    created = False
    async with owner.connect() as connection:
        is_superuser, marker = (await connection.execute(text(
            "SELECT r.rolsuper, shobj_description(d.oid, 'pg_database') "
            "FROM pg_roles r CROSS JOIN pg_database d "
            "WHERE r.rolname = current_user AND d.datname = current_database()"
        ))).one()
    assert is_superuser and marker and marker.startswith("chirp-pytest-run ")
    try:
        async with owner.begin() as connection:
            await connection.execute(text(
                f"CREATE ROLE {role} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOINHERIT NOREPLICATION NOBYPASSRLS"
            ))
            dbname = connection.dialect.identifier_preparer.quote(connection.engine.url.database)
            await connection.execute(text(f"GRANT CONNECT ON DATABASE {dbname} TO {role}"))
            await connection.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
            for model in _all_models():
                table = model.__tablename__
                columns = [column.name for column in model.__table__.columns]
                if table == "chapter_stripe_customers":
                    columns.remove("created_at")
                quoted_columns = ", ".join(columns)
                await connection.execute(text(
                    f"GRANT SELECT ({quoted_columns}) ON TABLE public.{table} TO {role}"
                ))
            for model, privileges in REQUEST_OPERATION_MODELS:
                for privilege in privileges[1:]:
                    await connection.execute(text(
                        f"GRANT {privilege} ON TABLE public.{model.__tablename__} TO {role}"
                    ))
        created = True
        engine = create_async_engine(
            migrated_db,
            pool_size=1,
            max_overflow=0,
            connect_args={"server_settings": {"role": role}},
        )
        yield engine
    finally:
        if engine is not None:
            await engine.dispose()
        if created:
            async with owner.begin() as connection:
                await connection.execute(text(f"DROP OWNED BY {role}"))
                await connection.execute(text(f"DROP ROLE {role}"))


@pytest.mark.asyncio
async def test_preflight_uses_real_column_reads_and_catches_missing_created_at(
    restricted_export_engine: AsyncEngine,
) -> None:
    async with restricted_export_engine.connect() as connection:
        role = await connection.scalar(text("SELECT current_user"))
    async with AsyncSession(restricted_export_engine) as session:
        report = await run_export_privilege_preflight(session, expected_role=role)
        assert await session.scalar(text("SHOW transaction_read_only")) == "on"
    assert report["status"] == "failed"
    assert {tuple(item.values()) for item in report["missing_columns"]} == {
        ("chapter_stripe_customers", "created_at"),
    }
    assert report["query_failures"] == []
    owner = get_engine()
    async with owner.begin() as connection:
        await connection.execute(text(
            "GRANT SELECT (created_at) ON TABLE public.chapter_stripe_customers "
            f"TO {role}"
        ))
    async with AsyncSession(restricted_export_engine) as session:
        report = await run_export_privilege_preflight(session, expected_role=role)
    assert report["status"] == "ok"
    async with owner.connect() as connection:
        assert not await connection.scalar(text(
            "SELECT has_table_privilege(:role, 'public.chapter_stripe_customers', 'SELECT')"
        ), {"role": role})
    async with AsyncSession(restricted_export_engine) as session:
        wrong_identity = await run_export_privilege_preflight(
            session, expected_role="not-the-runtime-role"
        )
        assert await session.scalar(text("SHOW transaction_read_only")) == "on"
    assert wrong_identity["status"] == "failed"
    assert wrong_identity["identity_match"] is False


@pytest.mark.asyncio
async def test_real_export_sql_surface_matches_preflight_and_detects_missing_model(
    make_user, migrated_db: str, monkeypatch,
) -> None:
    owner = await make_user("Export privilege owner")
    now = datetime.now(timezone.utc)
    async with get_session_factory()() as session:
        policy = models.LegalPolicy(
            policy_key="terms", version=f"test-{owner.id}", effective_at=now, is_current=False,
        )
        session.add(policy)
        await session.flush()
        session.add(models.LegalAcceptance(
            user_id=owner.id, policy_id=policy.id, age_declaration=18,
        ))
        session.add(models.Device(user_id=owner.id, registration_id=7, identity_key=b"d" * 32))
        await session.commit()
        user = await session.get(models.User, owner.id)
        assert user is not None

        observed: set[str] = set()

        def capture(_conn, clauseelement, _multiparams, _params, _execution_options):
            if not getattr(clauseelement, "is_select", False):
                return
            for from_clause in clauseelement.get_final_froms():
                observed.update(_from_tables(from_clause))

        sync_engine = get_engine().sync_engine
        event.listen(sync_engine, "before_execute", capture)
        try:
            await account_data.build_export(session, user)
        finally:
            event.remove(sync_engine, "before_execute", capture)

    expected = {model.__tablename__ for model in account_data.export_relation_models()}
    assert observed == expected

    # Simulate a newly-added direct export query omitted from the preflight
    # surface. The real SQL capture identifies the missing relation rather than
    # repeating the expected list in a second test fixture.
    monkeypatch.setattr(
        account_data,
        "EXPORT_ADDITIONAL_MODELS",
        tuple(model for model in account_data.EXPORT_ADDITIONAL_MODELS if model is not models.MessageReceipt),
    )
    assert "message_receipts" not in {
        model.__tablename__ for model in account_data.export_relation_models()
    }
    assert "message_receipts" in observed


def test_cli_preflight_failure_exits_nonzero(monkeypatch) -> None:
    async def failed(**_kwargs):
        return {"status": "failed", "identity_match": False}

    monkeypatch.setattr("app.jobs.account_data._run_cli", failed)
    with pytest.raises(SystemExit) as raised:
        account_data_main(["--preflight-export-privileges", "--expected-role", "chirp_api"])
    assert raised.value.code == 1
