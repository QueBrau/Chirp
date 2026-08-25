"""Purge job (board c69; /privacy section 14): app.jobs.purge.purge_expired_soft_deletes.

Inserts rows straight via SQL (not the API) so each row's deleted_at/removed_at can
be backdated precisely — the API always stamps "now", which can't put a row on
either side of a 30-day boundary deterministically.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy import text

from app.db import get_session_factory
from app.jobs.purge import purge_expired_soft_deletes
from tests.conftest import ChapterSetup, MakeCampus, MakeChapterWith, MakeUser

RETENTION_DAYS = 30


async def _insert_post(chapter_id: str, author_id: str, *, deleted_at: datetime | None) -> str:
    # campus_id is NOT NULL since c71 and is read off the CHAPTER here rather than
    # passed in, for the same reason routers/feed.py create_post does it that way:
    # the post belongs where it was made. Deriving it in the helper also keeps every
    # call site below unchanged - the purge job's subject is retention, not campus.
    async with get_session_factory()() as session:
        result = await session.execute(
            text(
                "INSERT INTO posts (chapter_id, campus_id, author_id, body, deleted_at) "
                "VALUES (:chapter_id, "
                "(SELECT campus_id FROM chapters WHERE id = :chapter_id), "
                ":author_id, 'purge test post', :deleted_at) "
                "RETURNING id"
            ),
            {"chapter_id": chapter_id, "author_id": author_id, "deleted_at": deleted_at},
        )
        post_id = str(result.scalar_one())
        await session.commit()
    return post_id


async def _insert_comment(post_id: str, author_id: str, *, deleted_at: datetime | None) -> str:
    async with get_session_factory()() as session:
        result = await session.execute(
            text(
                "INSERT INTO post_comments (post_id, author_id, body, deleted_at) "
                "VALUES (:post_id, :author_id, 'purge test comment', :deleted_at) "
                "RETURNING id"
            ),
            {"post_id": post_id, "author_id": author_id, "deleted_at": deleted_at},
        )
        comment_id = str(result.scalar_one())
        await session.commit()
    return comment_id


async def _insert_post_like(post_id: str, user_id: str) -> None:
    async with get_session_factory()() as session:
        await session.execute(
            text("INSERT INTO post_likes (post_id, user_id) VALUES (:post_id, :user_id)"),
            {"post_id": post_id, "user_id": user_id},
        )
        await session.commit()


async def _insert_chirp(campus_id: str, author_id: str, *, removed_at: datetime | None) -> str:
    async with get_session_factory()() as session:
        result = await session.execute(
            text(
                "INSERT INTO chirps (campus_id, author_id, body, removed_at) "
                "VALUES (:campus_id, :author_id, 'purge test chirp', :removed_at) "
                "RETURNING id"
            ),
            {"campus_id": campus_id, "author_id": author_id, "removed_at": removed_at},
        )
        chirp_id = str(result.scalar_one())
        await session.commit()
    return chirp_id


async def _insert_chirp_vote(chirp_id: str, user_id: str) -> None:
    async with get_session_factory()() as session:
        await session.execute(
            text(
                "INSERT INTO chirp_votes (chirp_id, user_id, value) VALUES (:chirp_id, :user_id, 1)"
            ),
            {"chirp_id": chirp_id, "user_id": user_id},
        )
        await session.commit()


async def _row_exists(table: str, row_id: str) -> bool:
    async with get_session_factory()() as session:
        result = await session.execute(
            text(f"SELECT 1 FROM {table} WHERE id = :id"), {"id": row_id}
        )
        return result.first() is not None


async def _run_purge(*, now: datetime, retention_days: int = RETENTION_DAYS):
    async with get_session_factory()() as session:
        result = await purge_expired_soft_deletes(session, now=now, retention_days=retention_days)
        await session.commit()
    return result


async def test_purge_posts_expired_gone_fresh_and_live_survive(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """A post deleted 31 days ago is purged; one deleted 5 days ago and a live post are not."""
    setup: ChapterSetup = await make_chapter_with("president")
    now = datetime.now(timezone.utc)

    expired_post = await _insert_post(
        setup.chapter_id, setup.president.id, deleted_at=now - timedelta(days=31)
    )
    fresh_post = await _insert_post(
        setup.chapter_id, setup.president.id, deleted_at=now - timedelta(days=5)
    )
    live_post = await _insert_post(setup.chapter_id, setup.president.id, deleted_at=None)

    result = await _run_purge(now=now)

    assert result.posts == 1
    assert not await _row_exists("posts", expired_post)
    assert await _row_exists("posts", fresh_post)
    assert await _row_exists("posts", live_post)


async def test_purge_comments_expired_gone_fresh_and_live_survive(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Same window rule applied to post_comments, independent of the parent post's state."""
    setup: ChapterSetup = await make_chapter_with("president")
    now = datetime.now(timezone.utc)
    post_id = await _insert_post(setup.chapter_id, setup.president.id, deleted_at=None)

    expired_comment = await _insert_comment(
        post_id, setup.president.id, deleted_at=now - timedelta(days=31)
    )
    fresh_comment = await _insert_comment(
        post_id, setup.president.id, deleted_at=now - timedelta(days=5)
    )
    live_comment = await _insert_comment(post_id, setup.president.id, deleted_at=None)

    result = await _run_purge(now=now)

    assert result.post_comments == 1
    assert not await _row_exists("post_comments", expired_comment)
    assert await _row_exists("post_comments", fresh_comment)
    assert await _row_exists("post_comments", live_comment)


async def test_purge_chirps_expired_gone_fresh_and_live_survive(
    client: AsyncClient, make_campus: MakeCampus, make_user: MakeUser
) -> None:
    """Same window rule applied to chirps via removed_at instead of deleted_at."""
    campus_id = await make_campus()
    author = await make_user("Chirp Author", account_type="non_greek")
    now = datetime.now(timezone.utc)

    expired_chirp = await _insert_chirp(campus_id, author.id, removed_at=now - timedelta(days=31))
    fresh_chirp = await _insert_chirp(campus_id, author.id, removed_at=now - timedelta(days=5))
    live_chirp = await _insert_chirp(campus_id, author.id, removed_at=None)

    result = await _run_purge(now=now)

    assert result.chirps == 1
    assert not await _row_exists("chirps", expired_chirp)
    assert await _row_exists("chirps", fresh_chirp)
    assert await _row_exists("chirps", live_chirp)


async def test_purge_post_cascades_likes_and_comments_without_fk_violation(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Purging a post also removes its likes/comments — post_likes.post_id and
    post_comments.post_id are plain FKs with no ON DELETE CASCADE, so deleting the
    post first would raise a ForeignKeyViolation if this job didn't clear them first.
    """
    setup: ChapterSetup = await make_chapter_with("president")
    now = datetime.now(timezone.utc)
    expired_post = await _insert_post(
        setup.chapter_id, setup.president.id, deleted_at=now - timedelta(days=31)
    )
    await _insert_post_like(expired_post, setup.president.id)
    live_comment_on_expired_post = await _insert_comment(
        expired_post, setup.president.id, deleted_at=None
    )

    result = await _run_purge(now=now)

    assert result.posts == 1
    assert not await _row_exists("posts", expired_post)
    assert not await _row_exists("post_comments", live_comment_on_expired_post)


async def test_purge_chirp_cascades_votes_without_fk_violation(
    client: AsyncClient, make_campus: MakeCampus, make_user: MakeUser
) -> None:
    """Purging a chirp also removes its votes — chirp_votes.chirp_id has no ON DELETE CASCADE."""
    campus_id = await make_campus()
    author = await make_user("Chirp Author", account_type="non_greek")
    voter = await make_user("Chirp Voter", account_type="non_greek")
    now = datetime.now(timezone.utc)
    expired_chirp = await _insert_chirp(campus_id, author.id, removed_at=now - timedelta(days=31))
    await _insert_chirp_vote(expired_chirp, voter.id)

    result = await _run_purge(now=now)

    assert result.chirps == 1
    assert not await _row_exists("chirps", expired_chirp)


async def test_purge_is_safe_to_run_twice(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """A second run right after the first finds nothing left to do — no error, 0 rows."""
    setup: ChapterSetup = await make_chapter_with("president")
    now = datetime.now(timezone.utc)
    await _insert_post(setup.chapter_id, setup.president.id, deleted_at=now - timedelta(days=31))

    first = await _run_purge(now=now)
    second = await _run_purge(now=now)

    assert first.posts == 1
    assert second.posts == 0
    assert second.total == 0


async def test_purge_uses_settings_retention_when_not_overridden(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """With no explicit retention_days, the job falls back to Settings.purge_retention_days
    (default 30) rather than silently doing nothing or purging everything.
    """
    from app.config import get_settings

    assert get_settings().purge_retention_days == RETENTION_DAYS

    setup: ChapterSetup = await make_chapter_with("president")
    now = datetime.now(timezone.utc)
    expired_post = await _insert_post(
        setup.chapter_id, setup.president.id, deleted_at=now - timedelta(days=31)
    )

    async with get_session_factory()() as session:
        result = await purge_expired_soft_deletes(session, now=now)  # no retention_days passed
        await session.commit()

    assert result.posts == 1
    assert not await _row_exists("posts", expired_post)
