"""Fail-closed, retry-safe account deletion orchestration.

Provider adapters are deliberately injected. Production callers must configure real
adapters and durable confirmation; the default adapter raises and cannot erase data.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models

PROVIDER_STEPS = ("firebase_auth", "payment_provider", "media_storage", "email_logs_backups")


class ProviderUnavailable(RuntimeError):
    pass


class DeletionProvider(Protocol):
    async def delete_account_data(self, *, user: models.User, idempotency_key: str) -> dict[str, str]: ...


class UnconfiguredProvider:
    async def delete_account_data(self, *, user: models.User, idempotency_key: str) -> dict[str, str]:
        raise ProviderUnavailable("provider_not_configured")


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
    steps = await prepare_deletion_plan(session, request)
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
        step.status = "running"
        step.attempt_count += 1
        step.started_at = _now()
        await session.commit()
        try:
            result = await provider.delete_account_data(user=user, idempotency_key=step.idempotency_key)
            if not isinstance(result, dict) or not result.get("confirmed"):
                raise ProviderUnavailable("provider_confirmation_missing")
        except Exception as exc:
            step.status = "manual_review" if isinstance(exc, ProviderUnavailable) else "failed"
            step.last_error = type(exc).__name__ if not isinstance(exc, ProviderUnavailable) else str(exc)
            request.status = "blocked"
            request.failure_code = "provider_step_incomplete"
            request.provider_steps = {s.step_key: s.status for s in steps}
            await session.commit()
            return DeletionRun("blocked", tuple(completed), step_key)
        step.status = "succeeded"
        step.provider_ref = {"confirmed": "true"}
        step.completed_at = _now()
        completed.append(step_key)
        request.provider_steps = {s.step_key: s.status for s in steps}
        await session.commit()
    # Keep shared organization, authored message, legal, safety, and append-only
    # financial rows. Remove provider linkage and direct identity credentials only.
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
