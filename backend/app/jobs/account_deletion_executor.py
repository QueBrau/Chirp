"""Explicit two-stage operator runner for one approved c436 deletion request.

Prepare captures a private, expiring provider plan and performs no delete. Execute
requires that exact plan digest plus a still-valid operator attestation, current
database identity/ACL/schema, all admission flags, the committed account hold, and
zero registered work. This is an operator tool, never a scheduler entrypoint.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from types import SimpleNamespace

from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.config import get_settings
from app.db import get_session_factory
from app.jobs.account_deletion_preflight import inspect_request
from app.services.account_deletion_hold import DeletionHoldInvalid
from app.services.account_external_operations import inspect_registered_operation_drain
from app.services import account_provider_gcs as media
from app.services.account_fulfillment import PROVIDER_STEPS, run_deletion
from app.services.account_deletion_provider_factory import (
    ANALYTICS_DATASET_ID,
    ANALYTICS_PROJECT_ID,
    FIREBASE_PROJECT_ID,
    build_deletion_providers,
    capture_provider_snapshots,
    deserialize_analytics_snapshot,
    deserialize_media_snapshot,
)

EXPECTED_SCHEMA_HEAD = "0051"
PLAN_VERSION = 1
MAX_PLAN_BYTES = 2 * 1024 * 1024
MAX_PLAN_AGE_SECONDS = 900
MAX_APPROVAL_AGE_SECONDS = 3600
GCS_METADATA_ENV = "DISABLE_GCS_PYTHON_CLIENT_OTEL_BUCKET_METADATA"
ADMISSION_FLAGS = (
    "account_deletion_http_admission_enabled",
    "account_deletion_ws_admission_enabled",
    "account_deletion_delivery_admission_enabled",
    "account_deletion_deferred_cleanup_enabled",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EVIDENCE_REF = re.compile(r"^[A-Za-z0-9._:/-]{2,256}$")
_APPROVAL_FIELDS = frozenset({
    "type", "request_id", "user_id", "database_name", "database_role",
    "schema_head", "reviewed_by", "reviewed_at", "expires_at",
    "old_revisions_drained", "old_revisions_evidence_ref",
    "old_revisions_evidence_sha256", "admission_flags", "firebase_project",
    "analytics_project", "analytics_dataset", "bucket", "soft_delete_seconds",
    "analytics_tables", "analytics_exclusions", "analytics_freeze",
})
_PLAN_FIELDS = frozenset({
    "version", "request_id", "user_id", "created_at", "expires_at",
    "database", "approval_sha256", "approval", "admission_flags", "snapshot_bundle",
})


class ExecutorRefused(RuntimeError):
    """A sanitized refusal code safe for the operator CLI."""


def _canonical_uuid(value: object, name: str) -> uuid.UUID:
    try:
        parsed = uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise ExecutorRefused(f"{name}_invalid") from None
    if type(value) is not str or str(parsed) != value:
        raise ExecutorRefused(f"{name}_invalid")
    return parsed


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _parse_time(value: object, name: str) -> datetime:
    if type(value) is not str or not value or len(value) > 64:
        raise ExecutorRefused(f"{name}_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ExecutorRefused(f"{name}_invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExecutorRefused(f"{name}_invalid")
    return parsed.astimezone(timezone.utc)


def _private_file(path: str | Path, *, create: bool = False) -> tuple[int, Path]:
    target = Path(path).expanduser()
    parent = target.parent.resolve(strict=True)
    parent_stat = parent.stat()
    if (not stat.S_ISDIR(parent_stat.st_mode) or parent_stat.st_uid != os.geteuid()
            or stat.S_IMODE(parent_stat.st_mode) & 0o077):
        raise ExecutorRefused("private_directory_required")
    if create:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(target, flags, 0o600)
        except FileExistsError:
            raise ExecutorRefused("plan_already_exists") from None
        except OSError:
            raise ExecutorRefused("private_file_unavailable") from None
        return fd, target
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags)
        info = os.fstat(fd)
    except OSError:
        raise ExecutorRefused("private_file_unavailable") from None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > MAX_PLAN_BYTES):
        os.close(fd)
        raise ExecutorRefused("private_file_invalid")
    return fd, target


def _read_private_json(path: str | Path, *, limit: int = MAX_PLAN_BYTES) -> tuple[object, bytes]:
    fd, _ = _private_file(path)
    try:
        chunks = bytearray()
        while len(chunks) <= limit:
            part = os.read(fd, min(65536, limit + 1 - len(chunks)))
            if not part:
                break
            chunks.extend(part)
        if len(chunks) > limit:
            raise ExecutorRefused("private_file_too_large")
        raw = bytes(chunks)
    finally:
        os.close(fd)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise ExecutorRefused("private_json_invalid") from None
    return value, raw


def _write_private_json(path: str | Path, value: object) -> None:
    fd, target = _private_file(path, create=True)
    try:
        raw = _canonical_json(value)
        if len(raw) > MAX_PLAN_BYTES:
            raise ExecutorRefused("plan_too_large")
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            target.unlink()
        except OSError:
            pass
        raise


def _database_endpoint(database_url: str) -> dict[str, object]:
    try:
        url = make_url(database_url)
    except Exception:
        raise ExecutorRefused("database_url_invalid") from None
    if url.drivername not in {"postgresql+asyncpg", "postgresql"} or not url.host or not url.database:
        raise ExecutorRefused("postgres_database_required")
    allowed_query = {"sslmode", "ssl", "target_session_attrs", "application_name"}
    if set(url.query) - allowed_query:
        raise ExecutorRefused("database_url_query_unsupported")
    return {
        "driver": "postgresql",
        "host": url.host.lower(),
        "port": url.port or 5432,
        "database": url.database,
        "username": url.username or "",
        "query": {key: list(value) if isinstance(value, tuple) else value
                  for key, value in sorted(url.query.items())},
    }


def _require_database_url() -> tuple[str, dict[str, object]]:
    configured = os.environ.get("DATABASE_URL")
    if type(configured) is not str or not configured:
        raise ExecutorRefused("DATABASE_URL_required")
    settings_url = get_settings().database_url
    try:
        same = make_url(configured).render_as_string(hide_password=False) == make_url(settings_url).render_as_string(hide_password=False)
    except Exception:
        same = False
    if not same:
        raise ExecutorRefused("app_database_url_mismatch")
    return configured, _database_endpoint(configured)


def _validate_approval(
    value: object, *, request_id: uuid.UUID, user_id: uuid.UUID,
    expected_database: str, expected_role: str,
) -> tuple[dict[str, Any], str]:
    if type(value) is not dict or set(value) != _APPROVAL_FIELDS:
        raise ExecutorRefused("approval_file_invalid")
    if (value.get("type") != "c436_operator_approval_v1"
            or value.get("request_id") != str(request_id)
            or value.get("user_id") != str(user_id)
            or value.get("database_name") != expected_database
            or value.get("database_role") != expected_role
            or value.get("schema_head") != EXPECTED_SCHEMA_HEAD):
        raise ExecutorRefused("approval_binding_invalid")
    reviewer = value.get("reviewed_by")
    if type(reviewer) is not str or not 2 <= len(reviewer.strip()) <= 120:
        raise ExecutorRefused("approval_reviewer_invalid")
    now = datetime.now(timezone.utc)
    reviewed_at = _parse_time(value.get("reviewed_at"), "approval_reviewed_at")
    expires_at = _parse_time(value.get("expires_at"), "approval_expires_at")
    if (reviewed_at > now + timedelta(minutes=5)
            or now - reviewed_at > timedelta(seconds=MAX_APPROVAL_AGE_SECONDS)
            or expires_at <= now or expires_at > now + timedelta(seconds=MAX_APPROVAL_AGE_SECONDS)):
        raise ExecutorRefused("approval_expired")
    if value.get("old_revisions_drained") is not True:
        raise ExecutorRefused("old_revisions_not_attested_drained")
    evidence_ref = value.get("old_revisions_evidence_ref")
    if (type(evidence_ref) is not str or _EVIDENCE_REF.fullmatch(evidence_ref) is None
            or any(part in {"", ".", ".."} for part in evidence_ref.split("/"))
            or not _SHA256.fullmatch(str(value.get("old_revisions_evidence_sha256", "")))):
        raise ExecutorRefused("old_revisions_evidence_invalid")
    flags = value.get("admission_flags")
    if type(flags) is not dict or set(flags) != set(ADMISSION_FLAGS) or any(v is not True for v in flags.values()):
        raise ExecutorRefused("admission_flags_not_attested")
    bucket = get_settings().media_bucket_name
    if (value.get("firebase_project") != FIREBASE_PROJECT_ID
            or value.get("analytics_project") != ANALYTICS_PROJECT_ID
            or value.get("analytics_dataset") != ANALYTICS_DATASET_ID
            or not bucket or value.get("bucket") != bucket
            or type(value.get("soft_delete_seconds")) is not int
            or value["soft_delete_seconds"] < 0):
        raise ExecutorRefused("media_retention_approval_invalid")
    tables = value.get("analytics_tables")
    exclusions = value.get("analytics_exclusions")
    if (type(tables) is not list or not tables or tables != sorted(set(tables))
            or type(exclusions) is not list or len(exclusions) > 1):
        raise ExecutorRefused("analytics_scope_review_invalid")
    try:
        for row in exclusions:
            if type(row) is not dict or set(row) != {"name", "schema_digest", "reviewed_non_account_probe"}:
                raise ValueError
            analytics_exclusion = {key: row[key] for key in ("name", "schema_digest", "reviewed_non_account_probe")}
            from app.services.account_provider_analytics import AnalyticsExclusion
            AnalyticsExclusion(**analytics_exclusion).validate()
        from app.services.account_provider_analytics import STDOUT
        if any(type(name) is not str or STDOUT.fullmatch(name) is None for name in tables):
            raise ValueError
        if tables != sorted(set(tables)):
            raise ValueError
    except (TypeError, ValueError):
        raise ExecutorRefused("analytics_scope_review_invalid") from None
    if len(tables) + len(exclusions) > 32:
        raise ExecutorRefused("analytics_scope_review_invalid")
    freeze = value.get("analytics_freeze")
    try:
        from app.services.account_deletion_provider_factory import analytics_freeze_from_approval
        analytics_freeze_from_approval(freeze, user_id=user_id)
    except (TypeError, ValueError):
        raise ExecutorRefused("analytics_freeze_invalid") from None
    approval_digest = _sha256(value)
    return dict(value), approval_digest


def _runtime_flags() -> dict[str, bool]:
    settings = get_settings()
    result = {name: getattr(settings, name, False) is True for name in ADMISSION_FLAGS}
    if not all(result.values()):
        raise ExecutorRefused("admission_flags_not_enabled")
    if os.environ.get(GCS_METADATA_ENV, "").strip().lower() not in {"1", "true", "yes", "on"}:
        raise ExecutorRefused("gcs_sdk_background_requests_enabled")
    return result


_ACL: dict[str, tuple[str, ...]] = {
    "alembic_version": ("SELECT",),
    "users": ("SELECT",),
    "account_data_requests": ("SELECT",),
    "account_fulfillment_steps": ("SELECT",),
    "account_deletion_holds": ("SELECT",),
    "account_external_operations": ("SELECT",),
    "account_external_operation_scopes": ("SELECT",),
    "account_ws_connections": ("SELECT",),
    "account_delivery_attempts": ("SELECT",),
    "account_payment_attempts": ("SELECT",),
    "account_post_deletion_cleanup": ("SELECT",),
    "account_data_artifacts": ("SELECT", "DELETE"),
    "alumni_profiles": ("DELETE",),
    "campus_verifications": ("DELETE",),
    "memberships": (),
    "role_terms": (),
    "chapter_invites": (),
    "posts": ("SELECT",),
    "post_comments": (),
    "chirps": ("SELECT",),
    "job_posts": ("DELETE",),
    "post_likes": ("DELETE",),
    "chirp_votes": ("DELETE",),
    "poll_votes": ("DELETE",),
    "house_ballots": ("DELETE",),
    "user_blocks": ("DELETE",),
    "conversation_members": (),
    "devices": ("SELECT",),
    "signed_prekeys": ("DELETE",),
    "one_time_prekeys": ("DELETE",),
    "kyber_prekeys": ("DELETE",),
    "chapter_stripe_customers": ("SELECT",),
    "chapters": ("SELECT",),
    "dues_payment_plans": ("SELECT",),
    "dues_payment_intents": ("SELECT",),
}
_COLUMN_ACL: dict[str, dict[str, set[str]]] = {
    "users": {"SELECT": {"id", "firebase_uid", "email", "is_ghost"}, "UPDATE": {
        "firebase_uid", "email", "display_name", "avatar_url", "campus_id",
        "pseudonym_seed", "campus_verified_at", "is_platform_admin",
        "suspended_at", "suspension_reason", "suspended_by", "is_ghost",
    }},
    "account_data_requests": {"SELECT": {"id", "user_id", "kind", "status", "open_key",
        "failure_code", "provider_steps", "fulfillment_lease_token", "fulfillment_lease_expires_at",
        "completed_at", "updated_at", "retention_reasons"}, "UPDATE": {
        "status", "open_key", "failure_code", "provider_steps", "fulfillment_lease_token",
        "fulfillment_lease_expires_at", "completed_at", "updated_at", "retention_reasons",
    }},
    "memberships": {"SELECT": {"id", "user_id", "status"}, "UPDATE": {"status"}},
    "role_terms": {"SELECT": {"id", "membership_id", "ended_at"}, "UPDATE": {"ended_at"}},
    "chapter_invites": {"SELECT": {"id", "created_by", "revoked_at"}, "UPDATE": {"revoked_at"}},
    "posts": {"SELECT": {"id", "author_id", "body", "media_urls", "deleted_at", "removed_reason"},
              "UPDATE": {"body", "media_urls", "deleted_at", "removed_reason"}},
    "post_comments": {"SELECT": {"id", "author_id", "body", "deleted_at", "removed_reason"},
                      "UPDATE": {"body", "deleted_at", "removed_reason"}},
    "chirps": {"SELECT": {"id", "author_id", "body", "removed_at", "removed_reason"},
               "UPDATE": {"body", "removed_at", "removed_reason"}},
    "conversation_members": {"SELECT": {"conversation_id", "user_id", "left_at"}, "UPDATE": {"left_at"}},
    "devices": {"SELECT": {"id", "user_id", "revoked_at"}, "UPDATE": {"revoked_at"}},
    "alumni_profiles": {"SELECT": {"user_id"}},
    "campus_verifications": {"SELECT": {"user_id"}},
    "account_data_artifacts": {"SELECT": {"request_id"}},
    "job_posts": {"SELECT": {"posted_by"}},
    "post_likes": {"SELECT": {"user_id"}},
    "chirp_votes": {"SELECT": {"user_id"}},
    "poll_votes": {"SELECT": {"user_id"}},
    "house_ballots": {"SELECT": {"voter_id"}},
    "user_blocks": {"SELECT": {"blocker_id"}},
    "signed_prekeys": {"SELECT": {"device_id"}},
    "one_time_prekeys": {"SELECT": {"device_id"}},
    "kyber_prekeys": {"SELECT": {"device_id"}},
    "account_fulfillment_steps": {
        "INSERT": {"request_id", "step_key", "idempotency_key", "provider_ref", "last_error",
                   "started_at", "completed_at", "lease_token", "lease_expires_at"},
        "UPDATE": {"status", "attempt_count", "idempotency_key", "provider_ref", "last_error",
                   "started_at", "completed_at", "lease_token", "lease_expires_at"},
    },
    "account_post_deletion_cleanup": {"INSERT": {
        "request_id", "user_id", "category", "state", "scope", "scope_sha256",
        "last_pass_digest", "remaining_count", "attempt_count", "next_attempt_at", "updated_at",
    }},
}
_WRITE_TABLES = tuple(sorted(
    {table for table, privileges in _ACL.items() if any(p != "SELECT" for p in privileges)}
    | {table for table, privileges in _COLUMN_ACL.items()
       if any(privilege in {"UPDATE", "INSERT"} for privilege in privileges)}
))


async def _database_identity_and_acl(
    session: AsyncSession, *, expected_database: str, expected_role: str,
    endpoint: dict[str, object],
) -> dict[str, object]:
    if session.in_transaction() or session.new or session.dirty or session.deleted:
        raise ExecutorRefused("database_session_not_fresh")
    await session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED READ ONLY"))
    row = (await session.execute(text(
        "SELECT current_database() AS database_name, current_user AS role_name, "
        "current_schema() AS schema_name"
    ))).mappings().one()
    database_name, role_name = str(row["database_name"]), str(row["role_name"])
    if database_name != expected_database or role_name != expected_role or row["schema_name"] != "public":
        raise ExecutorRefused("database_identity_mismatch")
    if endpoint.get("database") != database_name or endpoint.get("username") != expected_role:
        raise ExecutorRefused("database_endpoint_mismatch")
    role_flags = (await session.execute(text(
        "SELECT r.rolsuper, r.rolbypassrls, r.rolcreaterole, r.rolcreatedb, r.rolreplication, "
        "pg_has_role(r.oid, d.datdba, 'MEMBER') AS database_owner, "
        "EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_roles owner_role ON owner_role.oid = c.relowner "
        "WHERE n.nspname = 'public' AND c.relname = ANY(:write_tables) "
        "AND pg_has_role(current_user, owner_role.oid, 'MEMBER')) AS owns_write_table "
        "FROM pg_roles r JOIN pg_database d ON d.datname = current_database() "
        "WHERE r.rolname = current_user"
    ), {"write_tables": list(_WRITE_TABLES)})).one_or_none()
    if role_flags is None or any(bool(value) for value in role_flags):
        raise ExecutorRefused("database_role_too_privileged")
    dangerous_membership = await session.scalar(text(
        "SELECT EXISTS (SELECT 1 FROM pg_roles target WHERE target.rolname = ANY(:roles) "
        "AND pg_has_role(current_user, target.oid, 'MEMBER'))"
    ), {"roles": ["pg_read_server_files", "pg_write_server_files",
                   "pg_execute_server_program", "pg_read_all_data", "pg_write_all_data",
                   "pg_monitor", "pg_signal_backend"]})
    if dangerous_membership:
        raise ExecutorRefused("database_role_too_privileged")
    versions = list((await session.scalars(text("SELECT version_num FROM alembic_version"))).all())
    if versions != [EXPECTED_SCHEMA_HEAD]:
        raise ExecutorRefused("database_schema_mismatch")
    missing: list[str] = []
    write_privileges = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "TRIGGER")
    allowed_table_writes = [
        f"{table}:{privilege}" for table, privileges in _ACL.items()
        for privilege in privileges if privilege in write_privileges
    ]
    allowed_column_writes = [
        f"{table}.{column}:{privilege}"
        for table, privileges in _COLUMN_ACL.items()
        for privilege, columns in privileges.items()
        if privilege in {"INSERT", "UPDATE", "REFERENCES"}
        for column in columns
    ]
    excessive = await session.scalar(text(
        "WITH app_tables AS ("
        " SELECT c.oid, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace"
        " WHERE n.nspname='public' AND c.relkind IN ('r','p')"
        "), table_excess AS ("
        " SELECT 1 FROM app_tables t CROSS JOIN unnest(CAST(:write_privileges AS text[])) p(privilege)"
        " WHERE has_table_privilege(current_user, t.oid, p.privilege)"
        " AND NOT (t.relname || ':' || p.privilege = ANY(CAST(:allowed_table_writes AS text[])))"
        "), column_excess AS ("
        " SELECT 1 FROM app_tables t JOIN pg_attribute a ON a.attrelid=t.oid"
        " CROSS JOIN unnest(ARRAY['INSERT','UPDATE','REFERENCES']::text[]) p(privilege)"
        " WHERE a.attnum>0 AND NOT a.attisdropped"
        " AND has_column_privilege(current_user, t.oid, a.attnum, p.privilege)"
        " AND NOT ((t.relname || '.' || a.attname || ':' || p.privilege)"
        " = ANY(CAST(:allowed_column_writes AS text[])))"
        ") SELECT EXISTS (SELECT 1 FROM table_excess UNION ALL SELECT 1 FROM column_excess)"
    ), {"write_privileges": list(write_privileges),
        "allowed_table_writes": allowed_table_writes,
        "allowed_column_writes": allowed_column_writes})
    if excessive:
        raise ExecutorRefused("database_privileges_excessive")
    for table, privileges in _ACL.items():
        for privilege in privileges:
            allowed = await session.scalar(text(
                "SELECT has_table_privilege(current_user, :relation, :privilege)"
            ), {"relation": f"public.{table}", "privilege": privilege})
            if allowed is not True:
                missing.append(f"{table}:{privilege}")
    # Table-level UPDATE is unnecessarily broad for a destructive executor.
    # Check only columns this transaction can write, and the columns used to
    # select its owned rows (PostgreSQL requires SELECT for predicate columns).
    for table, by_privilege in _COLUMN_ACL.items():
        for privilege, columns in by_privilege.items():
            for column in sorted(columns):
                allowed = await session.scalar(text(
                    "SELECT has_column_privilege(current_user, :relation, :column_name, :privilege)"
                ), {"relation": f"public.{table}", "column_name": column, "privilege": privilege})
                if allowed is not True:
                    missing.append(f"{table}.{column}:{privilege}")
    if missing:
        raise ExecutorRefused("database_privileges_missing")
    return {"database": database_name, "role": role_name, "schema_head": EXPECTED_SCHEMA_HEAD,
            "endpoint": endpoint}


async def _assert_hold_drain_and_request(
    *, request_id: uuid.UUID, user_id: uuid.UUID,
) -> tuple[dict[str, object], Any]:
    async with get_session_factory()() as session:
        request = await session.get(models.AccountDataRequest, request_id)
        user = await session.get(models.User, user_id)
        if request is None or user is None or request.user_id != user_id or request.kind != "deletion":
            raise ExecutorRefused("deletion_target_not_found")
        if request.status in {"completed", "failed", "canceled", "verifying"}:
            raise ExecutorRefused("deletion_request_not_runnable")
        report = await inspect_request(session, request_id=request_id, expected_user_id=user_id)
        blocked = set(report.get("blockers", [])) - {"journal_steps_missing"}
        if report.get("unknown_outcome_steps") or blocked:
            raise ExecutorRefused("deletion_preflight_blocked")
        await session.rollback()
    try:
        drain = await inspect_registered_operation_drain(user_id=user_id, request_id=request_id)
    except DeletionHoldInvalid:
        raise ExecutorRefused("active_deletion_hold_required") from None
    if not drain.registered_operations_drained:
        raise ExecutorRefused("registered_operations_not_drained")
    # Re-read request/user after the exclusive hold drain snapshot. The runner
    # independently repeats both checks immediately before work and finalization.
    async with get_session_factory()() as session:
        request = await session.get(models.AccountDataRequest, request_id)
        user = await session.get(models.User, user_id)
        if request is None or user is None or request.user_id != user_id:
            raise ExecutorRefused("deletion_target_not_found")
        if request.status in {"completed", "failed", "canceled", "verifying"}:
            raise ExecutorRefused("deletion_request_not_runnable")
        request_view = {"status": request.status, "created_at": request.created_at.isoformat()}
        user_view = SimpleNamespace(id=user.id, firebase_uid=user.firebase_uid)
        await session.rollback()
    return request_view, user_view


async def prepare_deletion_plan(
    *, request_id: uuid.UUID, expected_user_id: uuid.UUID, plan_path: str | Path,
    approval_path: str | Path, expected_database: str, expected_role: str,
    snapshot_builder: Callable[..., Any] | None = None,
) -> str:
    database_url, endpoint = _require_database_url()
    flags = _runtime_flags()
    approval_value, _ = _read_private_json(approval_path, limit=64 * 1024)
    approval, approval_digest = _validate_approval(
        approval_value, request_id=request_id, user_id=expected_user_id,
        expected_database=expected_database, expected_role=expected_role,
    )
    async with get_session_factory()() as session:
        database = await _database_identity_and_acl(
            session, expected_database=expected_database, expected_role=expected_role,
            endpoint=endpoint,
        )
        request_view, user = await _assert_hold_drain_and_request(
            request_id=request_id, user_id=expected_user_id,
        )
        await session.rollback()
        builder = snapshot_builder or capture_provider_snapshots
        bundle = await builder(
            session=session, user=user, request_id=request_id, approval=approval,
        )
        await session.rollback()
    now = datetime.now(timezone.utc)
    analytics_snapshot = deserialize_analytics_snapshot(bundle["analytics_snapshot"])
    media_snapshot = deserialize_media_snapshot(bundle["media_snapshot"])
    if (analytics_snapshot.request_id != str(request_id)
            or analytics_snapshot.user_id != str(expected_user_id)
            or analytics_snapshot.project != ANALYTICS_PROJECT_ID
            or analytics_snapshot.dataset != ANALYTICS_DATASET_ID
            or media_snapshot.user_id != str(expected_user_id)):
        raise ExecutorRefused("provider_snapshot_binding_invalid")
    expiry = min(
        now + timedelta(seconds=MAX_PLAN_AGE_SECONDS),
        datetime.fromtimestamp(analytics_snapshot.created_at + MAX_PLAN_AGE_SECONDS, timezone.utc),
        _parse_time(approval["expires_at"], "approval_expires_at"),
        datetime.fromtimestamp(approval["analytics_freeze"]["expires_at"], timezone.utc),
    )
    plan = {
        "version": PLAN_VERSION,
        "request_id": str(request_id),
        "user_id": str(expected_user_id),
        "created_at": now.isoformat(),
        "expires_at": expiry.isoformat(),
        "database": database,
        "approval_sha256": approval_digest,
        "approval": approval,
        "admission_flags": flags,
        "request": request_view,
        "snapshot_bundle": bundle,
    }
    if expiry <= now:
        raise ExecutorRefused("plan_snapshot_expired")
    _validate_snapshot_bundle(bundle, request_id=request_id, user_id=expected_user_id)
    digest = _sha256(plan)
    envelope = {"plan": plan, "sha256": digest}
    _write_private_json(plan_path, envelope)
    return digest


def _parse_plan(value: object, *, confirm_digest: str) -> tuple[dict[str, Any], str]:
    if type(value) is not dict or set(value) != {"plan", "sha256"}:
        raise ExecutorRefused("plan_invalid")
    plan = value.get("plan")
    digest = value.get("sha256")
    if (type(plan) is not dict or set(plan) != (_PLAN_FIELDS | {"request"})
            or type(digest) is not str or not _SHA256.fullmatch(digest)
            or _sha256(plan) != digest or confirm_digest != digest):
        raise ExecutorRefused("plan_digest_mismatch")
    if plan.get("version") != PLAN_VERSION:
        raise ExecutorRefused("plan_version_invalid")
    _canonical_uuid(plan.get("request_id"), "request_id")
    _canonical_uuid(plan.get("user_id"), "user_id")
    now = datetime.now(timezone.utc)
    created, expires = _parse_time(plan.get("created_at"), "plan_created_at"), _parse_time(plan.get("expires_at"), "plan_expires_at")
    if expires <= now or created > now + timedelta(minutes=5) or now - created > timedelta(seconds=MAX_PLAN_AGE_SECONDS):
        raise ExecutorRefused("plan_expired")
    if expires - created > timedelta(seconds=MAX_PLAN_AGE_SECONDS):
        raise ExecutorRefused("plan_expiry_invalid")
    request = plan.get("request")
    if (type(request) is not dict or set(request) != {"status", "created_at"}
            or request.get("status") not in {"received", "processing", "blocked", "partially_completed"}):
        raise ExecutorRefused("plan_request_invalid")
    _parse_time(request.get("created_at"), "plan_request_created_at")
    approval_digest = plan.get("approval_sha256")
    if type(approval_digest) is not str or not _SHA256.fullmatch(approval_digest):
        raise ExecutorRefused("plan_approval_digest_invalid")
    database = plan.get("database")
    if (type(database) is not dict or set(database) != {"database", "role", "schema_head", "endpoint"}
            or database.get("schema_head") != EXPECTED_SCHEMA_HEAD
            or type(database.get("endpoint")) is not dict):
        raise ExecutorRefused("plan_database_invalid")
    _validate_snapshot_bundle(
        plan.get("snapshot_bundle"), request_id=uuid.UUID(plan["request_id"]),
        user_id=uuid.UUID(plan["user_id"]),
    )
    return plan, digest


def _validate_snapshot_bundle(value: object, *, request_id: uuid.UUID, user_id: uuid.UUID) -> None:
    expected = {"stripe_references", "media_snapshot", "media_references", "media_retention",
                "analytics_snapshot", "analytics_freeze"}
    if type(value) is not dict or set(value) != expected:
        raise ExecutorRefused("provider_snapshot_invalid")
    try:
        media_snapshot = deserialize_media_snapshot(value["media_snapshot"])
        analytics_snapshot = deserialize_analytics_snapshot(value["analytics_snapshot"])
        from app.services.account_deletion_provider_factory import analytics_freeze_from_approval
        freeze = analytics_freeze_from_approval(value["analytics_freeze"], user_id=user_id)
        refs = value["stripe_references"]
        from app.services.account_provider_stripe import StripeCustomerReference, _valid_provider_id
        parsed_refs = tuple(StripeCustomerReference(**row) for row in refs)
    except (TypeError, ValueError, KeyError):
        raise ExecutorRefused("provider_snapshot_invalid") from None
    if (media_snapshot.user_id != str(user_id)
            or analytics_snapshot.request_id != str(request_id)
            or analytics_snapshot.user_id != str(user_id)
            or analytics_snapshot.project != ANALYTICS_PROJECT_ID
            or analytics_snapshot.dataset != ANALYTICS_DATASET_ID
            or analytics_snapshot.freeze_id != freeze.freeze_id
            or len(parsed_refs) > 20
            or len({ref.customer_id for ref in parsed_refs}) != len(parsed_refs)
            or any(not _valid_provider_id(ref.connected_account_id, "acct_")
                   or not _valid_provider_id(ref.customer_id, "cus_") for ref in parsed_refs)):
        raise ExecutorRefused("provider_snapshot_binding_invalid")
    media_references = value["media_references"]
    if (type(media_references) is not list
            or any(type(name) is not str for name in media_references)
            or media_references != sorted(set(media_references))
            or any(not media._owned_name(name, user_id) for name in media_references)):
        raise ExecutorRefused("provider_snapshot_binding_invalid")
    try:
        generations_valid = all(media._generation(item.generation) == item.generation
                                for item in media_snapshot.objects)
    except Exception:
        generations_valid = False
    if (media_snapshot.prefixes != media.PREFIXES
            or len(media_snapshot.objects) > media.MAX_OBJECTS
            or len({item.name for item in media_snapshot.objects}) != len(media_snapshot.objects)
            or not generations_valid
            or any(not media._owned_name(item.name, user_id)
                   or not any(item.name.startswith(f"{prefix}{user_id}/") for prefix in media.PREFIXES)
                   for item in media_snapshot.objects)):
        raise ExecutorRefused("provider_snapshot_binding_invalid")
    retention = value["media_retention"]
    if (type(retention) is not dict
            or set(retention) != {"bucket", "versioning_enabled", "soft_delete_seconds", "verified"}
            or retention.get("bucket") != media_snapshot.bucket
            or retention.get("versioning_enabled") is not False
            or retention.get("verified") is not True
            or type(retention.get("soft_delete_seconds")) is not int
            or retention["soft_delete_seconds"] < 0):
        raise ExecutorRefused("provider_snapshot_binding_invalid")


async def execute_deletion_plan(
    *, plan_path: str | Path, approval_path: str | Path, confirm_digest: str,
    expected_request_id: uuid.UUID, expected_user_id: uuid.UUID,
    expected_database: str, expected_role: str,
    provider_builder: Callable[..., Any] | None = None,
) -> dict[str, object]:
    _, endpoint = _require_database_url()
    flags = _runtime_flags()
    envelope, _ = _read_private_json(plan_path)
    plan, digest = _parse_plan(envelope, confirm_digest=confirm_digest)
    request_id = _canonical_uuid(plan["request_id"], "request_id")
    user_id = _canonical_uuid(plan["user_id"], "user_id")
    if request_id != expected_request_id or user_id != expected_user_id:
        raise ExecutorRefused("plan_target_mismatch")
    approval_value, _ = _read_private_json(approval_path, limit=64 * 1024)
    approval, approval_digest = _validate_approval(
        approval_value, request_id=request_id, user_id=user_id,
        expected_database=expected_database, expected_role=expected_role,
    )
    if (approval_digest != plan.get("approval_sha256")
            or plan.get("approval") != approval_value
            or plan.get("admission_flags") != flags
            or plan.get("database", {}).get("endpoint") != endpoint
            or plan.get("database", {}).get("database") != expected_database
            or plan.get("database", {}).get("role") != expected_role
            or plan.get("database", {}).get("schema_head") != EXPECTED_SCHEMA_HEAD):
        raise ExecutorRefused("plan_current_binding_mismatch")
    async with get_session_factory()() as session:
        database = await _database_identity_and_acl(
            session, expected_database=expected_database, expected_role=expected_role,
            endpoint=endpoint,
        )
        if database != plan["database"]:
            raise ExecutorRefused("plan_database_identity_changed")
        request_view, user = await _assert_hold_drain_and_request(request_id=request_id, user_id=user_id)
        if request_view != plan.get("request"):
            raise ExecutorRefused("plan_request_changed")
        # Verify DB-owned provider targets still match the reviewed snapshot before
        # initializing any provider SDK or making any deletion call.
        from app.services.account_provider_stripe import load_customer_references
        refs = await load_customer_references(session, user_id)
        expected_refs = tuple((row["connected_account_id"], row["customer_id"])
                              for row in plan["snapshot_bundle"]["stripe_references"])
        if tuple((ref.connected_account_id, ref.customer_id) for ref in refs) != expected_refs:
            raise ExecutorRefused("plan_stripe_scope_changed")
        bucket = get_settings().media_bucket_name
        from app.services import account_provider_gcs as gcs
        media_refs = sorted(await gcs._owned_references(session, user_id, bucket))
        if media_refs != plan["snapshot_bundle"].get("media_references"):
            raise ExecutorRefused("plan_media_references_changed")
        await session.rollback()
    builder = provider_builder or build_deletion_providers
    providers, firebase_app = await builder(
        user_id=user_id, request_id=request_id, snapshot_bundle=plan["snapshot_bundle"],
    )
    from firebase_admin import delete_app
    try:
        async with get_session_factory()() as session:
            if session.in_transaction() or session.new or session.dirty or session.deleted:
                raise ExecutorRefused("executor_session_not_fresh")
            isolation = await session.scalar(text("SELECT current_setting('transaction_isolation')"))
            if isolation != "read committed":
                raise ExecutorRefused("executor_isolation_invalid")
            result = await run_deletion(session, request_id, user, providers)
            return {
                "status": result.status,
                "completed_step_count": len(result.completed_steps),
                "failed_step": result.failed_step,
                "plan_sha256": digest,
            }
    finally:
        for provider in providers.values():
            rest = getattr(provider, "rest", None)
            client = getattr(rest, "client", None)
            if client is not None and not client.is_closed:
                await client.aclose()
        if firebase_app is not None:
            delete_app(firebase_app)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", action="store_true", help="capture and write a private review plan; no delete")
    mode.add_argument("--execute", action="store_true", help="execute one reviewed plan")
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--expected-user-id", required=True)
    parser.add_argument("--approval-file", required=True)
    parser.add_argument("--plan-file", required=True)
    parser.add_argument("--expected-database", required=True)
    parser.add_argument("--expected-role", required=True)
    parser.add_argument("--confirm-digest")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        request_id = _canonical_uuid(args.request_id, "request_id")
        user_id = _canonical_uuid(args.expected_user_id, "user_id")
        if args.prepare:
            if args.confirm_digest is not None:
                raise ExecutorRefused("prepare_confirmation_not_allowed")
            digest = asyncio.run(prepare_deletion_plan(
                request_id=request_id, expected_user_id=user_id,
                plan_path=args.plan_file, approval_path=args.approval_file,
                expected_database=args.expected_database, expected_role=args.expected_role,
            ))
            print(json.dumps({"status": "prepared_for_review", "plan_sha256": digest}, sort_keys=True))
            return 0
        if type(args.confirm_digest) is not str or not _SHA256.fullmatch(args.confirm_digest):
            raise ExecutorRefused("confirmation_digest_required")
        result = asyncio.run(execute_deletion_plan(
            plan_path=args.plan_file, approval_path=args.approval_file,
            confirm_digest=args.confirm_digest,
            expected_request_id=request_id, expected_user_id=user_id,
            expected_database=args.expected_database, expected_role=args.expected_role,
        ))
    except ExecutorRefused as exc:
        print(json.dumps({"status": "refused", "code": str(exc)}, sort_keys=True))
        return 2
    except Exception:
        # Do not echo SQL URLs, SDK exceptions, provider objects or credentials.
        print(json.dumps({"status": "failed", "code": "operator_executor_failed"}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
