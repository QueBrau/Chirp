"""Chapter feed + campus feed: posts CRUD (soft delete), likes, comments.

Post audience: 'org' (private to the chapter, never leaves /chapters/{id}/posts)
or 'campus' (also surfaces on GET /campuses/{id}/feed). Chosen by the author at
compose time (PostCreate.audience), defaulting to 'org' — see board Decisions log,
Aug 14. "For You" is a ranking over the same campus posts, not a third value.
"""
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.core.campus_access import require_campus_member, require_verified_campus
from app.core.errors import forbidden, not_found
from app.core.permissions import Role
from app.db import get_session
from app.middleware.auth import get_current_user
from app.middleware.org_scope import get_current_membership
from app.schemas.social import (
    FeedPostOut,
    PostCommentCreate,
    PostCommentOut,
    PostCreate,
    PostLikeOut,
    PostOut,
    PostUpdate,
)

router = APIRouter(tags=["feed"])


def _post_counts_select(caller_id: uuid.UUID):
    """Base SELECT for posts + author identity + batched like/comment counts (c43),
    with blocked-author filtering (c35).

    Scalar subqueries (not a join-and-COUNT(DISTINCT)) so a post with many likes
    AND many comments doesn't fan out into a cartesian product of rows before
    counting — that would multiply rows (3 likes x 4 comments = 12 rows) and
    COUNT(DISTINCT) only masks the bug while making the query slow.

    The UserBlock outerjoin/filter is folded in here rather than repeated in each
    caller (list_posts, list_campus_feed) since both already thread `caller_id`
    through for liked_by_me — same anti-join proven in routers/yaks.py list_yaks:
    outerjoin on (blocked_id == Post.author_id) AND (blocker_id == caller_id), then
    WHERE blocker_id IS NULL. The ON clause pins blocker_id to this one caller, so
    it matches at most one UserBlock row per post — no fan-out. A blocked author's
    posts are simply ABSENT: no tombstone, no count, nothing revealing a hide
    happened (mirrors the yak anonymity invariant, SPEC §8.3, applied here to
    known-author feed posts).
    """
    like_count = (
        select(func.count())
        .select_from(models.PostLike)
        .where(models.PostLike.post_id == models.Post.id)
        .correlate(models.Post)
        .scalar_subquery()
    )
    comment_count = (
        select(func.count())
        .select_from(models.PostComment)
        .where(
            models.PostComment.post_id == models.Post.id,
            models.PostComment.deleted_at.is_(None),
        )
        .correlate(models.Post)
        .scalar_subquery()
    )
    liked_by_me = (
        select(models.PostLike.post_id)
        .where(
            models.PostLike.post_id == models.Post.id,
            models.PostLike.user_id == caller_id,
        )
        .correlate(models.Post)
        .exists()
    )
    return (
        select(
            models.Post,
            models.User.display_name,
            models.User.avatar_url,
            like_count.label("like_count"),
            comment_count.label("comment_count"),
            liked_by_me.label("liked_by_me"),
        )
        # INNER JOIN: display_name is non-null on FeedPostOut (matches MemberOut).
        .join(models.User, models.User.id == models.Post.author_id)
        .outerjoin(
            models.UserBlock,
            (models.UserBlock.blocked_id == models.Post.author_id)
            & (models.UserBlock.blocker_id == caller_id),
        )
        .where(models.UserBlock.blocker_id.is_(None))
    )


def _feed_post_out(row: Any) -> FeedPostOut:
    """Build a FeedPostOut from one row of `_post_counts_select`."""
    post, display_name, avatar_url, like_count, comment_count, liked_by_me = row
    return FeedPostOut(
        id=post.id,
        chapter_id=post.chapter_id,
        author_id=post.author_id,
        body=post.body,
        media_urls=post.media_urls,
        audience=post.audience,
        post_type=post.post_type,
        duration_sec=post.duration_sec,
        created_at=post.created_at,
        display_name=display_name,
        avatar_url=avatar_url,
        like_count=like_count,
        comment_count=comment_count,
        liked_by_me=liked_by_me,
    )


async def _post_with_membership(
    post_id: uuid.UUID,
    user: models.User,
    session: AsyncSession,
) -> tuple[models.Post, models.Membership]:
    """Load a live post and the caller's active membership in the post's chapter.

    /posts/{post_id}/* routes have no chapter_id path param, so org scoping is
    checked through the post's chapter (§8.4 spirit): 404 if the post is missing
    or soft-deleted, 403 if the caller is not an active member.
    """
    post = await session.get(models.Post, post_id)
    if post is None or post.deleted_at is not None:
        raise not_found("post_not_found")
    result = await session.execute(
        select(models.Membership).where(
            models.Membership.chapter_id == post.chapter_id,
            models.Membership.user_id == user.id,
            models.Membership.status == "active",
        )
    )
    membership = result.scalar_one_or_none()
    if membership is None:
        raise forbidden("not_a_member")
    return post, membership


@router.get("/chapters/{chapter_id}/posts")
async def list_posts(
    chapter_id: uuid.UUID,
    membership: models.Membership = Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
) -> list[FeedPostOut]:
    """List the chapter's posts, newest first, excluding soft-deleted ones.

    Includes author identity and batched like/comment counts in one round trip
    (c43) — see `_post_counts_select` for why this isn't a join-and-COUNT(DISTINCT).
    Posts by an author the caller has blocked are silently absent (c35) — see
    `_post_counts_select` for the anti-join.
    """
    stmt = (
        _post_counts_select(membership.user_id)
        .where(
            models.Post.chapter_id == chapter_id,
            models.Post.deleted_at.is_(None),
        )
        .order_by(models.Post.created_at.desc())
    )
    result = await session.execute(stmt)
    return [_feed_post_out(row) for row in result.all()]


@router.post("/chapters/{chapter_id}/posts", status_code=201)
async def create_post(
    chapter_id: uuid.UUID,
    body: PostCreate,
    membership: models.Membership = Depends(get_current_membership),
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> PostOut:
    """Create a post authored by the caller.

    `audience` defaults to 'org' (PostCreate schema default) so a client that
    omits it can never accidentally broadcast a chapter post campus-wide.

    THE CAMPUS GATE APPLIES HERE TOO, and this is the route it was missing (c88). The
    path is chapter-scoped, so it never went through `require_campus_member` and read
    as an org endpoint — but `audience='campus'` makes the post appear on
    GET /campuses/{id}/feed, so this is a campus-wide WRITE wearing a chapter URL.
    Gating the read alone would have left the door open facing the other way: an
    unverified member could not READ the campus feed but could still PUBLISH to it,
    which is the worse half of the pair.

    The campus is taken from the CHAPTER, never from the caller's own campus_id — the
    post lands on the chapter's campus feed, so that is the campus whose gate must be
    satisfied, and reading it off the user would let a mismatched pair through.
    """
    if body.audience == "campus":
        chapter = await session.get(models.Chapter, chapter_id)
        if chapter is None:
            raise not_found("chapter_not_found")
        require_verified_campus(user, chapter.campus_id)

    post = models.Post(
        chapter_id=chapter_id,
        author_id=membership.user_id,
        body=body.body,
        media_urls=body.media_urls,
        audience=body.audience,
        post_type=body.post_type,
        duration_sec=body.duration_sec,
    )
    session.add(post)
    await session.commit()
    await session.refresh(post)
    return PostOut.model_validate(post)


@router.get("/campuses/{campus_id}/feed")
async def list_campus_feed(
    campus_id: uuid.UUID,
    before: datetime | None = None,
    before_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    user: models.User = Depends(require_campus_member),
    session: AsyncSession = Depends(get_session),
) -> list[FeedPostOut]:
    """Campus-wide feed: only `audience = 'campus'` posts from chapters on this
    campus, newest first. 'org' posts NEVER appear here — that is the endpoint's
    core privacy guarantee (board Decisions log, Aug 14).

    Reverse-chron with a compound (created_at, id) cursor (`before` + `before_id`)
    so rows sharing a timestamp at a page boundary are never skipped, matching
    routers/messages.py list_messages / tests/test_pagination.py exactly. `before`
    alone still works (legacy clients) but doesn't guarantee that tie-break.

    Also carries author identity and batched like/comment counts (c43), same
    shape as GET /chapters/{id}/posts. Posts by an author the caller has blocked
    are silently absent (c35) — see `_post_counts_select` for the anti-join.
    """
    stmt = (
        _post_counts_select(user.id)
        .join(models.Chapter, models.Chapter.id == models.Post.chapter_id)
        .where(
            models.Chapter.campus_id == campus_id,
            models.Post.audience == "campus",
            models.Post.deleted_at.is_(None),
        )
    )
    if before is not None and before_id is not None:
        stmt = stmt.where(
            tuple_(models.Post.created_at, models.Post.id) < (before, before_id)
        )
    elif before is not None:
        stmt = stmt.where(models.Post.created_at < before)
    stmt = stmt.order_by(
        models.Post.created_at.desc(), models.Post.id.desc()
    ).limit(limit)

    result = await session.execute(stmt)
    return [_feed_post_out(row) for row in result.all()]


@router.patch("/chapters/{chapter_id}/posts/{post_id}")
async def update_post(
    chapter_id: uuid.UUID,
    post_id: uuid.UUID,
    body: PostUpdate,
    membership: models.Membership = Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
) -> PostOut:
    """Edit a post; author only."""
    post = await session.get(models.Post, post_id)
    if post is None or post.chapter_id != chapter_id or post.deleted_at is not None:
        raise not_found("post_not_found")
    if post.author_id != membership.user_id:
        raise forbidden("not_author")
    if body.body is not None:
        post.body = body.body
    if body.media_urls is not None:
        post.media_urls = body.media_urls
    await session.commit()
    return PostOut.model_validate(post)


@router.delete("/chapters/{chapter_id}/posts/{post_id}", status_code=204)
async def delete_post(
    chapter_id: uuid.UUID,
    post_id: uuid.UUID,
    membership: models.Membership = Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Soft-delete a post (sets deleted_at); author or president only."""
    post = await session.get(models.Post, post_id)
    if post is None or post.chapter_id != chapter_id or post.deleted_at is not None:
        raise not_found("post_not_found")
    if post.author_id != membership.user_id and membership.role != Role.president.value:
        raise forbidden("not_author_or_president")
    post.deleted_at = datetime.now(timezone.utc)
    await session.commit()


@router.delete("/posts/{post_id}", status_code=204)
async def delete_own_post(
    post_id: uuid.UUID,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Soft-delete a post you authored, without going through a chapter (board c84).

    The existing route is DELETE /chapters/{chapter_id}/posts/{post_id} and it matches
    on post.chapter_id == the path chapter. That is fine for org posts and impossible
    to satisfy for a post that has no chapter: there is no chapter_id to put in the URL,
    so the author can publish and then cannot take it back.

    WHY THIS LANDS BEFORE THE BUG DOES. On main today posts.chapter_id is NOT NULL, so
    the broken state is unreachable and this route is merely a nicer alias. It becomes
    load-bearing the moment c71 merges: q/campus-posts changes the column to
    `Mapped[uuid.UUID | None]` and still ships only the chapter-scoped delete, so a
    campus post by a student with no chapter is undeletable by the person who wrote it.
    Verified by reading that branch rather than assuming. Building it now means c71 does
    not have to carry the fix as well, and there is no window where the bug is live.

    It also matters more than an ordinary gap because the promise is already in writing:
    /privacy is live and commits to deleting your content, and c69's purge job is built
    against the same promise. A delete the user cannot reach makes both untrue.

    AUTHORIZATION: the author always. A president may also delete, but only for a post
    that HAS a chapter — no chapter means no president, and inventing one would be the
    c85 mistake in a new place. Campus-wide removal is deliberately NOT here: that is
    moderation, it belongs behind POST /moderation/content/remove with its audit row and
    its own scoping, and folding it in would let a chapter officer quietly delete campus
    content with no record.
    """
    post = await session.get(models.Post, post_id)
    if post is None or post.deleted_at is not None:
        raise not_found("post_not_found")

    if post.author_id != user.id:
        if post.chapter_id is None:
            # Unreachable until c71; the branch exists so c71 does not have to add it.
            raise forbidden("not_author")
        result = await session.execute(
            select(models.Membership).where(
                models.Membership.chapter_id == post.chapter_id,
                models.Membership.user_id == user.id,
                models.Membership.status == "active",
            )
        )
        membership = result.scalar_one_or_none()
        if membership is None or membership.role != Role.president.value:
            raise forbidden("not_author_or_president")

    post.deleted_at = datetime.now(timezone.utc)
    await session.commit()


@router.put("/posts/{post_id}/likes")
async def like_post(
    post_id: uuid.UUID,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> PostLikeOut:
    """Like a post (idempotent upsert); scoped through the post's chapter."""
    await _post_with_membership(post_id, user, session)
    like = await session.get(models.PostLike, (post_id, user.id))
    if like is None:
        like = models.PostLike(post_id=post_id, user_id=user.id)
        session.add(like)
        try:
            await session.commit()
        except IntegrityError:
            # Concurrent double-tap raced us to insert the same (post_id, user_id)
            # like — rollback and treat as already-liked instead of a 500.
            await session.rollback()
            like = await session.get(models.PostLike, (post_id, user.id))
            if like is None:
                raise
        else:
            await session.refresh(like)
    return PostLikeOut.model_validate(like)


@router.delete("/posts/{post_id}/likes", status_code=204)
async def unlike_post(
    post_id: uuid.UUID,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Remove the caller's like (idempotent); scoped through the post's chapter."""
    await _post_with_membership(post_id, user, session)
    like = await session.get(models.PostLike, (post_id, user.id))
    if like is not None:
        await session.delete(like)
        await session.commit()


@router.get("/posts/{post_id}/comments")
async def list_comments(
    post_id: uuid.UUID,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[PostCommentOut]:
    """List a post's comments, oldest first, excluding soft-deleted ones."""
    await _post_with_membership(post_id, user, session)
    result = await session.execute(
        select(models.PostComment)
        .where(
            models.PostComment.post_id == post_id,
            models.PostComment.deleted_at.is_(None),
        )
        .order_by(models.PostComment.created_at)
    )
    return [PostCommentOut.model_validate(c) for c in result.scalars().all()]


@router.post("/posts/{post_id}/comments", status_code=201)
async def create_comment(
    post_id: uuid.UUID,
    body: PostCommentCreate,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> PostCommentOut:
    """Comment on a post; author membership checked through the post's chapter."""
    await _post_with_membership(post_id, user, session)
    comment = models.PostComment(post_id=post_id, author_id=user.id, body=body.body)
    session.add(comment)
    await session.commit()
    await session.refresh(comment)
    return PostCommentOut.model_validate(comment)
