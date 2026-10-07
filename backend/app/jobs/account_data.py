"""Bounded maintenance for immutable account-export artifacts.

This module exposes a callable job only. No scheduler is enabled by this change;
callers must opt into mutation explicitly with ``apply=True``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models

MAX_ARTIFACTS_PER_RUN = 1_000


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
