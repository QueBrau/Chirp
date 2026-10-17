"""Read-only operator inspection for one account-deletion request.

This command never invokes the deletion executor or provider SDKs. It binds one
request to one expected user, validates the durable journal, and reports why an
operator must still stop.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app import models
from app.services.account_fulfillment import (
    OUTCOME_UNKNOWN_ERRORS,
    PROVIDER_STEPS,
    ProviderUnavailable,
    provider_attempt_may_have_started,
    validate_provider_receipt,
)
from app.services.account_provider_retention import retention_only_receipt_from_journal

REQUIRED_STEPS = PROVIDER_STEPS + ("database_tombstone",)
SAFE_FAILURE_CODES = frozenset({
    "provider_not_configured",
    "provider_confirmation_missing",
    "provider_step_incomplete",
    "manual_processing_required",
})
ALLOWED_REQUEST_KINDS = frozenset({"deletion"})
ALLOWED_REQUEST_STATUSES = frozenset({"completed", "blocked", "processing", "received", "partially_completed"})
ALLOWED_STEP_STATUSES = frozenset({"pending", "running", "succeeded", "failed", "manual_review"})


class SafeParser(argparse.ArgumentParser):
    """Reject invalid arguments without echoing potentially private values."""

    def error(self, message: str) -> None:
        self.exit(2, '{"error_type": "invalid_arguments", "status": "failed"}\n')


def _parse_uuid(value: str, name: str) -> uuid.UUID:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{name}_must_be_uuid") from exc
    if str(parsed) != value.lower():
        raise ValueError(f"{name}_must_be_canonical_uuid")
    return parsed


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


def _lease_state(token: object, expires_at: datetime | None, now: datetime) -> str:
    if token is None and expires_at is None:
        return "absent"
    if token is None or expires_at is None:
        return "malformed"
    return "active" if expires_at > now else "expired"


def _receipt(step_key: str, value: object, expected_user_id: uuid.UUID, request_id: uuid.UUID) -> tuple[bool, dict[str, object], str | None]:
    """Validate a receipt while returning only structural, non-sensitive facts."""
    if isinstance(value, dict) and value.get("receipt_type") == "retention_only":
        try:
            if step_key != "email_logs_backups":
                raise ValueError("retention_receipt_invalid")
            receipt = retention_only_receipt_from_journal(
                value, subject=expected_user_id, request_id=request_id,
            )
        except (TypeError, ValueError):
            return False, {"present": True}, "provider_confirmation_invalid"
        return True, {
            "present": True,
            "disposition": receipt.action,
            "expiry": receipt.expiry,
            "email_backend": receipt.email_backend,
            "scope_count": len(receipt.scope),
            "retention_code_count": len(receipt.retained_with_reason),
        }, None
    try:
        retained = validate_provider_receipt(step_key, expected_user_id, value, expected_request_id=request_id)
    except (ProviderUnavailable, TypeError, ValueError):
        return False, {"present": isinstance(value, dict)}, "provider_confirmation_invalid"
    scope = value.get("scope") if isinstance(value, dict) else None
    if not isinstance(scope, list):
        return False, {"present": True}, "provider_confirmation_invalid"
    return True, {
        "present": True,
        "confirmed": True,
        "readback": True,
        "scope_count": len(scope),
        "retention_code_count": len(retained),
    }, None


async def inspect_request(
    session: AsyncSession,
    *,
    request_id: uuid.UUID,
    expected_user_id: uuid.UUID,
) -> dict[str, object]:
    """Inspect one owned deletion request in the caller's read-only transaction."""
    request = await session.scalar(select(models.AccountDataRequest).where(
        models.AccountDataRequest.id == request_id,
        models.AccountDataRequest.user_id == expected_user_id,
    ))
    if request is None:
        return {"status": "not_found_or_owner_mismatch", "request_found": False}
    user = await session.scalar(select(models.User).where(models.User.id == expected_user_id))
    steps = list((await session.scalars(
        select(models.AccountFulfillmentStep)
        .where(models.AccountFulfillmentStep.request_id == request_id)
        .order_by(models.AccountFulfillmentStep.step_key)
        .limit(len(REQUIRED_STEPS) + 1)
    )).all())
    by_key = {step.step_key: step for step in steps}
    missing = [key for key in REQUIRED_STEPS if key not in by_key]
    unknown = [
        step.step_key for step in steps
        if step.step_key in PROVIDER_STEPS and (
            step.last_error in OUTCOME_UNKNOWN_ERRORS
            or (step.status != "succeeded" and provider_attempt_may_have_started(step))
        )
    ]
    extra_step_count = sum(step.step_key not in REQUIRED_STEPS for step in steps)
    unrecognized_status_count = sum(
        step.step_key in REQUIRED_STEPS and step.status not in ALLOWED_STEP_STATUSES
        for step in steps
    )
    now = datetime.now(timezone.utc)
    receipt_report: dict[str, object] = {}
    receipt_errors: list[str] = []
    for key in PROVIDER_STEPS:
        step = by_key.get(key)
        if step is None or step.status != "succeeded":
            continue
        valid, structural, error = _receipt(key, step.provider_ref, expected_user_id, request_id)
        receipt_report[key] = {"status": step.status if step.status in ALLOWED_STEP_STATUSES else "unrecognized", **structural}
        if not valid and error:
            receipt_errors.append(f"{key}:{error}")
        if step.completed_at is None:
            receipt_errors.append(f"{key}:completion_timestamp_missing")
        if (not valid or step.completed_at is None
                or step.idempotency_key != f"account-deletion:{request_id}:{key}"):
            # A succeeded label with unusable evidence cannot authorize another
            # provider call, even when a legacy attempt counter is zero.
            if key not in unknown:
                unknown.append(key)
    unknown.sort()
    tombstone = by_key.get("database_tombstone")
    tombstone_valid = bool(
        tombstone is not None and tombstone.status == "succeeded" and tombstone.completed_at is not None
    )
    if request.status == "completed":
        tombstone_valid = tombstone_valid and user is not None and bool(
            user.is_ghost is True
            and isinstance(user.firebase_uid, str)
            and user.firebase_uid == f"deleted:{expected_user_id}"
            and isinstance(user.email, str)
            and user.email == f"deleted+{expected_user_id}@invalid.chirp"
        )
    blockers: list[str] = []
    if request.kind not in ALLOWED_REQUEST_KINDS:
        blockers.append("request_kind_not_deletion")
    if missing:
        blockers.append("journal_steps_missing")
    if unknown:
        blockers.append("provider_outcome_unknown")
    if receipt_errors:
        blockers.append("succeeded_receipt_invalid")
    if extra_step_count:
        blockers.append("unknown_journal_steps")
    if unrecognized_status_count:
        blockers.append("unrecognized_step_status")
    step_leases: dict[str, str] = {}
    for key in REQUIRED_STEPS:
        step = by_key.get(key)
        if step is not None and step.idempotency_key != f"account-deletion:{request_id}:{key}":
            blockers.append(f"{key}_idempotency_key_invalid")
        state = _lease_state(
            step.lease_token if step is not None else None,
            step.lease_expires_at if step is not None else None,
            now,
        )
        step_leases[key] = state
        if state != "absent":
            blockers.append(f"{key}_lease_{state}")
        if step is not None and step.status == "running" and state in {"absent", "expired", "malformed"}:
            blockers.append(f"{key}_outcome_unknown")
    lease_state = _lease_state(request.fulfillment_lease_token, request.fulfillment_lease_expires_at, now)
    if lease_state != "absent":
        blockers.append(f"request_lease_{lease_state}")
    if request.status not in ALLOWED_REQUEST_STATUSES:
        blockers.append("request_status_unrecognized")
    if request.status == "completed" and not tombstone_valid:
        blockers.append("tombstone_evidence_missing")
    complete_evidence = not blockers and request.status == "completed" and request.completed_at is not None and all(
        by_key[key].status == "succeeded" for key in REQUIRED_STEPS if key in by_key
    ) and tombstone_valid
    return {
        "status": "ok",
        "request_found": True,
        "request_kind": request.kind if request.kind in ALLOWED_REQUEST_KINDS else "unrecognized",
        "request_status": request.status if request.status in ALLOWED_REQUEST_STATUSES else "unrecognized",
        "request_failure_code": (
            request.failure_code if request.failure_code in SAFE_FAILURE_CODES else
            ("unrecognized" if request.failure_code else None)
        ),
        "request_created_at": _iso(request.created_at),
        "request_completed_at": _iso(request.completed_at),
        "request_lease": {"state": lease_state, "expires_at": _iso(request.fulfillment_lease_expires_at)},
        "expected_user_bound": True,
        "step_statuses": {
            key: (by_key[key].status if by_key[key].status in ALLOWED_STEP_STATUSES else "unrecognized")
            for key in REQUIRED_STEPS if key in by_key
        },
        "unknown_journal_step_count": extra_step_count,
        "unrecognized_step_status_count": unrecognized_status_count,
        "step_leases": step_leases,
        "missing_steps": missing,
        "unknown_outcome_steps": unknown,
        "receipt_validation": receipt_report,
        "receipt_errors": receipt_errors,
        "tombstone_evidence": tombstone_valid,
        "blockers": blockers,
        "durable_completion_evidence": complete_evidence,
        "ready_for_deletion": False,
    }


async def run_preflight(
    database_url: str,
    *,
    request_id: uuid.UUID,
    expected_user_id: uuid.UUID,
    timeout_seconds: float = 10.0,
) -> dict[str, object]:
    if database_url.startswith("postgresql://"):
        database_url = "postgresql+asyncpg://" + database_url[len("postgresql://"):]
    if not database_url or not database_url.startswith("postgresql+asyncpg://"):
        raise ValueError("database_url_must_be_postgresql")
    if not math.isfinite(timeout_seconds) or not 1 <= timeout_seconds <= 60:
        raise ValueError("timeout_seconds_invalid")
    async def _connected() -> dict[str, object]:
        engine = create_async_engine(
            database_url,
            pool_pre_ping=True,
            connect_args={"timeout": min(timeout_seconds, 10)},
        )
        try:
            async with engine.connect() as connection:
                transaction = await connection.begin()
                try:
                    await connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                    milliseconds = int(timeout_seconds * 1000)
                    await connection.execute(text(f"SET LOCAL statement_timeout = {milliseconds}"))
                    await connection.execute(text(f"SET LOCAL lock_timeout = {milliseconds}"))
                    await connection.execute(text(f"SET LOCAL idle_in_transaction_session_timeout = {milliseconds}"))
                    session = AsyncSession(bind=connection, expire_on_commit=False, autoflush=False)
                    try:
                        return await inspect_request(
                            session, request_id=request_id, expected_user_id=expected_user_id,
                        )
                    finally:
                        await session.close()
                finally:
                    await transaction.rollback()
        finally:
            await engine.dispose()

    return await asyncio.wait_for(_connected(), timeout=timeout_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = SafeParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--expected-user-id", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    args = parser.parse_args(argv)
    try:
        request_id = _parse_uuid(args.request_id, "request_id")
        expected_user_id = _parse_uuid(args.expected_user_id, "expected_user_id")
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise ValueError("DATABASE_URL_required")
        report = asyncio.run(run_preflight(
            database_url,
            request_id=request_id,
            expected_user_id=expected_user_id,
            timeout_seconds=args.timeout_seconds,
        ))
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}, sort_keys=True))
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0 if report.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
