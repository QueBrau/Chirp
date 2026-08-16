"""Hard-delete job for soft-deleted content (board c69; /privacy section 14).

Posts, comments and yaks are soft-deleted in place: the API sets deleted_at /
removed_at and every read path filters the row out, but until this job existed
nothing ever issued a DELETE FROM for them. This module is that job. It finds rows
whose soft-delete timestamp is older than Settings.purge_retention_days and erases
them for good.

Models covered — every soft-deleting model in app/models/, found by grepping for
deleted_at / removed_at:
  - Post          (app.models.social)  -- deleted_at
  - PostComment   (app.models.social)  -- deleted_at
  - Yak           (app.models.yak)     -- removed_at

Nothing else in app/models/ soft-deletes (no other deleted_at/removed_at column
exists), so there is nothing else for this job to cover.

Purging a Post or a Yak also removes its PostLike / YakVote rows first. Those
tables have a plain `REFERENCES posts(id)` / `REFERENCES yaks(id)` FK with no
ON DELETE CASCADE at the DB level (see alembic/versions/0001_initial.py), so
deleting the parent while a like/vote still points at it would raise a foreign key
violation, not silently cascade. A purged post's comments are removed the same way
regardless of the comment's own deleted_at — once the post is gone forever there is
no route that can ever surface those comments again, so keeping them around only to
honor their own not-yet-expired window would just delay an inevitable orphan.
content_reports.target_id is intentionally not a foreign key (it is polymorphic
across yak/post/comment/message_forward/user), so a report that named purged
content is left as-is: a dangling but harmless historical record, matching
/privacy section 13's existing promise to keep report records reviewable.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.social import Post, PostComment, PostLike
from app.models.yak import Yak, YakVote


@dataclass(frozen=True)
class PurgeResult:
    """Row counts hard-deleted per table, returned so callers/tests can assert on them."""

    posts: int
    post_comments: int
    yaks: int

    @property
    def total(self) -> int:
        return self.posts + self.post_comments + self.yaks


async def purge_expired_soft_deletes(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    retention_days: int | None = None,
) -> PurgeResult:
    """Hard-delete posts/comments/yaks whose soft-delete timestamp is past the window.

    Pass `now` / `retention_days` to make the boundary deterministic in tests; both
    default to real time / Settings.purge_retention_days for the real job. Does not
    commit — the caller owns the transaction, so a caller that wraps this in a
    savepoint or rolls back on error leaves every row untouched.

    Safe to call twice in a row: a second call's WHERE clauses simply match nothing
    that the first call already removed, so it deletes 0 additional rows rather than
    erroring or double-counting.
    """
    resolved_now = now if now is not None else datetime.now(timezone.utc)
    resolved_retention_days = (
        retention_days if retention_days is not None else get_settings().purge_retention_days
    )
    cutoff = resolved_now - timedelta(days=resolved_retention_days)

    expired_post_ids = (
        await session.execute(
            select(Post.id).where(Post.deleted_at.is_not(None), Post.deleted_at < cutoff)
        )
    ).scalars().all()

    posts_deleted = 0
    if expired_post_ids:
        await session.execute(delete(PostLike).where(PostLike.post_id.in_(expired_post_ids)))
        await session.execute(
            delete(PostComment).where(PostComment.post_id.in_(expired_post_ids))
        )
        post_result = await session.execute(delete(Post).where(Post.id.in_(expired_post_ids)))
        posts_deleted = post_result.rowcount or 0

    # Comments soft-deleted on their own (post still live, or the post's own window
    # hasn't expired yet) rather than swept up by the post cascade above.
    comments_result = await session.execute(
        delete(PostComment).where(
            PostComment.deleted_at.is_not(None), PostComment.deleted_at < cutoff
        )
    )

    expired_yak_ids = (
        await session.execute(
            select(Yak.id).where(Yak.removed_at.is_not(None), Yak.removed_at < cutoff)
        )
    ).scalars().all()

    yaks_deleted = 0
    if expired_yak_ids:
        await session.execute(delete(YakVote).where(YakVote.yak_id.in_(expired_yak_ids)))
        yak_result = await session.execute(delete(Yak).where(Yak.id.in_(expired_yak_ids)))
        yaks_deleted = yak_result.rowcount or 0

    return PurgeResult(
        posts=posts_deleted,
        post_comments=comments_result.rowcount or 0,
        yaks=yaks_deleted,
    )


async def _run_and_report() -> PurgeResult:
    """Open one session against the configured DB, purge, commit, and return the counts."""
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        result = await purge_expired_soft_deletes(session)
        await session.commit()
    return result


def main() -> None:
    """CLI entry point: `python -m app.jobs.purge` — one run, prints counts, exits."""
    result = asyncio.run(_run_and_report())
    print(
        f"purge: posts={result.posts} post_comments={result.post_comments} "
        f"yaks={result.yaks} total={result.total}"
    )


if __name__ == "__main__":
    main()
