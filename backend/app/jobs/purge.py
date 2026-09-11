"""Bounded hard deletion of expired soft deletes; the CLI previews unless --apply is set."""
from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, exists, func, or_, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql import Delete, Select

from app.config import get_settings
from app.models.chirp import Chirp, ChirpVote
from app.models.e2ee import Device, KyberPrekey, OneTimePrekey, SignedPrekey
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


@dataclass(frozen=True)
class KeyRetirementResult:
    """e2ee key-material retirement counts; independent of PurgeResult and its 'counts'

    report key (test_purge.py pins that dict's exact six-key shape) — a separate
    crypto-hygiene phase with its own cutoff, reported under its own report key.
    """

    retired_one_time_prekeys: int = 0
    retired_kyber_one_time_prekeys: int = 0
    retired_signed_prekeys: int = 0
    retired_revoked_device_prekeys: int = 0

    @property
    def physical_rows(self) -> int:
        return sum(asdict(self).values())

    def __add__(self, other: KeyRetirementResult) -> KeyRetirementResult:
        return KeyRetirementResult(**{
            field.name: getattr(self, field.name) + getattr(other, field.name)
            for field in fields(self)
        })


def _key_retirement_cutoff(
    now: datetime | None, grace_days: int | None,
) -> tuple[datetime, int]:
    days = _positive(
        grace_days if grace_days is not None
        else get_settings().e2ee_key_retirement_grace_days,
        "grace_days",
    )
    instant = now if now is not None else datetime.now(timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("now must include a timezone")
    try:
        return instant.astimezone(timezone.utc) - timedelta(days=days), days
    except OverflowError as exc:
        raise ValueError("grace_days is outside the supported date range") from exc


def _revoked_device_ids(cutoff: datetime) -> Select:
    # The Device row itself is never touched here — it stays the audit trail and stays
    # counted against MAX_RETAINED_DEVICES (routers/keys.py), which this job does not
    # change. Only its now-orphaned prekey rows are eligible for the wipe below.
    return select(Device.id).where(Device.revoked_at.is_not(None), Device.revoked_at < cutoff)


def _expired_one_time_prekeys(cutoff: datetime, revoked: Select) -> Select:
    return select(OneTimePrekey.id).where(
        OneTimePrekey.consumed_at.is_not(None), OneTimePrekey.consumed_at < cutoff,
        OneTimePrekey.device_id.not_in(revoked),
    )


def _expired_kyber_one_time_prekeys(cutoff: datetime, revoked: Select) -> Select:
    # is_last_resort rows are excluded here on purpose: they are never consumed, so
    # consumed_at alone never makes one eligible. A revoked device's last-resort row is
    # still retired, but only via the revoked-device wipe below, not this query.
    return select(KyberPrekey.id).where(
        KyberPrekey.consumed_at.is_not(None), KyberPrekey.is_last_resort.is_(False),
        KyberPrekey.consumed_at < cutoff, KyberPrekey.device_id.not_in(revoked),
    )


def _expired_signed_prekeys(cutoff: datetime, revoked: Select) -> Select:
    # The newest signed-prekey row per device always survives regardless of its own
    # age: a row is only eligible when a STRICTLY newer sibling exists for the same
    # device (tie-broken by id so two rows sharing a timestamp still pick one survivor).
    newer = aliased(SignedPrekey)
    has_newer_row = exists(select(newer.id).where(
        newer.device_id == SignedPrekey.device_id,
        or_(
            newer.created_at > SignedPrekey.created_at,
            and_(newer.created_at == SignedPrekey.created_at, newer.id > SignedPrekey.id),
        ),
    ))
    return select(SignedPrekey.id).where(
        SignedPrekey.created_at < cutoff, has_newer_row,
        SignedPrekey.device_id.not_in(revoked),
    )


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


async def retire_key_material_batch(
    session: AsyncSession, *, cutoff: datetime, batch_size: int = 100,
) -> KeyRetirementResult:
    """Delete at most batch_size rows per reason; caller commits.

    Mirrors purge_batch's SELECT-ids-FOR-UPDATE-SKIP-LOCKED-then-DELETE-by-id-batch
    pattern. Four reasons, each independently bounded: consumed EC one-time prekeys,
    consumed one-time Kyber prekeys, superseded signed prekeys (newest per device always
    excluded), and — separately — every prekey row of a device revoked past the grace
    window, regardless of that row's own consumed/created state.
    """
    _positive(batch_size, "batch_size")
    revoked = _revoked_device_ids(cutoff)

    one_time_ids = list((await session.scalars(
        _expired_one_time_prekeys(cutoff, revoked).order_by(OneTimePrekey.id)
        .limit(batch_size).with_for_update(skip_locked=True)
    )).all())
    one_time = 0
    if one_time_ids:
        one_time = await _delete_count(session, delete(OneTimePrekey).where(
            OneTimePrekey.id.in_(one_time_ids)
        ).execution_options(synchronize_session=False))

    kyber_ids = list((await session.scalars(
        _expired_kyber_one_time_prekeys(cutoff, revoked).order_by(KyberPrekey.id)
        .limit(batch_size).with_for_update(skip_locked=True)
    )).all())
    kyber_one_time = 0
    if kyber_ids:
        kyber_one_time = await _delete_count(session, delete(KyberPrekey).where(
            KyberPrekey.id.in_(kyber_ids)
        ).execution_options(synchronize_session=False))

    signed_ids = list((await session.scalars(
        _expired_signed_prekeys(cutoff, revoked).order_by(SignedPrekey.id)
        .limit(batch_size).with_for_update(skip_locked=True)
    )).all())
    signed = 0
    if signed_ids:
        signed = await _delete_count(session, delete(SignedPrekey).where(
            SignedPrekey.id.in_(signed_ids)
        ).execution_options(synchronize_session=False))

    revoked_wipe = 0
    for model in (OneTimePrekey, KyberPrekey, SignedPrekey):
        ids = list((await session.scalars(
            select(model.id).where(model.device_id.in_(revoked))
            .order_by(model.id).limit(batch_size).with_for_update(skip_locked=True)
        )).all())
        if ids:
            revoked_wipe += await _delete_count(session, delete(model).where(
                model.id.in_(ids)
            ).execution_options(synchronize_session=False))

    return KeyRetirementResult(one_time, kyber_one_time, signed, revoked_wipe)


async def preview_expired_key_material(
    session: AsyncSession, *, cutoff: datetime, count_limit: int,
) -> tuple[KeyRetirementResult, list[str]]:
    """SELECT-only aggregate preview mirroring preview_expired_soft_deletes' contract."""
    _positive(count_limit, "count_limit")
    revoked = _revoked_device_ids(cutoff)
    queries = {
        "retired_one_time_prekeys": _expired_one_time_prekeys(cutoff, revoked),
        "retired_kyber_one_time_prekeys": _expired_kyber_one_time_prekeys(cutoff, revoked),
        "retired_signed_prekeys": _expired_signed_prekeys(cutoff, revoked),
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

    revoked_total = 0
    for model in (OneTimePrekey, KyberPrekey, SignedPrekey):
        observed = int(await session.scalar(select(func.count()).select_from(
            select(model.id).where(model.device_id.in_(revoked))
            .limit(count_limit + 1).subquery()
        )) or 0)
        revoked_total += min(observed, count_limit)
        if observed > count_limit:
            capped.append("retired_revoked_device_prekeys")
    counts["retired_revoked_device_prekeys"] = revoked_total
    return KeyRetirementResult(**counts), capped


async def _has_remaining_key_material(session: AsyncSession, cutoff: datetime) -> bool:
    revoked = _revoked_device_ids(cutoff)
    return bool(await session.scalar(select(
        exists(_expired_one_time_prekeys(cutoff, revoked))
        | exists(_expired_kyber_one_time_prekeys(cutoff, revoked))
        | exists(_expired_signed_prekeys(cutoff, revoked))
        | exists(select(OneTimePrekey.id).where(OneTimePrekey.device_id.in_(revoked)))
        | exists(select(KyberPrekey.id).where(KyberPrekey.device_id.in_(revoked)))
        | exists(select(SignedPrekey.id).where(SignedPrekey.device_id.in_(revoked)))
    )))


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
    *, apply: bool = False, retention_days: int | None = None,
    key_retirement_grace_days: int | None = None, batch_size: int = 100,
    max_batches: int = 10, max_seconds: int = 120, now: datetime | None = None,
) -> dict[str, object]:
    """Run one finite job, reporting only aggregate counts and acknowledged commits.

    Content purge (report["counts"]) and e2ee key-material retirement
    (report["key_retirement_counts"]) are independent phases with independent cutoffs;
    neither's count or status feeds the other's.
    """
    cutoff, days = _cutoff(now, retention_days)
    key_cutoff, grace_days = _key_retirement_cutoff(now, key_retirement_grace_days)
    for name, value in (("batch_size", batch_size), ("max_batches", max_batches),
                        ("max_seconds", max_seconds)):
        _positive(value, name)
    from app.db import get_session_factory

    total = PurgeResult()
    key_total = KeyRetirementResult()
    report: dict[str, object] = {
        "mode": "apply" if apply else "dry_run", "cutoff": cutoff.isoformat(),
        "retention_days": days, "key_retirement_grace_days": grace_days,
        "key_retirement_cutoff": key_cutoff.isoformat(),
        "batch_size": batch_size, "max_batches": max_batches,
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
                    key_total, _key_capped = await preview_expired_key_material(
                        session, cutoff=key_cutoff, count_limit=batch_size * max_batches,
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
                    # Independent second phase, same session/deadline: content-purge
                    # status/remaining above is untouched by whatever happens here.
                    for _ in range(max_batches):
                        await _transaction_limits(session, deadline - loop.time())
                        key_batch = await retire_key_material_batch(
                            session, cutoff=key_cutoff, batch_size=batch_size,
                        )
                        committing = True
                        await session.commit()
                        committing = False
                        key_total += key_batch
                        await _transaction_limits(session, deadline - loop.time())
                        key_remaining = await _has_remaining_key_material(session, key_cutoff)
                        await session.rollback()
                        if not key_remaining or key_batch.physical_rows == 0:
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
    report["key_retirement_counts"] = asdict(key_total)
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
    parser.add_argument("--key-retirement-grace-days", type=_positive_arg, default=None)
    parser.add_argument("--batch-size", type=_positive_arg, default=100)
    parser.add_argument("--max-batches", type=_positive_arg, default=10)
    parser.add_argument("--max-seconds", type=_positive_arg, default=120)
    args = parser.parse_args(argv)
    # Resolve even the environment-sourced retention before constructing an engine.
    try:
        _, days = _cutoff(None, args.retention_days)
        _, grace_days = _key_retirement_cutoff(None, args.key_retirement_grace_days)
    except ValueError as exc:
        parser.error(str(exc))
    result = asyncio.run(run_purge_job(
        apply=args.apply, retention_days=days, key_retirement_grace_days=grace_days,
        batch_size=args.batch_size, max_batches=args.max_batches, max_seconds=args.max_seconds,
    ))
    print(json.dumps(result, sort_keys=True))
    if result["status"] not in ("preview", "complete"):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
