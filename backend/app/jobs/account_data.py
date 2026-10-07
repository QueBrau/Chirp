"""Bounded maintenance for immutable account-export artifacts.

This module exposes a callable job only. No scheduler is enabled by this change;
callers must opt into mutation explicitly with ``apply=True``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
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


async def _run_cli(*, delete_rows: bool, max_rows: int, timeout_seconds: int) -> dict[str, object]:
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        async with asyncio.timeout(timeout_seconds):
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
    parser.add_argument("--max-rows", type=_positive_arg, default=100,
                        help=f"maximum artifacts (cap {MAX_ARTIFACTS_PER_RUN})")
    parser.add_argument("--timeout-seconds", type=_positive_arg, default=30)
    args = parser.parse_args(argv)
    if args.max_rows > MAX_ARTIFACTS_PER_RUN:
        parser.error(f"max-rows cannot exceed {MAX_ARTIFACTS_PER_RUN}")
    try:
        report = asyncio.run(_run_cli(
            delete_rows=args.delete,
            max_rows=args.max_rows,
            timeout_seconds=args.timeout_seconds,
        ))
    except TimeoutError:
        report = {"mode": "apply" if args.delete else "dry_run", "status": "timed_out"}
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
