"""Fail-closed, retry-safe account deletion orchestration.

Provider adapters are deliberately injected. Production callers must configure real
adapters and durable confirmation; the default adapter raises and cannot erase data.
"""
from __future__ import annotations

import uuid
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.services.account_cleanup import cleanup_local_account

PROVIDER_STEPS = ("payment_provider", "media_storage", "email_logs_backups", "analytics", "firebase_auth")
SAFE_PROVIDER_ERRORS = frozenset({
    "provider_not_configured", "provider_confirmation_missing", "provider_error",
    "firebase_delete_failed", "firebase_readback_failed", "firebase_delete_not_confirmed",
})
PROVIDER_RETENTION_REASONS = {
    "stripe_deleted_customer_history": "Stripe's deleted-customer history remains subject to provider retention; expiry is not measured",
    "gcs_soft_deleted_versions": "Object-storage soft-deleted versions remain subject to provider retention; expiry is not measured",
    "email_delivery_records": "Email-provider delivery records remain subject to provider retention; expiry is not measured",
    "operational_logs": "Operational logs remain subject to provider retention; expiry is not measured",
    "database_backups": "Database backups remain subject to provider retention; expiry is not measured",
    "bigquery_time_travel": "BigQuery time-travel retention remains provider controlled; expiry is not measured",
    "bigquery_fail_safe": "BigQuery fail-safe retention remains provider controlled; expiry is not measured",
}
PROVIDER_RETENTION_CODES = {
    "payment_provider": frozenset({"stripe_deleted_customer_history"}),
    "media_storage": frozenset({"gcs_soft_deleted_versions"}),
    "email_logs_backups": frozenset({"email_delivery_records", "operational_logs", "database_backups"}),
    "firebase_auth": frozenset(),
    "analytics": frozenset({"bigquery_time_travel", "bigquery_fail_safe"}),
}
MAX_RETENTION_RECEIPT_CODES = 3
MAX_RECEIPT_SCOPES = 16
MAX_RECEIPT_SCOPE_LENGTH = 512
FIREBASE_CALL_TIMEOUT_SECONDS = 30.0


class ProviderUnavailable(RuntimeError):
    pass


class DeletionProvider(Protocol):
    async def delete_account_data(self, *, user: models.User, idempotency_key: str) -> dict[str, object]: ...


class UnconfiguredProvider:
    async def delete_account_data(self, *, user: models.User, idempotency_key: str) -> dict[str, str]:
        raise ProviderUnavailable("provider_not_configured")


class FirebaseProvider:
    """Firebase Auth deletion with readback; never treats a malformed response as success."""
    async def delete_account_data(self, *, user: models.User, idempotency_key: str) -> dict[str, object]:
        from firebase_admin import auth

        async def drain(worker: asyncio.Task[object]) -> None:
            """Wait for a cancelled SDK thread before releasing its deletion lease."""
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if worker.done() and not worker.cancelled():
                try:
                    worker.result()
                except Exception:
                    pass

        async def call(operation, *args):
            worker = asyncio.create_task(asyncio.to_thread(operation, *args))
            try:
                return await asyncio.wait_for(
                    asyncio.shield(worker), timeout=FIREBASE_CALL_TIMEOUT_SECONDS
                )
            except (asyncio.TimeoutError, asyncio.CancelledError) as original:
                await drain(worker)
                if isinstance(original, asyncio.CancelledError):
                    raise
                raise ProviderUnavailable("firebase_delete_failed") from None
        try:
            await call(auth.delete_user, user.firebase_uid)
        except auth.UserNotFoundError:
            pass
        except Exception:
            raise ProviderUnavailable("firebase_delete_failed") from None
        try:
            await call(auth.get_user, user.firebase_uid)
        except auth.UserNotFoundError:
            return {
                "confirmed": True,
                "provider": "firebase_auth",
                "subject": str(user.id),
                "scope": ["firebase_auth_user"],
                "readback": True,
            }
        except Exception:
            raise ProviderUnavailable("firebase_readback_failed") from None
        raise ProviderUnavailable("firebase_delete_not_confirmed")


@dataclass(frozen=True)
class DeletionRun:
    status: str
    completed_steps: tuple[str, ...]
    failed_step: str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _key(request_id: uuid.UUID, step: str) -> str:
    return f"account-deletion:{request_id}:{step}"


def _retention_receipt(step_key: str, result: dict[str, object]) -> list[str]:
    """Validate optional fixed retention acknowledgments from a provider."""
    if not isinstance(result, dict):
        raise ProviderUnavailable("provider_confirmation_missing")
    raw = result.get("retained_with_reason", [])
    if not isinstance(raw, list) or len(raw) > MAX_RETENTION_RECEIPT_CODES:
        raise ProviderUnavailable("provider_confirmation_missing")
    allowed = PROVIDER_RETENTION_CODES.get(step_key, frozenset())
    if any(not isinstance(code, str) or code not in allowed for code in raw):
        raise ProviderUnavailable("provider_confirmation_missing")
    if len(set(raw)) != len(raw):
        raise ProviderUnavailable("provider_confirmation_missing")
    return list(raw)


def validate_provider_receipt(step_key: str, subject: uuid.UUID, result: object) -> list[str]:
    """Validate fresh and durable confirmation against the exact account and step.

    Scope labels are descriptive adapter output, not independent proof of a
    remote operation. Actual provider readback remains the adapter's contract.
    """
    if (
        step_key not in PROVIDER_STEPS
        or not isinstance(result, dict)
        or result.get("confirmed") is not True
        or result.get("provider") != step_key
        or result.get("subject") != str(subject)
        or result.get("readback") is not True
    ):
        raise ProviderUnavailable("provider_confirmation_missing")
    if step_key == "analytics":
        if (type(result.get("time_travel_hours")) is not int
                or not 48 <= result["time_travel_hours"] <= 168
                or result.get("fail_safe_days") != 7):
            raise ProviderUnavailable("provider_confirmation_missing")
        excluded = result.get("excluded_scope")
        if (not isinstance(excluded, list) or len(excluded) > MAX_RECEIPT_SCOPES
                or any(not isinstance(item, str) or not item.strip() or len(item) > MAX_RECEIPT_SCOPE_LENGTH for item in excluded)):
            raise ProviderUnavailable("provider_confirmation_missing")
    scope = result.get("scope")
    if (
        not isinstance(scope, list)
        or not 1 <= len(scope) <= MAX_RECEIPT_SCOPES
        or any(not isinstance(item, str) or not item.strip()
               or len(item) > MAX_RECEIPT_SCOPE_LENGTH for item in scope)
        or len(set(scope)) != len(scope)
    ):
        raise ProviderUnavailable("provider_confirmation_missing")
    return _retention_receipt(step_key, result)


async def prepare_deletion_plan(session: AsyncSession, request: models.AccountDataRequest) -> list[models.AccountFulfillmentStep]:
    """Create the complete provider plan before any irreversible work."""
    if request.kind != "deletion":
        raise ValueError("fulfillment_plan_requires_deletion")
    existing = list((await session.scalars(
        select(models.AccountFulfillmentStep).where(models.AccountFulfillmentStep.request_id == request.id).order_by(models.AccountFulfillmentStep.step_key)
    )).all())
    by_key = {step.step_key: step for step in existing}
    steps = existing[:]
    for step_key in (*PROVIDER_STEPS, "database_tombstone"):
        if step_key not in by_key:
            step = models.AccountFulfillmentStep(
                request_id=request.id, step_key=step_key, idempotency_key=_key(request.id, step_key),
            )
            session.add(step)
            steps.append(step)
    current_steps = dict(request.provider_steps or {})
    for step_key in (*PROVIDER_STEPS, "database_tombstone"):
        current_steps.setdefault(step_key, by_key.get(step_key).status if by_key.get(step_key) else "pending")
    request.provider_steps = current_steps
    request.status = "processing"
    request.failure_code = None
    request.updated_at = _now()
    await session.flush()
    return sorted(steps, key=lambda step: step.step_key)


async def run_deletion(
    session: AsyncSession,
    request_id: uuid.UUID,
    user: models.User,
    providers: dict[str, DeletionProvider],
) -> DeletionRun:
    """Run injected provider steps, then retain shared rows and tombstone identity.

    Each provider step is committed before and after its external call. A missing
    adapter, exception, or ambiguous result leaves the account and remaining steps
    intact for retry/manual review.
    """
    request = await session.get(models.AccountDataRequest, request_id, with_for_update=True)
    if request is None or request.user_id != user.id or request.kind != "deletion":
        raise LookupError("deletion_request_not_found")
    if request.status == "completed":
        durable = list((await session.scalars(select(models.AccountFulfillmentStep).where(
            models.AccountFulfillmentStep.request_id == request.id))).all())
        required = set((*PROVIDER_STEPS, "database_tombstone"))
        if {step.step_key for step in durable} != required:
            # A historical terminal request must remain an immutable record. Do
            # not grow a new plan or retry providers against its tombstoned user.
            await session.rollback()
            return DeletionRun("blocked", (), "analytics")
        tombstone = next(step for step in durable if step.step_key == "database_tombstone")
        valid_terminal = (tombstone.status == "succeeded" and tombstone.completed_at is not None)
        for step in durable:
            if step.step_key == "database_tombstone":
                continue
            try:
                validate_provider_receipt(step.step_key, user.id, step.provider_ref)
                valid_terminal = valid_terminal and step.status == "succeeded" and step.completed_at is not None
            except ProviderUnavailable:
                valid_terminal = False
        if not valid_terminal:
            await session.rollback()
            return DeletionRun("blocked", (), "analytics")
        return DeletionRun("completed", tuple(PROVIDER_STEPS) + ("database_tombstone",))
    if request.status in {"canceled", "failed"}:
        return DeletionRun("blocked", (), None)
    steps = await prepare_deletion_plan(session, request)
    missing = [key for key in PROVIDER_STEPS if key not in providers or isinstance(providers[key], UnconfiguredProvider)]
    if missing:
        request.status = "blocked"
        request.failure_code = "provider_not_configured"
        request.provider_steps = {step.step_key: step.status for step in steps}
        await session.commit()
        return DeletionRun("blocked", (), missing[0])
    request_lease = uuid.uuid4()
    lease_until = _now() + timedelta(minutes=5)
    claimed_request = await session.execute(update(models.AccountDataRequest).where(
        models.AccountDataRequest.id == request.id,
        models.AccountDataRequest.status == "processing",
        or_(models.AccountDataRequest.fulfillment_lease_token.is_(None),
            models.AccountDataRequest.fulfillment_lease_expires_at < _now()),
    ).values(fulfillment_lease_token=request_lease, fulfillment_lease_expires_at=lease_until))
    if claimed_request.rowcount != 1:
        await session.rollback()
        return DeletionRun("blocked", (), None)
    await session.commit()
    completed: list[str] = []
    for step_key in PROVIDER_STEPS:
        request_fence = await session.execute(update(models.AccountDataRequest).where(
            models.AccountDataRequest.id == request.id,
            models.AccountDataRequest.status == "processing",
            models.AccountDataRequest.fulfillment_lease_token == request_lease,
            models.AccountDataRequest.fulfillment_lease_expires_at > _now(),
        ).values(updated_at=_now()))
        if request_fence.rowcount != 1:
            await session.rollback()
            return DeletionRun("blocked", tuple(completed), step_key)
        step = (await session.scalars(select(models.AccountFulfillmentStep).where(
            models.AccountFulfillmentStep.request_id == request.id,
            models.AccountFulfillmentStep.step_key == step_key,
        ).with_for_update())).one()
        if step.status == "succeeded":
            # A legacy or manually altered receipt must not silently authorize
            # continuation. Validate it before invoking any later provider.
            try:
                validate_provider_receipt(step_key, user.id, step.provider_ref)
            except ProviderUnavailable:
                step.status = "manual_review"
                step.last_error = "provider_confirmation_missing"
                provider_steps = dict(request.provider_steps or {})
                provider_steps[step_key] = "manual_review"
                request.status = "blocked"
                request.failure_code = "provider_step_incomplete"
                request.provider_steps = provider_steps
                request.fulfillment_lease_token = None
                request.fulfillment_lease_expires_at = None
                await session.commit()
                return DeletionRun("blocked", tuple(completed), step_key)
            completed.append(step_key)
            continue
        if step_key == "firebase_auth" and (
            step.status == "running"
            or (step.status == "manual_review" and step.last_error == "firebase_outcome_unknown")
        ):
            # A previous Firebase worker may still be finishing after its
            # request and step leases expired. Do not invoke Firebase again;
            # persist the uncertain outcome before releasing this runner.
            step.status = "manual_review"
            step.last_error = "firebase_outcome_unknown"
            provider_steps = dict(request.provider_steps or {})
            provider_steps[step_key] = "manual_review"
            request.status = "blocked"
            request.failure_code = "provider_step_incomplete"
            request.provider_steps = provider_steps
            request.fulfillment_lease_token = None
            request.fulfillment_lease_expires_at = None
            step.lease_token = None
            step.lease_expires_at = None
            await session.commit()
            return DeletionRun("blocked", tuple(completed), step_key)
        provider = providers.get(step_key)
        if provider is None:
            step.status = "manual_review"
            step.last_error = "provider_not_configured"
            request.status = "blocked"
            request.failure_code = "provider_not_configured"
            request.provider_steps = {s.step_key: s.status for s in steps}
            await session.commit()
            return DeletionRun("blocked", tuple(completed), step_key)
        token = uuid.uuid4()
        lease_until = _now() + timedelta(minutes=5)
        claimed = await session.execute(update(models.AccountFulfillmentStep).where(
            models.AccountFulfillmentStep.id == step.id,
            or_(models.AccountFulfillmentStep.status.in_(["pending", "failed", "manual_review"]),
                and_(models.AccountFulfillmentStep.status == "running", models.AccountFulfillmentStep.lease_expires_at < _now())),
        ).values(status="running", lease_token=token, lease_expires_at=lease_until,
                 attempt_count=models.AccountFulfillmentStep.attempt_count + 1,
                 started_at=_now()))
        if claimed.rowcount != 1:
            return DeletionRun("blocked", tuple(completed), step_key)
        await session.refresh(step)
        await session.commit()
        try:
            result = await provider.delete_account_data(user=user, idempotency_key=step.idempotency_key)
            retained_with_reason = validate_provider_receipt(step_key, user.id, result)
            if step_key == "analytics" and (
                    type(result.get("time_travel_hours")) is not int
                    or type(result.get("fail_safe_days")) is not int
                    or not isinstance(result.get("excluded_scope"), list)
                    or any(not isinstance(v, str) or len(v) > MAX_RECEIPT_SCOPE_LENGTH for v in result["excluded_scope"])):
                raise ProviderUnavailable("provider_confirmation_missing")
        except Exception as exc:
            code = str(exc) if isinstance(exc, ProviderUnavailable) else "provider_error"
            step_status = "manual_review" if isinstance(exc, ProviderUnavailable) else "failed"
            safe_error = code if code in SAFE_PROVIDER_ERRORS else "provider_error"
            failed_step = await session.execute(update(models.AccountFulfillmentStep).where(
                models.AccountFulfillmentStep.id == step.id,
                models.AccountFulfillmentStep.status == "running",
                models.AccountFulfillmentStep.lease_token == token,
                models.AccountFulfillmentStep.lease_expires_at > _now(),
            ).values(status=step_status, last_error=safe_error, lease_token=None, lease_expires_at=None))
            # Publish the step outcome only in the same lease-fenced transaction
            # as the request transition.  A stale runner must roll back both
            # writes, otherwise status can claim ``pending`` while the journal
            # is already in manual review.
            provider_steps = dict(request.provider_steps or {})
            provider_steps[step_key] = step_status
            failed_request = await session.execute(update(models.AccountDataRequest).where(
                models.AccountDataRequest.id == request.id,
                models.AccountDataRequest.status == "processing",
                models.AccountDataRequest.fulfillment_lease_token == request_lease,
                models.AccountDataRequest.fulfillment_lease_expires_at > _now(),
            ).values(status="blocked", failure_code="provider_step_incomplete",
                     provider_steps=provider_steps,
                     fulfillment_lease_token=None, fulfillment_lease_expires_at=None,
                     updated_at=_now()))
            if failed_step.rowcount != 1 or failed_request.rowcount != 1:
                await session.rollback()
                return DeletionRun("blocked", tuple(completed), step_key)
            await session.commit()
            return DeletionRun("blocked", tuple(completed), step_key)
        provider_ref = {"confirmed": True, "provider": result["provider"],
                        "subject": result["subject"], "scope": result["scope"],
                        "readback": True, "retained_with_reason": retained_with_reason}
        if step_key == "analytics":
            provider_ref.update({"time_travel_hours": result["time_travel_hours"],
                                 "fail_safe_days": result["fail_safe_days"],
                                 "excluded_scope": result["excluded_scope"]})
        fenced = await session.execute(update(models.AccountFulfillmentStep).where(
            models.AccountFulfillmentStep.id == step.id,
            models.AccountFulfillmentStep.status == "running",
            models.AccountFulfillmentStep.lease_token == token,
            models.AccountFulfillmentStep.lease_expires_at > _now(),
        ).values(status="succeeded", lease_token=None, lease_expires_at=None,
                 provider_ref=provider_ref, completed_at=_now()))
        if fenced.rowcount != 1:
            await session.rollback()
            return DeletionRun("blocked", tuple(completed), step_key)
        completed.append(step_key)
        request.provider_steps = {s.step_key: s.status for s in steps}
        await session.commit()
    # Reacquire the request fence before local mutation. A concurrent runner or
    # stale lease cannot clean up after the request has become terminal.
    request = (await session.scalars(select(models.AccountDataRequest).where(
        models.AccountDataRequest.id == request_id,
    ).execution_options(populate_existing=True).with_for_update())).one_or_none()
    if request is None or request.status != "processing" or request.user_id != user.id or request.fulfillment_lease_token != request_lease or request.fulfillment_lease_expires_at is None or request.fulfillment_lease_expires_at <= _now():
        return DeletionRun("blocked", tuple(completed), None)
    durable_steps = list((await session.scalars(select(models.AccountFulfillmentStep).where(
        models.AccountFulfillmentStep.request_id == request.id,
    ).order_by(models.AccountFulfillmentStep.step_key).execution_options(populate_existing=True).with_for_update())).all())
    if {step.step_key for step in durable_steps} != set((*PROVIDER_STEPS, "database_tombstone")):
        request.status = "blocked"
        request.failure_code = "provider_step_incomplete"
        request.fulfillment_lease_token = None
        request.fulfillment_lease_expires_at = None
        await session.commit()
        return DeletionRun("blocked", tuple(completed), None)
    provider_retention_codes: dict[str, list[str]] = {}
    for durable_step in durable_steps:
        if durable_step.step_key == "database_tombstone":
            continue
        try:
            if durable_step.status != "succeeded":
                raise ProviderUnavailable("provider_confirmation_missing")
            provider_retention_codes[durable_step.step_key] = validate_provider_receipt(
                durable_step.step_key, user.id, durable_step.provider_ref,
            )
        except ProviderUnavailable:
            request.status = "blocked"
            request.failure_code = "provider_step_incomplete"
            request.fulfillment_lease_token = None
            request.fulfillment_lease_expires_at = None
            await session.commit()
            return DeletionRun("blocked", tuple(completed), durable_step.step_key)
    # Apply the approved local cleanup boundary; shared/history rows remain.
    cleanup = await cleanup_local_account(session, user.id)
    retention_reasons = dict(cleanup["retained"])
    for retained_with_reason in provider_retention_codes.values():
        for code in retained_with_reason:
            retention_reasons[code] = PROVIDER_RETENTION_REASONS[code]
    request.retention_reasons = list(retention_reasons.values())
    await session.flush()
    # Keep shared organization, authored encrypted message, legal, safety, and
    # append-only financial rows. Remove direct identity credentials only.
    user.firebase_uid = f"deleted:{user.id}"
    user.email = f"deleted+{user.id}@invalid.chirp"
    user.display_name = "Deleted account"
    user.avatar_url = None
    user.campus_id = None
    user.pseudonym_seed = str(uuid.uuid4())
    user.campus_verified_at = None
    user.is_platform_admin = False
    user.suspended_at = None
    user.suspension_reason = None
    user.suspended_by = None
    user.is_ghost = True
    tombstone = (await session.scalars(select(models.AccountFulfillmentStep).where(
        models.AccountFulfillmentStep.request_id == request.id,
        models.AccountFulfillmentStep.step_key == "database_tombstone",
    ).with_for_update())).one()
    tombstone.status = "succeeded"
    tombstone.attempt_count += 1
    tombstone.started_at = tombstone.started_at or _now()
    tombstone.completed_at = _now()
    request.status = "completed"
    request.failure_code = None
    request.provider_steps = {s.step_key: "succeeded" for s in steps}
    request.completed_at = _now()
    request.updated_at = request.completed_at
    request.open_key = None
    request.fulfillment_lease_token = None
    request.fulfillment_lease_expires_at = None
    await session.commit()
    return DeletionRun("completed", tuple(completed) + ("database_tombstone",))
