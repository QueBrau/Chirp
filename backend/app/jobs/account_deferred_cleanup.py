"""Explicit operator entry point for one deferred account-cleanup pass.

Inspection is the default and is read-only.  Provider I/O is reachable only
through :func:`run_one_batch` with an explicitly constructed provider object;
this module never discovers credentials or registers a scheduler.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
from pathlib import Path
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app import models
from app.services.account_post_deletion_cleanup import (
    CATEGORIES,
    CleanupCompletionProof,
    DeferredCleanupReceipt,
    _digest,
    _valid_scope,
    reconcile_cleanup_obligation,
    run_post_deletion_cleanup_batch,
)
from app.services.account_provider_gcs import GCSMediaReader

EXPECTED_SCHEMA = "0050"
EXPECTED_GCS_PROJECT = "chirps-prod"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_TIMEOUT_SECONDS = 120.0


class CleanupOperatorError(ValueError):
    """Sanitized operator-facing validation failure."""


def build_media_reader(*, bucket: str, project: str, credentials_file: str | None = None,
                       use_adc: bool = False) -> GCSMediaReader:
    """Build a bound GCS reader only during explicit apply preparation.

    Credentials are selected by the operator: either an existing private file or
    ADC.  No credential lookup occurs on import or in inspect mode.
    """
    if project != EXPECTED_GCS_PROJECT:
        raise CleanupOperatorError("media_project_invalid")
    if bool(credentials_file) == bool(use_adc):
        raise CleanupOperatorError("media_credentials_selection_invalid")
    credentials = None
    if credentials_file:
        from google.auth import load_credentials_from_file
        credentials, credential_project = load_credentials_from_file(credentials_file)
        if credential_project not in (None, project):
            raise CleanupOperatorError("media_credentials_project_mismatch")
    else:
        import google.auth
        credentials, credential_project = google.auth.default(quota_project_id=project)
        if credential_project not in (None, project):
            raise CleanupOperatorError("media_credentials_project_mismatch")
    return GCSMediaReader(bucket, project=project, credentials=credentials)


@dataclass(frozen=True)
class CleanupBinding:
    obligation_id: uuid.UUID
    request_id: uuid.UUID
    user_id: uuid.UUID
    category: str
    scope_sha256: str


def _uuid(value: str, label: str) -> uuid.UUID:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise CleanupOperatorError(f"{label}_invalid") from None
    if str(parsed) != value.lower():
        raise CleanupOperatorError(f"{label}_invalid")
    return parsed


def _safe_scope(row: models.AccountPostDeletionCleanup, binding: CleanupBinding) -> None:
    if (row.category not in CATEGORIES or row.scope_sha256 != binding.scope_sha256
            or _SHA256.fullmatch(binding.scope_sha256) is None
            or _digest(row.scope) != row.scope_sha256
            or not _valid_scope(row.category, row.user_id, row.scope)):
        raise CleanupOperatorError("cleanup_scope_invalid")


def _load_proof(path: str, binding: CleanupBinding) -> CleanupCompletionProof:
    """Load only the bounded, typed reconciliation fields from a private file."""
    try:
        raw = Path(path).read_bytes()
        if len(raw) > 64 * 1024:
            raise CleanupOperatorError("cleanup_proof_too_large")
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise CleanupOperatorError("cleanup_proof_invalid") from None
    if type(value) is not dict or set(value) != {
        "category", "request_id", "subject", "scope_sha256", "pass_digest",
        "attempt_token", "proof_id", "verified", "source_drained", "readback",
    }:
        raise CleanupOperatorError("cleanup_proof_invalid")
    try:
        proof = CleanupCompletionProof(**value)
    except (TypeError, ValueError):
        raise CleanupOperatorError("cleanup_proof_invalid") from None
    if proof.request_id != str(binding.request_id) or proof.subject != str(binding.user_id):
        raise CleanupOperatorError("cleanup_proof_binding_invalid")
    return proof


async def _preflight(session: AsyncSession, *, expected_role: str,
                     expected_database: str) -> dict[str, Any]:
    """Verify the exact schema identity and minimum worker privileges."""
    identity = (await session.execute(text("SELECT current_database(), current_user"))).one()
    actual_database, actual_role = str(identity[0]), str(identity[1])
    if actual_database != expected_database or actual_role != expected_role:
        raise CleanupOperatorError("database_identity_mismatch")
    versions = (await session.scalars(text(
        "SELECT version_num FROM alembic_version ORDER BY version_num"
    ))).all()
    if versions != [EXPECTED_SCHEMA]:
        raise CleanupOperatorError("schema_head_mismatch")
    required = {
        "account_post_deletion_cleanup": ("SELECT", "UPDATE"),
        "account_data_requests": ("SELECT",),
        "users": ("SELECT",),
    }
    missing: list[str] = []
    for table, privileges in required.items():
        for privilege in privileges:
            allowed = await session.scalar(text(
                "SELECT has_table_privilege(current_user, :table, :privilege)"
            ), {"table": f"public.{table}", "privilege": privilege})
            if allowed is not True:
                missing.append(f"{table}:{privilege}")
    if missing:
        raise CleanupOperatorError("cleanup_privileges_missing")
    return {"database": actual_database, "role": actual_role, "schema": EXPECTED_SCHEMA}


async def inspect_one(session: AsyncSession, binding: CleanupBinding,
                      *, expected_role: str, expected_database: str) -> dict[str, Any]:
    identity = await _preflight(session, expected_role=expected_role, expected_database=expected_database)
    row = (await session.scalar(select(models.AccountPostDeletionCleanup).where(
        models.AccountPostDeletionCleanup.id == binding.obligation_id,
        models.AccountPostDeletionCleanup.request_id == binding.request_id,
        models.AccountPostDeletionCleanup.user_id == binding.user_id,
        models.AccountPostDeletionCleanup.category == binding.category,
        models.AccountPostDeletionCleanup.scope_sha256 == binding.scope_sha256,
    )))
    if row is None:
        raise CleanupOperatorError("cleanup_obligation_not_found")
    _safe_scope(row, binding)
    return {
        **identity,
        "obligation_id": str(row.id),
        "request_id": str(row.request_id),
        "user_id": str(row.user_id),
        "category": row.category,
        "state": row.state,
        "attempt_count": row.attempt_count,
        "remaining_count": row.remaining_count,
        "scope_sha256": row.scope_sha256,
        "owner_present": row.owner_token is not None,
        "lease_present": row.lease_expires_at is not None,
    }


async def run_one_batch(session_factory: Callable[[], Any], provider: Callable[..., Awaitable[Any]],
                        binding: CleanupBinding,
                        *, expected_role: str, expected_database: str,
                        limit: int = 1, timeout_seconds: float = 120.0) -> dict[str, int]:
    """Run at most one explicit provider pass after a fresh read-only preflight."""
    if limit != 1 or type(limit) is not int:
        raise CleanupOperatorError("cleanup_limit_must_be_one")
    if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise CleanupOperatorError("cleanup_timeout_invalid")
    async with session_factory() as session:
        await _preflight(session, expected_role=expected_role, expected_database=expected_database)
    return await run_post_deletion_cleanup_batch(
        session_factory, provider, limit=1, timeout_seconds=float(timeout_seconds),
        obligation_id=binding.obligation_id, request_id=binding.request_id,
        user_id=binding.user_id, scope_sha256=binding.scope_sha256,
    )


async def reconcile_one(session: AsyncSession, binding: CleanupBinding,
                        proof: CleanupCompletionProof, *, expected_role: str,
                        expected_database: str) -> bool:
    await _preflight(session, expected_role=expected_role, expected_database=expected_database)
    if proof.category != binding.category or proof.request_id != str(binding.request_id) or proof.subject != str(binding.user_id):
        raise CleanupOperatorError("cleanup_proof_binding_invalid")
    row = await session.scalar(select(models.AccountPostDeletionCleanup).where(
        models.AccountPostDeletionCleanup.id == binding.obligation_id,
        models.AccountPostDeletionCleanup.request_id == binding.request_id,
        models.AccountPostDeletionCleanup.user_id == binding.user_id,
        models.AccountPostDeletionCleanup.scope_sha256 == binding.scope_sha256,
    ))
    if row is None:
        raise CleanupOperatorError("cleanup_obligation_not_found")
    _safe_scope(row, binding)
    return await reconcile_cleanup_obligation(session, binding.obligation_id, proof)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--obligation-id", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--expected-user-id", required=True)
    parser.add_argument("--category", choices=sorted(CATEGORIES), required=True)
    parser.add_argument("--scope-sha256", required=True)
    parser.add_argument("--expected-role", required=True)
    parser.add_argument("--expected-database", required=True)
    parser.add_argument("--apply-one", action="store_true")
    parser.add_argument("--reconcile-proof")
    args = parser.parse_args(argv)
    try:
        binding = CleanupBinding(
            _uuid(args.obligation_id, "obligation_id"), _uuid(args.request_id, "request_id"),
            _uuid(args.expected_user_id, "expected_user_id"), args.category, args.scope_sha256,
        )
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise CleanupOperatorError("DATABASE_URL_required")
        if args.apply_one and args.reconcile_proof:
            raise CleanupOperatorError("cleanup_modes_conflict")
        if args.apply_one:
            raise CleanupOperatorError("provider_factory_required_for_apply")
        proof = _load_proof(args.reconcile_proof, binding) if args.reconcile_proof else None
        async def run() -> dict[str, Any]:
            url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
            engine = create_async_engine(url, connect_args={"timeout": 10})
            try:
                if proof is not None:
                    async with engine.connect() as connection:
                        tx = await connection.begin()
                        try:
                            await connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                            session = AsyncSession(bind=connection, autoflush=False, expire_on_commit=False)
                            try:
                                await inspect_one(session, binding, expected_role=args.expected_role, expected_database=args.expected_database)
                            finally:
                                await session.close()
                        finally:
                            await tx.rollback()
                    async with engine.begin() as connection:
                        session = AsyncSession(bind=connection, autoflush=False, expire_on_commit=False)
                        try:
                            changed = await reconcile_one(
                                session, binding, proof,
                                expected_role=args.expected_role, expected_database=args.expected_database,
                            )
                            return {"status": "reconciled" if changed else "unchanged"}
                        finally:
                            await session.close()
                async with engine.connect() as connection:
                    tx = await connection.begin()
                    try:
                        await connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                        session = AsyncSession(bind=connection, autoflush=False, expire_on_commit=False)
                        try:
                            return await inspect_one(
                                session, binding,
                                expected_role=args.expected_role,
                                expected_database=args.expected_database,
                            )
                        finally:
                            await session.close()
                    finally:
                        await tx.rollback()
            finally:
                await engine.dispose()
        print(json.dumps(asyncio.run(run()), sort_keys=True))
        return 0
    except (CleanupOperatorError, OSError, RuntimeError):
        print(json.dumps({"status": "blocked", "error": "cleanup_preflight_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
