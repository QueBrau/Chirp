"""Purge job (board c69; /privacy section 14): app.jobs.purge.purge_expired_soft_deletes.

Inserts rows straight via SQL (not the API) so each row's deleted_at/removed_at can
be backdated precisely — the API always stamps "now", which can't put a row on
either side of a 30-day boundary deterministically.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
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


async def test_helper_rolls_back_all_cascades(
    client: AsyncClient, make_chapter_with: MakeChapterWith,
) -> None:
    """The existing helper still never commits its caller's transaction."""
    setup = await make_chapter_with("president")
    now = datetime.now(timezone.utc)
    post = await _insert_post(setup.chapter_id, setup.president.id,
                              deleted_at=now - timedelta(days=31))
    comment = await _insert_comment(post, setup.president.id, deleted_at=None)
    await _insert_post_like(post, setup.president.id)
    async with get_session_factory()() as session:
        result = await purge_expired_soft_deletes(session, now=now)
        assert result.total == 1
        assert result.physical_rows == 3
        await session.rollback()
    assert await _row_exists("posts", post)
    assert await _row_exists("post_comments", comment)
    async with get_session_factory()() as session:
        assert await session.scalar(text("SELECT count(*) FROM post_likes")) == 1


async def test_exact_cutoff_survives_for_all_retention_columns(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_campus: MakeCampus,
) -> None:
    setup = await make_chapter_with("president")
    now = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
    boundary = now - timedelta(days=RETENTION_DAYS)
    post = await _insert_post(setup.chapter_id, setup.president.id, deleted_at=boundary)
    comment = await _insert_comment(post, setup.president.id, deleted_at=boundary)
    chirp = await _insert_chirp(await make_campus(), setup.president.id, removed_at=boundary)
    result = await _run_purge(now=now)
    assert result.physical_rows == 0
    assert await _row_exists("posts", post)
    assert await _row_exists("post_comments", comment)
    assert await _row_exists("chirps", chirp)


async def test_dry_run_is_read_only_and_capped_without_touching_children(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_campus: MakeCampus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real PostgreSQL read-only mode plus an observed SQL stream prove no DML."""
    from sqlalchemy import event
    from app.db import get_engine
    from app.jobs import purge

    setup = await make_chapter_with("president")
    now = datetime.now(timezone.utc)
    expired = now - timedelta(days=31)
    post = await _insert_post(setup.chapter_id, setup.president.id, deleted_at=expired)
    for _ in range(3):
        await _insert_comment(post, setup.president.id, deleted_at=None)
    await _insert_post_like(post, setup.president.id)
    live = await _insert_post(setup.chapter_id, setup.president.id, deleted_at=None)
    await _insert_comment(live, setup.president.id, deleted_at=expired)
    chirp = await _insert_chirp(await make_campus(), setup.president.id, removed_at=expired)
    await _insert_chirp_vote(chirp, setup.president.id)
    observed: list[str] = []

    def record(_conn, _cursor, statement, _parameters, _context, _many):
        observed.append(statement)

    preview = purge.preview_expired_soft_deletes

    async def check_read_only(session, **kwargs):
        assert await session.scalar(text("SHOW transaction_read_only")) == "on"
        assert await session.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        return await preview(session, **kwargs)

    monkeypatch.setattr(purge, "preview_expired_soft_deletes", check_read_only)
    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        report = await purge.run_purge_job(now=now, batch_size=2, max_batches=1)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert report["status"] == "preview"
    assert report["batches_committed"] == 0
    assert report["counts"] == {
        "posts": 1, "post_comments": 1, "chirps": 1, "post_likes": 1,
        "cascaded_post_comments": 2, "chirp_votes": 1,
    }
    assert report["capped_counts"] == ["cascaded_post_comments"]
    assert all(sql.lstrip().split()[0].upper() in {"SELECT", "SET", "SHOW"}
               for sql in observed)
    assert await _row_exists("posts", post)
    assert await _row_exists("chirps", chirp)
    async with get_session_factory()() as session:
        for table, count in (("post_comments", 4), ("post_likes", 1), ("chirp_votes", 1)):
            assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == count


async def test_batch_budget_counts_children_and_drains_large_parent_next_run(
    client: AsyncClient, make_chapter_with: MakeChapterWith,
) -> None:
    from app.jobs.purge import run_purge_job

    setup = await make_chapter_with("president")
    now = datetime.now(timezone.utc)
    post = await _insert_post(setup.chapter_id, setup.president.id,
                              deleted_at=now - timedelta(days=31))
    for _ in range(5):
        await _insert_comment(post, setup.president.id, deleted_at=None)
    await _insert_post_like(post, setup.president.id)

    first = await run_purge_job(apply=True, now=now, batch_size=2, max_batches=1)
    assert first["status"] == "incomplete"
    assert first["remaining"] is True
    assert first["batches_committed"] == 1
    assert first["counts"]["posts"] == 0
    assert first["counts"]["cascaded_post_comments"] == 2
    assert first["physical_rows"] == 3
    assert await _row_exists("posts", post)
    async with get_session_factory()() as session:
        assert await session.scalar(text("SELECT count(*) FROM post_comments")) == 3

    second = await run_purge_job(apply=True, now=now, batch_size=2, max_batches=5)
    assert second["status"] == "complete"
    assert second["batches_committed"] == 2
    assert second["counts"]["posts"] == 1
    assert second["counts"]["cascaded_post_comments"] == 3
    assert not await _row_exists("posts", post)
    third = await run_purge_job(apply=True, now=now, batch_size=2, max_batches=1)
    assert third["status"] == "complete"
    assert third["physical_rows"] == 0


async def test_each_reason_is_bounded_and_historical_reports_survive(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser,
    make_campus: MakeCampus,
) -> None:
    from app.jobs.purge import purge_batch

    setup = await make_chapter_with("president")
    now = datetime.now(timezone.utc)
    expired = now - timedelta(days=31)
    post = await _insert_post(setup.chapter_id, setup.president.id, deleted_at=expired)
    live = await _insert_post(setup.chapter_id, setup.president.id, deleted_at=None)
    chirp = await _insert_chirp(await make_campus(), setup.president.id, removed_at=expired)
    for index in range(3):
        user = await make_user(f"Purge dependent {index}")
        await _insert_post_like(post, user.id)
        await _insert_comment(post, user.id, deleted_at=None)
        await _insert_comment(live, user.id, deleted_at=expired)
        await _insert_chirp_vote(chirp, user.id)
    async with get_session_factory()() as session:
        await session.execute(text(
            "INSERT INTO content_reports (reporter_id, target_type, target_id, reason) "
            "VALUES (:reporter, 'post', :post, 'purge test report')"
        ), {"reporter": setup.president.id, "post": post})
        await session.commit()
    async with get_session_factory()() as session:
        first = await purge_batch(session, cutoff=now - timedelta(days=30), batch_size=2)
        await session.commit()
    assert first.posts == first.chirps == 0
    assert first.post_likes == first.cascaded_post_comments == first.chirp_votes == 2
    assert first.post_comments == 2
    assert first.physical_rows == 8
    await _run_purge(now=now)
    async with get_session_factory()() as session:
        assert await session.scalar(text("SELECT count(*) FROM content_reports")) == 1
        assert await session.scalar(text("SELECT count(*) FROM post_comments")) == 0
        assert await session.scalar(text("SELECT count(*) FROM post_likes")) == 0
        assert await session.scalar(text("SELECT count(*) FROM chirp_votes")) == 0


async def test_locked_parent_is_reported_as_backlog_and_can_be_retried(
    client: AsyncClient, make_chapter_with: MakeChapterWith,
) -> None:
    from app.jobs.purge import run_purge_job

    setup = await make_chapter_with("president")
    now = datetime.now(timezone.utc)
    post = await _insert_post(setup.chapter_id, setup.president.id,
                              deleted_at=now - timedelta(days=31))
    async with get_session_factory()() as writer:
        await writer.execute(text("SELECT id FROM posts WHERE id = :id FOR UPDATE"), {"id": post})
        report = await run_purge_job(apply=True, now=now, batch_size=1, max_batches=2)
        assert report["status"] == "blocked"
        assert report["remaining"] is True
        assert report["physical_rows"] == 0
        await writer.rollback()
    retry = await run_purge_job(apply=True, now=now, batch_size=1, max_batches=2)
    assert retry["status"] == "complete"
    assert retry["physical_rows"] == 1


async def test_parent_lock_blocks_new_fk_children_until_batch_finishes(
    client: AsyncClient, make_chapter_with: MakeChapterWith,
) -> None:
    from sqlalchemy.exc import DBAPIError
    from app.jobs.purge import purge_batch

    setup = await make_chapter_with("president")
    now = datetime.now(timezone.utc)
    post = await _insert_post(setup.chapter_id, setup.president.id,
                              deleted_at=now - timedelta(days=31))
    for _ in range(2):
        await _insert_comment(post, setup.president.id, deleted_at=None)
    async with get_session_factory()() as purger:
        batch = await purge_batch(purger, cutoff=now - timedelta(days=30), batch_size=1)
        assert batch.posts == 0  # Still has a child, but holds the parent UPDATE lock.
        async with get_session_factory()() as writer:
            await writer.execute(text("SET LOCAL lock_timeout = 100"))
            with pytest.raises(DBAPIError) as exc:
                await writer.execute(text(
                    "INSERT INTO post_comments(post_id, author_id, body) "
                    "VALUES(:post, :author, 'concurrent child')"
                ), {"post": post, "author": setup.president.id})
            assert exc.value.orig.sqlstate == "55P03"  # Lock timeout, not FK corruption.
            await writer.rollback()
        await purger.commit()
    await _run_purge(now=now)
    assert not await _row_exists("posts", post)
