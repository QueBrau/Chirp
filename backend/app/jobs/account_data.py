"""Bounded maintenance CLI for immutable account-export artifacts.

The CLI previews by default. No scheduler is enabled by this change; callers
must explicitly pass ``--delete`` to mutate artifacts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone

from sqlalchemy import delete, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.services import account_data

MAX_ARTIFACTS_PER_RUN = 1_000
MAX_TIMEOUT_SECONDS = 300

REQUEST_OPERATION_MODELS: tuple[tuple[type, tuple[str, ...]], ...] = (
    (models.User, ("SELECT",)),
    (models.AccountDataRequest, ("SELECT", "INSERT", "UPDATE")),
    (models.AccountDataArtifact, ("SELECT", "INSERT")),
)


def _model_columns(model: type) -> tuple[str, ...]:
    return tuple(column.name for column in model.__table__.columns)


async def run_export_privilege_preflight(
    session: AsyncSession, *, expected_role: str | None,
) -> dict[str, object]:
    """Check the current database identity against the export's real ORM reads.

    This is read-only.  Column checks intentionally allow least-privilege grants
    such as ``SELECT`` on every exported column without requiring table-wide
    ``SELECT``.  Each relation with sufficient column grants also receives a real
    ``SELECT ... LIMIT 0`` through the current session, catching schema/query drift.
    """
    if session.in_transaction():
        raise RuntimeError("preflight_requires_fresh_transaction")
    await session.execute(text("SET TRANSACTION READ ONLY"))
    current_user = str(await session.scalar(text("SELECT current_user")))
    export_models = account_data.export_relation_models()
    operation_models = tuple(model for model, _privileges in REQUEST_OPERATION_MODELS)
    models_to_check: list[type] = []
    for model in (*export_models, *operation_models):
        if model not in models_to_check:
            models_to_check.append(model)

    missing_columns: list[dict[str, str]] = []
    query_failures: list[str] = []
    checked_relations: list[str] = []
    for model in models_to_check:
        table = f"public.{model.__tablename__}"
        checked_relations.append(model.__tablename__)
        for column in _model_columns(model):
            allowed = await session.scalar(text(
                "SELECT has_column_privilege(current_user, :table, :column, 'SELECT')"
            ), {"table": table, "column": column})
            if not allowed:
                missing_columns.append({"table": model.__tablename__, "column": column})
        if not any(item["table"] == model.__tablename__ for item in missing_columns):
            try:
                async with session.begin_nested():
                    await session.execute(select(model).limit(0))
            except SQLAlchemyError:
                query_failures.append(model.__tablename__)

    missing_operations: list[dict[str, str]] = []
    for model, privileges in REQUEST_OPERATION_MODELS:
        for privilege in privileges:
            if privilege == "SELECT":
                continue
            allowed = await session.scalar(text(
                "SELECT has_table_privilege(current_user, :table, :privilege)"
            ), {"table": f"public.{model.__tablename__}", "privilege": privilege})
            if not allowed:
                missing_operations.append({"table": model.__tablename__, "privilege": privilege})

    return {
        "status": "ok" if (
            current_user == expected_role
            and not missing_columns
            and not query_failures
            and not missing_operations
        ) else "failed",
        "current_user": current_user,
        "expected_role": expected_role,
        "identity_match": current_user == expected_role,
        "checked_relations": checked_relations,
        "missing_columns": missing_columns,
        "query_failures": query_failures,
        "missing_operations": missing_operations,
    }


def _positive_int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


async def run_export_artifact_expiry(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    batch_size: int = 100,
    max_batches: int = 10,
    apply: bool = False,
) -> dict[str, object]:
    """Preview or delete expired artifacts within an explicit bounded budget.

    Dry-run is the default. ``apply=True`` is the only mutation switch and the
    total deletion budget is capped at ``MAX_ARTIFACTS_PER_RUN``. The caller owns
    the transaction and must commit an applied run.
    """
    if not isinstance(apply, bool):
        raise TypeError("apply must be a bool")
    _positive_int(batch_size, "batch_size")
    _positive_int(max_batches, "max_batches")
    budget = min(batch_size * max_batches, MAX_ARTIFACTS_PER_RUN)
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("now must include a timezone")
    cutoff = instant.astimezone(timezone.utc)

    ids = list((await session.scalars(
        select(models.AccountDataArtifact.id)
        .where(
            models.AccountDataArtifact.expires_at.is_not(None),
            models.AccountDataArtifact.expires_at <= cutoff,
        )
        .order_by(models.AccountDataArtifact.expires_at, models.AccountDataArtifact.id)
        .limit(budget)
    )).all())
    deleted = 0
    if apply and ids:
        result = await session.execute(
            delete(models.AccountDataArtifact)
            .where(models.AccountDataArtifact.id.in_(ids))
            .where(
                models.AccountDataArtifact.expires_at.is_not(None),
                models.AccountDataArtifact.expires_at <= cutoff,
            )
            .returning(models.AccountDataArtifact.id)
        )
        deleted = len(result.scalars().all())
    return {
        "mode": "apply" if apply else "dry_run",
        "cutoff": cutoff.isoformat(),
        "eligible": len(ids),
        "deleted": deleted,
        "capped": len(ids) >= budget,
        "budget": budget,
    }


def _positive_arg(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    try:
        return _positive_int(parsed, "value")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


async def _run_cli(*, delete_rows: bool, max_rows: int, timeout_seconds: int,
                   preflight: bool = False, expected_role: str | None = None) -> dict[str, object]:
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        async with asyncio.timeout(timeout_seconds):
            if preflight:
                report = await run_export_privilege_preflight(session, expected_role=expected_role)
                await session.rollback()
                return report
            report = await run_export_artifact_expiry(
                session,
                batch_size=max_rows,
                max_batches=1,
                apply=delete_rows,
            )
            if delete_rows:
                await session.commit()
            else:
                await session.rollback()
            return report


def main(argv: list[str] | None = None) -> None:
    """Preview expired export artifacts; use --delete for bounded mutation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delete", action="store_true", help="delete eligible artifacts")
    parser.add_argument(
        "--preflight-export-privileges", action="store_true",
        help="read-only check of current identity, export columns, and request operations",
    )
    parser.add_argument(
        "--expected-role",
        help="database role expected by --preflight-export-privileges",
    )
    parser.add_argument("--max-rows", type=_positive_arg, default=100,
                        help=f"maximum artifacts (cap {MAX_ARTIFACTS_PER_RUN})")
    parser.add_argument("--timeout-seconds", type=_positive_arg, default=30,
                        help=f"hard timeout (cap {MAX_TIMEOUT_SECONDS}s)")
    args = parser.parse_args(argv)
    if args.max_rows > MAX_ARTIFACTS_PER_RUN:
        parser.error(f"max-rows cannot exceed {MAX_ARTIFACTS_PER_RUN}")
    if args.timeout_seconds > MAX_TIMEOUT_SECONDS:
        parser.error(f"timeout-seconds cannot exceed {MAX_TIMEOUT_SECONDS}")
    if args.delete and args.preflight_export_privileges:
        parser.error("--preflight-export-privileges cannot be combined with --delete")
    if args.preflight_export_privileges and not args.expected_role:
        parser.error("--expected-role is required with --preflight-export-privileges")
    try:
        cli_kwargs: dict[str, object] = {
            "delete_rows": args.delete,
            "max_rows": args.max_rows,
            "timeout_seconds": args.timeout_seconds,
        }
        if args.preflight_export_privileges:
            cli_kwargs.update(preflight=True, expected_role=args.expected_role)
        report = asyncio.run(_run_cli(**cli_kwargs))
    except TimeoutError:
        report = {"mode": "apply" if args.delete else "dry_run", "status": "timed_out"}
        print(json.dumps(report, sort_keys=True))
        raise SystemExit(2)
    except Exception as exc:
        # Never serialize database URLs, SQL, provider responses, or exception text.
        report = {
            "mode": "apply" if args.delete else "dry_run",
            "status": "failed",
            "error_type": type(exc).__name__,
        }
        print(json.dumps(report, sort_keys=True))
        raise SystemExit(2)
    print(json.dumps(report, sort_keys=True))
    if args.preflight_export_privileges and report.get("status") != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
