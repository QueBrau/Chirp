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
from uuid import UUID

from app import models
from app.services.account_deletion_hold import _protocol_start, advisory_key

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


async def _fence_users(session: AsyncSession, user_ids: list[UUID]) -> set[UUID]:
    """Take shared account locks, then return users with an active deletion hold.

    Locks are acquired in the shared signed-advisory-key order. A hold that starts
    after this point waits for the purge transaction, so both paths are ordered.
    """
    held: set[UUID] = set()
    # Match the shared guard's global order: signed advisory key, then UUID as a
    # deterministic tie-breaker.  Do not collapse equal keys: a hash collision
    # must still trigger the fresh hold check for every affected account.
    ordered = sorted(set(user_ids), key=lambda uid: (advisory_key(uid), str(uid)))
    for user_id in ordered:
        await _protocol_start(session, user_id)
        active = await session.scalar(_blocking_hold_for_user(user_id))
        if active is not None:
            held.add(user_id)
    return held


def _blocking_hold_for_user(user_id: object) -> Select:
    hold = models.AccountDeletionHold
    request = models.AccountDataRequest
    user = models.User
    completed_tombstone = and_(
        request.kind == "deletion", request.status == "completed",
        request.completed_at.is_not(None), user.is_ghost.is_(True),
        user.firebase_uid == func.concat("deleted:", user.id),
        user.email == func.concat("deleted+", user.id, "@invalid.chirp"),
    )
    return select(hold.id).select_from(hold).outerjoin(
        request, request.id == hold.request_id,
    ).outerjoin(user, user.id == hold.user_id).where(
        hold.user_id == user_id,
        hold.state == "active",
        or_(request.id.is_(None), request.user_id != hold.user_id,
            completed_tombstone.is_not(True)),
    ).limit(1)


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


def _revoked_device_ids(cutoff: datetime, excluded_users: set[UUID] | None = None) -> Select:
    # The Device row itself is never touched here — it stays the audit trail and stays
    # counted against MAX_RETAINED_DEVICES (routers/keys.py), which this job does not
    # change. Only its now-orphaned prekey rows are eligible for the wipe below.
    query = select(Device.id).where(Device.revoked_at.is_not(None), Device.revoked_at < cutoff)
    if excluded_users:
        query = query.where(Device.user_id.not_in(excluded_users))
    return query


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
    """Delete one bounded batch after fencing every affected account.

    Candidate IDs and owner IDs are captured before any row lock. Shared account
    advisory locks are acquired in deterministic order before parent/child row
    locks, matching hold activation and preventing unfenced late child deletes.
    """
    _positive(batch_size, "batch_size")
    # Look past held owners, while retaining a hard bound for each query.  The
    # first page must not let one held account starve unrelated eligible work.
    candidate_limit = batch_size + min(batch_size, 256)
    post_candidates = (await session.execute(
        select(Post.id, Post.author_id).where(
            Post.deleted_at < cutoff,
            ~exists(_blocking_hold_for_user(Post.author_id)),
            # A retained held child would keep this parent at the front of
            # every page. Defer the whole parent until that child's deletion
            # completes, allowing unrelated parents to make progress.
            ~exists(select(PostLike.post_id).where(
                PostLike.post_id == Post.id,
                exists(_blocking_hold_for_user(PostLike.user_id)),
            )),
            ~exists(select(PostComment.id).where(
                PostComment.post_id == Post.id,
                exists(_blocking_hold_for_user(PostComment.author_id)),
            )),
        )
        .order_by(Post.deleted_at, Post.id).limit(candidate_limit)
    )).all()
    post_candidate_ids = [row.id for row in post_candidates]
    post_owner_ids = [row.author_id for row in post_candidates]
    post_like_candidates = (await session.execute(
        select(PostLike.post_id, PostLike.user_id)
        .where(PostLike.post_id.in_(post_candidate_ids))
        .where(~exists(_blocking_hold_for_user(PostLike.user_id)))
        .order_by(PostLike.post_id, PostLike.user_id).limit(candidate_limit)
    )).all() if post_candidate_ids else []
    post_comment_candidates = (await session.execute(
        select(PostComment.id, PostComment.post_id, PostComment.author_id)
        .where(PostComment.post_id.in_(post_candidate_ids))
        .where(~exists(_blocking_hold_for_user(PostComment.author_id)))
        .order_by(PostComment.post_id, PostComment.id).limit(candidate_limit)
    )).all() if post_candidate_ids else []
    independent_comment_candidates = (await session.execute(
        select(PostComment.id, PostComment.author_id, Post.author_id.label("post_author_id"))
        .join(Post, Post.id == PostComment.post_id)
        .where(PostComment.deleted_at < cutoff,
               or_(Post.deleted_at.is_(None), Post.deleted_at >= cutoff),
               ~exists(_blocking_hold_for_user(PostComment.author_id)),
               ~exists(_blocking_hold_for_user(Post.author_id)))
        .order_by(PostComment.deleted_at, PostComment.id).limit(candidate_limit)
    )).all()
    chirp_candidates = (await session.execute(
        select(Chirp.id, Chirp.author_id).where(
            Chirp.removed_at < cutoff,
            ~exists(_blocking_hold_for_user(Chirp.author_id)),
            ~exists(select(ChirpVote.chirp_id).where(
                ChirpVote.chirp_id == Chirp.id,
                exists(_blocking_hold_for_user(ChirpVote.user_id)),
            )),
        )
        .order_by(Chirp.removed_at, Chirp.id).limit(candidate_limit)
    )).all()
    chirp_candidate_ids = [row.id for row in chirp_candidates]
    chirp_vote_candidates = (await session.execute(
        select(ChirpVote.chirp_id, ChirpVote.user_id)
        .where(ChirpVote.chirp_id.in_(chirp_candidate_ids))
        .where(~exists(_blocking_hold_for_user(ChirpVote.user_id)))
        .order_by(ChirpVote.chirp_id, ChirpVote.user_id).limit(candidate_limit)
    )).all() if chirp_candidate_ids else []
    owners = post_owner_ids + [r.user_id for r in post_like_candidates]
    owners += [r.author_id for r in post_comment_candidates]
    owners += [r.author_id for r in independent_comment_candidates]
    owners += [r.post_author_id for r in independent_comment_candidates]
    owners += [r.author_id for r in chirp_candidates]
    owners += [r.user_id for r in chirp_vote_candidates]
    held = await _fence_users(session, owners)

    post_ids = [row.id for row in post_candidates if row.author_id not in held][:batch_size]
    post_ids = list((await session.scalars(
        select(Post.id).where(Post.id.in_(post_ids), Post.deleted_at < cutoff)
        .order_by(Post.deleted_at, Post.id).with_for_update(skip_locked=True)
    )).all())
    likes = cascaded_comments = posts = 0
    if post_ids:
        like_keys = [(r.post_id, r.user_id) for r in post_like_candidates
                     if r.post_id in post_ids and r.user_id not in held][:batch_size]
        if like_keys:
            likes = await _delete_count(session, delete(PostLike).where(
                tuple_(PostLike.post_id, PostLike.user_id).in_(like_keys)
            ).execution_options(synchronize_session=False))
        child_ids = [r.id for r in post_comment_candidates
                     if r.post_id in post_ids and r.author_id not in held
                     and r.id is not None][:batch_size]
        if child_ids:
            cascaded_comments = await _delete_count(session, delete(PostComment).where(
                PostComment.id.in_(child_ids), PostComment.post_id.in_(post_ids)
            ).execution_options(synchronize_session=False))
        posts = await _delete_count(session, delete(Post).where(
            Post.id.in_(post_ids), Post.deleted_at < cutoff,
            ~exists(select(PostLike.post_id).where(PostLike.post_id == Post.id)),
            ~exists(select(PostComment.id).where(PostComment.post_id == Post.id)),
        ).execution_options(synchronize_session=False))

    independent_ids = [r.id for r in independent_comment_candidates
                       if r.author_id not in held and r.post_author_id not in held][:batch_size]
    locked_independent = list((await session.scalars(
        select(PostComment.id).where(
            PostComment.id.in_(independent_ids), PostComment.deleted_at < cutoff,
        ).order_by(PostComment.deleted_at, PostComment.id)
        .with_for_update(skip_locked=True)
    )).all()) if independent_ids else []
    comments = await _delete_count(session, delete(PostComment).where(
        PostComment.id.in_(locked_independent), PostComment.deleted_at < cutoff
    ).execution_options(synchronize_session=False))

    chirp_ids = [row.id for row in chirp_candidates if row.author_id not in held][:batch_size]
    chirp_ids = list((await session.scalars(
        select(Chirp.id).where(Chirp.id.in_(chirp_ids), Chirp.removed_at < cutoff)
        .order_by(Chirp.removed_at, Chirp.id).with_for_update(skip_locked=True)
    )).all())
    votes = chirps = 0
    if chirp_ids:
        vote_keys = [(r.chirp_id, r.user_id) for r in chirp_vote_candidates
                     if r.chirp_id in chirp_ids and r.user_id not in held][:batch_size]
        if vote_keys:
            votes = await _delete_count(session, delete(ChirpVote).where(
                tuple_(ChirpVote.chirp_id, ChirpVote.user_id).in_(vote_keys)
            ).execution_options(synchronize_session=False))
        chirps = await _delete_count(session, delete(Chirp).where(
            Chirp.id.in_(chirp_ids), Chirp.removed_at < cutoff,
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
    candidate_limit = batch_size + min(batch_size, 256)
    revoked_rows = (await session.execute(
        select(Device.id, Device.user_id).where(
            Device.revoked_at.is_not(None), Device.revoked_at < cutoff,
            ~exists(_blocking_hold_for_user(Device.user_id)),
        ).order_by(Device.revoked_at, Device.id).limit(candidate_limit)
    )).all()
    revoked_ids = [row.id for row in revoked_rows]

    # Capture both the row IDs and their owning account before taking any row
    # locks.  A prekey ID is not a device ID; filtering by the former was an
    # easy way to silently skip all key retirement for held accounts.
    normal_queries = (
        (OneTimePrekey, _expired_one_time_prekeys(cutoff, _revoked_device_ids(cutoff))),
        (KyberPrekey, _expired_kyber_one_time_prekeys(cutoff, _revoked_device_ids(cutoff))),
        (SignedPrekey, _expired_signed_prekeys(cutoff, _revoked_device_ids(cutoff))),
    )
    normal_rows: dict[type, list[tuple[UUID, UUID]]] = {}
    owners: list[UUID] = [row.user_id for row in revoked_rows]
    for model, query in normal_queries:
        rows = (await session.execute(
            select(model.id, Device.user_id).join(Device, Device.id == model.device_id)
            .where(
                model.id.in_(query.with_only_columns(model.id)),
                ~exists(_blocking_hold_for_user(Device.user_id)),
            )
            .order_by(model.id).limit(candidate_limit)
        )).all()
        normal_rows[model] = [(row.id, row.user_id) for row in rows]
        owners.extend(row.user_id for row in rows)
    revoked_rows_by_model: dict[type, list[tuple[UUID, UUID]]] = {}
    for model in (OneTimePrekey, KyberPrekey, SignedPrekey):
        rows = (await session.execute(
            select(model.id, Device.user_id).join(Device, Device.id == model.device_id)
            .where(model.device_id.in_(revoked_ids)).order_by(model.id).limit(candidate_limit)
        )).all() if revoked_ids else []
        revoked_rows_by_model[model] = [(row.id, row.user_id) for row in rows]
        owners.extend(row.user_id for row in rows)
    held = await _fence_users(session, owners)

    async def delete_owned(
        model: type, rows: list[tuple[UUID, UUID]], eligibility: Select,
    ) -> int:
        ids = [row_id for row_id, owner_id in rows if owner_id not in held][:batch_size]
        if not ids:
            return 0
        locked = list((await session.scalars(
            select(model.id).where(model.id.in_(ids)).order_by(model.id)
            .with_for_update(skip_locked=True)
        )).all())
        if not locked:
            return 0
        # Reapply the exact retention predicate after locking.  A concurrent
        # key upload/revocation can change eligibility between capture and the
        # row lock; deleting by ID alone would violate the retirement policy.
        eligible = list((await session.scalars(
            eligibility.where(model.id.in_(locked))
        )).all())
        if not eligible:
            return 0
        return await _delete_count(session, delete(model).where(
            model.id.in_(eligible)
        ).execution_options(synchronize_session=False))

    one_time = await delete_owned(
        OneTimePrekey, normal_rows[OneTimePrekey], normal_queries[0][1],
    )
    kyber_one_time = await delete_owned(
        KyberPrekey, normal_rows[KyberPrekey], normal_queries[1][1],
    )
    signed = await delete_owned(
        SignedPrekey, normal_rows[SignedPrekey], normal_queries[2][1],
    )
    revoked_wipe = 0
    for model in (OneTimePrekey, KyberPrekey, SignedPrekey):
        revoked_wipe += await delete_owned(
            model, revoked_rows_by_model[model],
            select(model.id).join(Device, Device.id == model.device_id).where(
                model.device_id.in_(revoked_ids),
                Device.revoked_at.is_not(None), Device.revoked_at < cutoff,
            ),
        )

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

    Content and key retirement have independent cutoffs, counts and progress.
    Existing content fields keep their meaning; key_retirement_* fields report
    the second phase. CLI success requires both phases to finish successfully.
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
        "key_retirement_status": "not_started", "key_retirement_remaining": None,
        "key_retirement_capped_counts": [], "key_retirement_batches_committed": 0,
    }
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_seconds
    committing = False
    committed_batches = 0
    committed_key_batches = 0
    key_phase_started = False
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
                    key_phase_started = True
                    key_total, key_capped = await preview_expired_key_material(
                        session, cutoff=key_cutoff, count_limit=batch_size * max_batches,
                    )
                    report.update(status="preview", capped_counts=capped,
                                  remaining=total.physical_rows > 0,
                                  key_retirement_status="preview",
                                  key_retirement_remaining=key_total.physical_rows > 0,
                                  key_retirement_capped_counts=sorted(set(key_capped)))
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
                    key_phase_started = True
                    report["key_retirement_status"] = "incomplete"
                    for _ in range(max_batches):
                        await _transaction_limits(session, deadline - loop.time())
                        key_batch = await retire_key_material_batch(
                            session, cutoff=key_cutoff, batch_size=batch_size,
                        )
                        committing = True
                        await session.commit()
                        committing = False
                        key_total += key_batch
                        committed_key_batches += 1
                        report["key_retirement_batches_committed"] = committed_key_batches
                        await _transaction_limits(session, deadline - loop.time())
                        key_remaining = await _has_remaining_key_material(session, key_cutoff)
                        await session.rollback()
                        report["key_retirement_remaining"] = key_remaining
                        if not key_remaining:
                            report["key_retirement_status"] = "complete"
                            break
                        if key_batch.physical_rows == 0:
                            report["key_retirement_status"] = "blocked"
                            break
    except TimeoutError:
        report.update(status="timed_out", remaining=None, commit_outcome_unknown=committing)
        if key_phase_started:
            report.update(key_retirement_status="timed_out", key_retirement_remaining=None)
    except Exception as exc:
        # Database errors can contain SQL parameters and connection details. Do not
        # emit raw exceptions or tracebacks into the job log. Preserve prior commits.
        report.update(status="failed", remaining=None, commit_outcome_unknown=committing,
                      error_type=type(exc).__name__)
        if key_phase_started:
            report.update(key_retirement_status="failed", key_retirement_remaining=None)
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
    expected_status = "complete" if args.apply else "preview"
    if (result.get("status") != expected_status
            or result.get("key_retirement_status") != expected_status):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
