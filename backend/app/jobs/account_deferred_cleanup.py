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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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
from app.services.account_post_deletion_providers import PostDeletionProviders
from app.services.account_provider_gcs import MediaRetentionReview
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
        try:
            stat_result = os.stat(credentials_file, follow_symlinks=False)
        except OSError:
            raise CleanupOperatorError("media_credentials_file_invalid") from None
        if (not os.path.isfile(credentials_file) or stat_result.st_mode & 0o077
                or stat_result.st_uid != os.getuid()):
            raise CleanupOperatorError("media_credentials_file_invalid")
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


def _manifest(path: str, *, category: str, binding: CleanupBinding) -> dict[str, Any]:
    try:
        raw = Path(path).read_bytes()
        if len(raw) > 64 * 1024:
            raise CleanupOperatorError("provider_manifest_too_large")
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise CleanupOperatorError("provider_manifest_invalid") from None
    if type(value) is not dict or value.get("schema") != EXPECTED_SCHEMA:
        raise CleanupOperatorError("provider_manifest_invalid")
    if (value.get("obligation_id") != str(binding.obligation_id)
            or value.get("request_id") != str(binding.request_id)
            or value.get("user_id") != str(binding.user_id)
            or value.get("category") != category
            or value.get("scope_sha256") != binding.scope_sha256):
        raise CleanupOperatorError("provider_manifest_binding_invalid")
    return value


def _providers_from_manifest(manifest: dict[str, Any], *, category: str,
                             scope: dict[str, Any]) -> PostDeletionProviders:
    if category == "temporary_media":
        config = manifest.get("media")
        if (type(config) is not dict or config.get("project") != EXPECTED_GCS_PROJECT
                or config.get("bucket") != scope.get("bucket")):
            raise CleanupOperatorError("media_manifest_invalid")
        reader = build_media_reader(
            bucket=str(config.get("bucket", "")), project=EXPECTED_GCS_PROJECT,
            credentials_file=(config.get("credentials_file")
                              if isinstance(config.get("credentials_file"), str) else None),
            use_adc=config.get("use_adc") is True,
        )
        expected_soft_delete = config.get("soft_delete_seconds")
        if type(expected_soft_delete) is not int or type(config.get("versioning_enabled")) is not bool:
            raise CleanupOperatorError("media_manifest_invalid")
        retention = reader.reviewed_retention(
            expected_soft_delete_seconds=expected_soft_delete,
            expected_versioning_enabled=config["versioning_enabled"],
        )
        return PostDeletionProviders(media_reader=reader, media_retention=retention)
    config = manifest.get("analytics")
    if type(config) is not dict or config.get("project") != scope.get("project") or config.get("dataset") != scope.get("dataset"):
        raise CleanupOperatorError("analytics_manifest_binding_invalid")
    token_env = config.get("token_env")
    if type(token_env) is not str or not re.fullmatch(r"C436_[A-Z0-9_]{1,80}", token_env):
        raise CleanupOperatorError("analytics_manifest_invalid")
    token = os.environ.get(token_env)
    if not token or len(token) > 16_384:
        raise CleanupOperatorError("analytics_token_missing")
    return PostDeletionProviders(analytics_token=token)


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
                     expected_database: str, write: bool = False) -> dict[str, Any]:
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
        "account_post_deletion_cleanup": ("SELECT",),
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
    if write:
        # The worker never needs table-wide UPDATE. These are the only columns
        # changed by claim/settle transitions; immutable binding columns stay
        # protected by column grants and the SQL predicates.
        for column in ("state", "owner_token", "lease_expires_at", "attempt_count",
                       "updated_at", "last_pass_digest", "remaining_count",
                       "last_error", "next_attempt_at", "completed_at"):
            allowed = await session.scalar(text(
                "SELECT has_column_privilege(current_user, :table, :column, 'UPDATE')"
            ), {"table": "public.account_post_deletion_cleanup", "column": column})
            if allowed is not True:
                missing.append(f"account_post_deletion_cleanup:{column}:UPDATE")
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
        "_scope": row.scope,
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
        await _preflight(session, expected_role=expected_role, expected_database=expected_database, write=True)
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
    parser.add_argument("--provider-manifest")
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
        if args.apply_one and not args.provider_manifest:
            raise CleanupOperatorError("provider_manifest_required_for_apply")
        if not args.apply_one and args.provider_manifest:
            raise CleanupOperatorError("provider_manifest_requires_apply")
        proof = _load_proof(args.reconcile_proof, binding) if args.reconcile_proof else None
        if args.apply_one and proof is not None:
            raise CleanupOperatorError("cleanup_modes_conflict")
        async def run() -> dict[str, Any]:
            url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
            engine = create_async_engine(url, connect_args={"timeout": 10})
            try:
                if args.apply_one:
                    async with engine.connect() as connection:
                        tx = await connection.begin()
                        try:
                            await connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                            session = AsyncSession(bind=connection, autoflush=False, expire_on_commit=False)
                            try:
                                info = await inspect_one(session, binding, expected_role=args.expected_role, expected_database=args.expected_database)
                            finally:
                                await session.close()
                        finally:
                            await tx.rollback()
                    scope = info.pop("_scope")
                    manifest = _manifest(args.provider_manifest, category=binding.category, binding=binding)
                    providers = _providers_from_manifest(manifest, category=binding.category, scope=scope)
                    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
                    return await run_one_batch(
                        factory, providers, binding,
                        expected_role=args.expected_role, expected_database=args.expected_database,
                    )
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
        result = asyncio.run(run())
        result.pop("_scope", None)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (CleanupOperatorError, OSError, RuntimeError):
        print(json.dumps({"status": "blocked", "error": "cleanup_preflight_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
