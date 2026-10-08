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

PROVIDER_STEPS = ("payment_provider", "media_storage", "email_logs_backups", "firebase_auth")
SAFE_PROVIDER_ERRORS = frozenset({
    "provider_not_configured", "provider_confirmation_missing", "provider_error",
    "firebase_delete_failed", "firebase_readback_failed", "firebase_delete_not_confirmed",
})


class ProviderUnavailable(RuntimeError):
    pass


class DeletionProvider(Protocol):
    async def delete_account_data(self, *, user: models.User, idempotency_key: str) -> dict[str, str]: ...


class UnconfiguredProvider:
    async def delete_account_data(self, *, user: models.User, idempotency_key: str) -> dict[str, str]:
        raise ProviderUnavailable("provider_not_configured")


class FirebaseProvider:
    """Firebase Auth deletion with readback; never treats a malformed response as success."""
    async def delete_account_data(self, *, user: models.User, idempotency_key: str) -> dict[str, bool]:
        from firebase_admin import auth
        try:
            await asyncio.to_thread(auth.delete_user, user.firebase_uid)
        except Exception as exc:
            if type(exc).__name__ not in {"UserNotFoundError"}:
                raise ProviderUnavailable("firebase_delete_failed") from None
        try:
            await asyncio.to_thread(auth.get_user, user.firebase_uid)
        except Exception as exc:
            if type(exc).__name__ == "UserNotFoundError":
                return {"confirmed": True}
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
    if not request.provider_steps:
        request.provider_steps = {step_key: "pending" for step_key in (*PROVIDER_STEPS, "database_tombstone")}
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
    await session.commit()
    completed: list[str] = []
    for step_key in PROVIDER_STEPS:
        step = (await session.scalars(select(models.AccountFulfillmentStep).where(
            models.AccountFulfillmentStep.request_id == request.id,
            models.AccountFulfillmentStep.step_key == step_key,
        ).with_for_update())).one()
        if step.status == "succeeded":
            completed.append(step_key)
            continue
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
            if not isinstance(result, dict) or result.get("confirmed") is not True:
                raise ProviderUnavailable("provider_confirmation_missing")
        except Exception as exc:
            step.status = "manual_review" if isinstance(exc, ProviderUnavailable) else "failed"
            code = str(exc) if isinstance(exc, ProviderUnavailable) else "provider_error"
            step.last_error = code if code in SAFE_PROVIDER_ERRORS else "provider_error"
            request.status = "blocked"
            request.failure_code = "provider_step_incomplete"
            request.provider_steps = {s.step_key: s.status for s in steps}
            await session.commit()
            return DeletionRun("blocked", tuple(completed), step_key)
        fenced = await session.execute(update(models.AccountFulfillmentStep).where(
            models.AccountFulfillmentStep.id == step.id,
            models.AccountFulfillmentStep.status == "running",
            models.AccountFulfillmentStep.lease_token == token,
            models.AccountFulfillmentStep.lease_expires_at > _now(),
        ).values(status="succeeded", lease_token=None, lease_expires_at=None,
                 provider_ref={"confirmed": True}, completed_at=_now()))
        if fenced.rowcount != 1:
            await session.rollback()
            return DeletionRun("blocked", tuple(completed), step_key)
        completed.append(step_key)
        request.provider_steps = {s.step_key: s.status for s in steps}
        await session.commit()
    # Apply the approved local cleanup boundary; shared/history rows remain.
    cleanup = await cleanup_local_account(session, user.id)
    request.retention_reasons = list(cleanup["retained"].values())
    await session.flush()
    # Keep shared organization, authored encrypted message, legal, safety, and
    # append-only financial rows. Remove direct identity credentials only.
    user.firebase_uid = f"deleted:{user.id}"
    user.email = f"deleted+{user.id}@invalid.chirp"
    user.display_name = "Deleted account"
    user.avatar_url = None
    user.campus_id = None
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
    await session.commit()
    return DeletionRun("completed", tuple(completed) + ("database_tombstone",))
