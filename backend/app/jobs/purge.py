"""Bounded hard deletion of expired soft deletes; the CLI previews unless --apply is set."""
from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, exists, func, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Delete, Select

from app.config import get_settings
from app.models.chirp import Chirp, ChirpVote
from app.models.social import Post, PostComment, PostLike


@dataclass(frozen=True)
class PurgeResult:
    """Counts by deletion reason; total preserves the original helper's contract."""

    posts: int = 0
    post_comments: int = 0
    chirps: int = 0
    post_likes: int = 0
    cascaded_post_comments: int = 0
    chirp_votes: int = 0

    @property
    def total(self) -> int:
        """Legacy count: excludes likes, votes and comments removed with their post."""
        return self.posts + self.post_comments + self.chirps

    @property
    def physical_rows(self) -> int:
        return sum(asdict(self).values())

    def __add__(self, other: PurgeResult) -> PurgeResult:
        return PurgeResult(**{
            field.name: getattr(self, field.name) + getattr(other, field.name)
            for field in fields(self)
        })


def _positive(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _cutoff(now: datetime | None, retention_days: int | None) -> tuple[datetime, int]:
    days = _positive(
        retention_days if retention_days is not None else get_settings().purge_retention_days,
        "retention_days",
    )
    instant = now if now is not None else datetime.now(timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("now must include a timezone")
    try:
        return instant.astimezone(timezone.utc) - timedelta(days=days), days
    except OverflowError as exc:
        raise ValueError("retention_days is outside the supported date range") from exc


def _expired_posts(cutoff: datetime) -> Select:
    return select(Post.id).where(Post.deleted_at < cutoff)


def _expired_chirps(cutoff: datetime) -> Select:
    return select(Chirp.id).where(Chirp.removed_at < cutoff)


def _independent_comments(cutoff: datetime) -> Select:
    # Expired parents own their comments' cleanup, including recently deleted/live
    # comments. Keep the legacy independent-comment count disjoint from that cascade.
    return select(PostComment.id).where(
        PostComment.deleted_at < cutoff,
        ~exists(select(Post.id).where(
            Post.id == PostComment.post_id, Post.deleted_at < cutoff,
        )),
    )


async def _delete_count(session: AsyncSession, statement: Delete) -> int:
    result = await session.execute(statement)
    return result.rowcount or 0


async def purge_batch(
    session: AsyncSession, *, cutoff: datetime, batch_size: int = 100,
) -> PurgeResult:
    """Delete at most batch_size rows per reason (six reasons); caller commits.

    Parent UPDATE locks conflict with FK KEY SHARE locks, so a concurrent child
    insert cannot slip between child cleanup and parent deletion. SKIP LOCKED
    leaves occupied parents for a later execution. Large parents retain their
    remaining children and are revisited oldest-first in the next batch.
    No schema cascade is assumed. Historical polymorphic content_reports remain.
    """
    _positive(batch_size, "batch_size")
    post_ids = list((await session.scalars(
        _expired_posts(cutoff).order_by(Post.deleted_at, Post.id)
        .limit(batch_size).with_for_update(skip_locked=True)
    )).all())
    likes = cascaded_comments = posts = 0
    if post_ids:
        likes = await _delete_count(session, delete(PostLike).where(
            tuple_(PostLike.post_id, PostLike.user_id).in_(
                select(PostLike.post_id, PostLike.user_id)
                .where(PostLike.post_id.in_(post_ids))
                .order_by(PostLike.post_id, PostLike.user_id).limit(batch_size)
            )
        ).execution_options(synchronize_session=False))
        cascaded_comments = await _delete_count(session, delete(PostComment).where(
            PostComment.id.in_(select(PostComment.id)
                .where(PostComment.post_id.in_(post_ids))
                .order_by(PostComment.post_id, PostComment.id).limit(batch_size))
        ).execution_options(synchronize_session=False))
        posts = await _delete_count(session, delete(Post).where(
            Post.id.in_(post_ids),
            ~exists(select(PostLike.post_id).where(PostLike.post_id == Post.id)),
            ~exists(select(PostComment.id).where(PostComment.post_id == Post.id)),
        ).execution_options(synchronize_session=False))

    comments = await _delete_count(session, delete(PostComment).where(
        PostComment.id.in_(
            _independent_comments(cutoff).order_by(PostComment.deleted_at, PostComment.id)
            .limit(batch_size).with_for_update(skip_locked=True)
        )
    ).execution_options(synchronize_session=False))

    chirp_ids = list((await session.scalars(
        _expired_chirps(cutoff).order_by(Chirp.removed_at, Chirp.id)
        .limit(batch_size).with_for_update(skip_locked=True)
    )).all())
    votes = chirps = 0
    if chirp_ids:
        # The existing vote trigger also updates chirps.score. The job identity
        # needs UPDATE(score), and counts below describe deletes, not trigger updates.
        votes = await _delete_count(session, delete(ChirpVote).where(
            tuple_(ChirpVote.chirp_id, ChirpVote.user_id).in_(
                select(ChirpVote.chirp_id, ChirpVote.user_id)
                .where(ChirpVote.chirp_id.in_(chirp_ids))
                .order_by(ChirpVote.chirp_id, ChirpVote.user_id).limit(batch_size)
            )
        ).execution_options(synchronize_session=False))
        chirps = await _delete_count(session, delete(Chirp).where(
            Chirp.id.in_(chirp_ids),
            ~exists(select(ChirpVote.chirp_id).where(ChirpVote.chirp_id == Chirp.id)),
        ).execution_options(synchronize_session=False))
    return PurgeResult(posts, comments, chirps, likes, cascaded_comments, votes)


async def purge_expired_soft_deletes(
    session: AsyncSession, *, now: datetime | None = None, retention_days: int | None = None,
) -> PurgeResult:
    """Preserve the full-run helper and caller-owned transaction (including rollback).

    Uses bounded statements but has no execution budget; use the CLI for scheduled
    work with per-batch commits and a finite run budget. Locked rows are left for
    the next call. Repeated calls are idempotent; exact cutoff timestamps survive.
    """
    cutoff, _ = _cutoff(now, retention_days)
    total = PurgeResult()
    while True:
        batch = await purge_batch(session, cutoff=cutoff)
        total += batch
        if batch.physical_rows == 0:
            return total


async def preview_expired_soft_deletes(
    session: AsyncSession, *, cutoff: datetime, count_limit: int,
) -> tuple[PurgeResult, list[str]]:
    """SELECT-only aggregate preview; capped counts are lower bounds, never exact.

    Each subquery returns at most count_limit + 1 rows to COUNT. PostgreSQL may
    still scan to locate them; the CLI additionally sets a statement timeout.
    The caller must supply a fresh read-only transaction for database enforcement.
    """
    _positive(count_limit, "count_limit")
    queries = {
        "posts": _expired_posts(cutoff),
        "post_comments": _independent_comments(cutoff),
        "chirps": _expired_chirps(cutoff),
        "post_likes": select(PostLike.post_id).where(
            PostLike.post_id.in_(_expired_posts(cutoff))),
        "cascaded_post_comments": select(PostComment.id).where(
            PostComment.post_id.in_(_expired_posts(cutoff))),
        "chirp_votes": select(ChirpVote.chirp_id).where(
            ChirpVote.chirp_id.in_(_expired_chirps(cutoff))),
    }
    counts: dict[str, int] = {}
    capped: list[str] = []
    for name, query in queries.items():
        observed = int(await session.scalar(select(func.count()).select_from(
            query.limit(count_limit + 1).subquery()
        )) or 0)
        counts[name] = min(observed, count_limit)
        if observed > count_limit:
            capped.append(name)
    return PurgeResult(**counts), capped


async def _has_remaining(session: AsyncSession, cutoff: datetime) -> bool:
    return bool(await session.scalar(select(
        exists(_expired_posts(cutoff))
        | exists(select(PostComment.id).where(PostComment.deleted_at < cutoff))
        | exists(_expired_chirps(cutoff))
    )))


async def _transaction_limits(session: AsyncSession, remaining_seconds: float) -> None:
    # Values are validated/generated integers, not user-provided SQL strings.
    milliseconds = max(1, min(30_000, int(remaining_seconds * 1000)))
    await session.execute(text(f"SET LOCAL statement_timeout = {milliseconds}"))
    await session.execute(text(f"SET LOCAL lock_timeout = {min(2000, milliseconds)}"))


async def run_purge_job(
    *, apply: bool = False, retention_days: int | None = None, batch_size: int = 100,
    max_batches: int = 10, max_seconds: int = 120, now: datetime | None = None,
) -> dict[str, object]:
    """Run one finite job, reporting only aggregate counts and acknowledged commits."""
    cutoff, days = _cutoff(now, retention_days)
    for name, value in (("batch_size", batch_size), ("max_batches", max_batches),
                        ("max_seconds", max_seconds)):
        _positive(value, name)
    from app.db import get_session_factory

    total = PurgeResult()
    report: dict[str, object] = {
        "mode": "apply" if apply else "dry_run", "cutoff": cutoff.isoformat(),
        "retention_days": days, "batch_size": batch_size, "max_batches": max_batches,
        "max_seconds": max_seconds, "batches_committed": 0, "status": "incomplete",
        "remaining": None, "capped_counts": [], "commit_outcome_unknown": False,
    }
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_seconds
    committing = False
    committed_batches = 0
    try:
        async with asyncio.timeout(max_seconds):
            async with get_session_factory()() as session:
                if not apply:
                    # First statement: enforce no DML even if a future edit regresses
                    # the preview. Repeatable read gives all six counts one snapshot.
                    await session.execute(text(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                    ))
                    await _transaction_limits(session, deadline - loop.time())
                    total, capped = await preview_expired_soft_deletes(
                        session, cutoff=cutoff, count_limit=batch_size * max_batches,
                    )
                    report.update(status="preview", capped_counts=capped,
                                  remaining=total.physical_rows > 0)
                    await session.rollback()
                else:
                    for _ in range(max_batches):
                        await _transaction_limits(session, deadline - loop.time())
                        batch = await purge_batch(session, cutoff=cutoff, batch_size=batch_size)
                        committing = True
                        await session.commit()
                        committing = False
                        total += batch
                        committed_batches += 1
                        report["batches_committed"] = committed_batches
                        await _transaction_limits(session, deadline - loop.time())
                        remaining = await _has_remaining(session, cutoff)
                        await session.rollback()
                        report["remaining"] = remaining
                        if not remaining:
                            report["status"] = "complete"
                            break
                        if batch.physical_rows == 0:
                            # SKIP LOCKED can return no progress despite a backlog.
                            report["status"] = "blocked"
                            break
    except TimeoutError:
        report.update(status="timed_out", remaining=None, commit_outcome_unknown=committing)
    except Exception as exc:
        # Database errors can contain SQL parameters and connection details. Do not
        # emit raw exceptions or tracebacks into the job log. Preserve prior commits.
        report.update(status="failed", remaining=None, commit_outcome_unknown=committing,
                      error_type=type(exc).__name__)
    # Only acknowledged commits are counted on apply. A deadline during COMMIT may
    # have reached the server; report that uncertainty instead of asserting rollback.
    report["counts"] = asdict(total)
    report["physical_rows"] = total.physical_rows
    return report


def _positive_arg(value: str) -> int:
    try:
        return _positive(int(value), "value")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc


def main(argv: Sequence[str] | None = None) -> None:
    """Preview by default; explicit --apply enables bounded deletion and commits."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="preview only (default)")
    mode.add_argument("--apply", action="store_true", help="hard-delete eligible rows")
    parser.add_argument("--retention-days", type=_positive_arg, default=None)
    parser.add_argument("--batch-size", type=_positive_arg, default=100)
    parser.add_argument("--max-batches", type=_positive_arg, default=10)
    parser.add_argument("--max-seconds", type=_positive_arg, default=120)
    args = parser.parse_args(argv)
    # Resolve even the environment-sourced retention before constructing an engine.
    try:
        _, days = _cutoff(None, args.retention_days)
    except ValueError as exc:
        parser.error(str(exc))
    result = asyncio.run(run_purge_job(
        apply=args.apply, retention_days=days, batch_size=args.batch_size,
        max_batches=args.max_batches, max_seconds=args.max_seconds,
    ))
    print(json.dumps(result, sort_keys=True))
    if result["status"] not in ("preview", "complete"):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
