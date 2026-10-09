"""Real PostgreSQL coverage for the account-export privilege preflight."""

from __future__ import annotations

import ast
import inspect
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app import models
from app.db import get_engine
from app.jobs.account_data import (
    REQUEST_OPERATION_MODELS,
    run_export_privilege_preflight,
)
from app.services import account_data


def _all_models() -> tuple[type, ...]:
    result: list[type] = []
    for model in (*account_data.export_relation_models(), *(model for model, _ in REQUEST_OPERATION_MODELS)):
        if model not in result:
            result.append(model)
    return tuple(result)


@pytest.fixture
async def restricted_export_engine(migrated_db: str) -> AsyncEngine:
    owner = get_engine()
    role = f"c454_export_{os.getpid()}_{uuid.uuid4().hex[:10]}"
    engine: AsyncEngine | None = None
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


def test_preflight_relation_surface_is_shared_with_export_queries(monkeypatch) -> None:
    original = account_data.EXPORT_QUERY_SPECS
    monkeypatch.setattr(
        account_data,
        "EXPORT_QUERY_SPECS",
        (*original, (models.Campus, "id", "drift_probe")),
    )
    assert models.Campus in account_data.export_relation_models()
    assert models.Campus.__tablename__ == "campuses"


def test_direct_export_selects_cannot_bypass_preflight_surface() -> None:
    tree = ast.parse(inspect.getsource(account_data.build_export))
    direct_models: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        if isinstance(node.func, ast.Name) and node.func.id in {"select", "_rows", "_rows_for_ids"}:
            argument = node.args[0]
            if isinstance(argument, ast.Attribute) and isinstance(argument.value, ast.Name):
                if argument.value.id == "models":
                    direct_models.add(argument.attr)
    checked_models = {model.__name__ for model in account_data.export_relation_models()}
    assert direct_models <= checked_models
