"""Chapter feed + campus feed: posts CRUD (soft delete), likes, comments.

Post audience: 'org' (private to the chapter, never leaves /chapters/{id}/posts)
or 'campus' (also surfaces on GET /campuses/{id}/feed). Chosen by the author at
compose time (PostCreate.audience), defaulting to 'org' — see board Decisions log,
Aug 14. "For You" is a ranking over the same campus posts, not a third value.
"""
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.core.campus_access import (
    is_campus_verified,
    require_campus_member,
    require_verified_campus,
)
from app.core.errors import forbidden, not_found
from app.core.permissions import Role
from app.db import get_session
from app.middleware.auth import get_current_user
from app.middleware.org_scope import get_current_membership
from app.schemas.social import (
    CampusPostCreate,
    FeedPostOut,
    PostCommentCreate,
    PostCommentOut,
    PostCreate,
    PostLikeOut,
    PostOut,
    PostUpdate,
)
from app.services.storage_service import (
    finalize_media_object,
    media_capability_url,
    media_signing_enabled,
    object_name_from_stored_url,
    validate_media_object_names,
    validate_media_urls,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["feed"])


def _serialize_media_urls(
    media_urls: list[str] | None, viewer_id: uuid.UUID
) -> list[str] | None:
    """Turn STORED media urls into what this viewer should actually fetch (board c140).

    THE STORED VALUE IS NEVER REWRITTEN. posts.media_urls keeps holding the canonical
    permanent url finalize_media_object() assigned; this is a read-time transform only.
    That is a deliberate invariant, not an implementation detail - c153's reconciliation
    job diffs bucket objects against that column, and it stays correct precisely because
    nothing here writes back.

    Falls through unchanged when signing is not configured, which is the state every
    deployment is in until the c140 cutover flips the bucket private. That makes this
    change additive: it flips nothing on its own.

    Foreign-host urls (possible only on pre-c139 rows, when media_urls was unvalidated
    client input) are passed through unchanged and logged. They are not ours to sign, and
    making our bucket private does not affect them.
    """
    if not media_urls or not media_signing_enabled():
        return media_urls
    serialized: list[str] = []
    for url in media_urls:
        object_name = object_name_from_stored_url(url)
        if object_name is None:
            logger.warning(
                "media url is not in the configured bucket, serving it unsigned url=%s",
                url,
            )
            serialized.append(url)
            continue
        serialized.append(media_capability_url(object_name, str(viewer_id)))
    return serialized


def _post_out(post: models.Post, viewer_id: uuid.UUID) -> PostOut:
    """PostOut for a single post, with media_urls transformed for this viewer (c140)."""
    out = PostOut.model_validate(post)
    out.media_urls = _serialize_media_urls(out.media_urls, viewer_id)
    return out


def _finalize_media(user_id: uuid.UUID, media_object_names: list[str] | None) -> list[str]:
    """Validate + move every tmp/ reference to its permanent posts/ location (c132).

    Returns the resulting permanent public urls to store on the post - [] for none.
    Re-validated by validate_media_urls() as the defensive invariant on what this
    function itself just built (see that function's docstring), before the caller ever
    writes the result to a post row.
    """
    validate_media_object_names(str(user_id), media_object_names)
    if not media_object_names:
        return []
    urls = [finalize_media_object(str(user_id), name) for name in media_object_names]
    validate_media_urls(urls)
    return urls


async def _commit_or_log_orphaned_media(session: AsyncSession, media_urls: list[str]) -> None:
    """await session.commit(), logging any already-moved permanent media loudly before
    re-raising if the commit fails.

    c132: if media was moved to its permanent posts/ location above and THEN the DB
    commit fails (rare — e.g. an IntegrityError), the resulting object cannot be
    compensated away by deleting it - the service account's delete grant is
    IAM-conditioned to tmp/ only, on purpose (see finalize_media_object()'s docstring).
    The orphan is logged loudly instead, for a human to hand-delete if it ever actually
    happens, rather than trading away posts/ immutability to auto-clean a rare failure.
    """
    try:
        await session.commit()
    except Exception:
        for url in media_urls:
            logger.error(
                "post commit failed after media was already moved to permanent "
                "storage url=%s - now an orphan with no automatic cleanup, needs "
                "hand deletion",
                url,
            )
        raise


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
            # c109: the count has to agree with what list_comments will actually
            # return, or a card reads "3 comments" and opens to show two. Blocked
            # authors are excluded from both or from neither.
            ~select(models.UserBlock.blocker_id)
            .where(
                models.UserBlock.blocker_id == caller_id,
                models.UserBlock.blocked_id == models.PostComment.author_id,
            )
            .exists(),
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


def _feed_post_out(row: Any, viewer_id: uuid.UUID) -> FeedPostOut:
    """Build a FeedPostOut from one row of `_post_counts_select`.

    viewer_id is threaded in for c140's media serialization - capability urls are minted
    per viewer, so this cannot be built without knowing who is asking.
    """
    post, display_name, avatar_url, like_count, comment_count, liked_by_me = row
    return FeedPostOut(
        id=post.id,
        chapter_id=post.chapter_id,
        campus_id=post.campus_id,
        author_id=post.author_id,
        body=post.body,
        media_urls=_serialize_media_urls(post.media_urls, viewer_id),
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


async def _readable_post(
    post_id: uuid.UUID,
    user: models.User,
    session: AsyncSession,
) -> models.Post:
    """Load a live post the caller is allowed to interact with (like/comment).

    /posts/{post_id}/* routes have no chapter_id path param, so scoping is checked
    through the post itself: 404 if missing or soft-deleted, 403 otherwise.

    The rule follows the post's audience, not just its chapter:

    - 'org' - active membership in the post's chapter, unchanged. Org posts stay
      private to the org.
    - 'campus' - anyone VERIFIED on the post's campus, OR an active member of the
      chapter it came from. This is what the campus feed already promises: it serves
      these posts to every student on the campus, so requiring chapter membership to
      like one was never coherent. It 403'd every cross-chapter campus post, and since
      c71 a chapter-less post has no chapter to be a member of, so the old rule
      would have locked out even its own author. Pre-existing bug, fixed here
      because c71 cannot ship on top of it.

      The verification half is c88, landed on main while this branch sat. Merging
      the branch's original `user.campus_id == post.campus_id` would have quietly
      reopened the bypass campus_access.py exists to close: that column is writable
      by redeeming a forwarded invite code (c96/c105), so it answers "associated
      with" and not "proved an address at". Campus content needs the second.

    The membership half of that union is not redundant. users.campus_id and
    chapters.campus_id are independent columns with nothing keeping them in step,
    so a member whose own campus_id is unset or stale would otherwise be refused a
    like on a post their own chapter made. Caught by an existing test doing exactly
    that (test_counts_present_on_campus_feed_too).
    """
    post = await session.get(models.Post, post_id)
    if post is None or post.deleted_at is not None:
        raise not_found("post_not_found")

    if (
        post.audience == "campus"
        and user.campus_id == post.campus_id
        and is_campus_verified(user)
    ):
        return post

    result = await session.execute(
        select(models.Membership).where(
            models.Membership.chapter_id == post.chapter_id,
            models.Membership.user_id == user.id,
            models.Membership.status == "active",
        )
    )
    if result.scalar_one_or_none() is None:
        # A campus post the caller is neither on the campus of nor a member of the
        # authoring chapter: report the campus mismatch, which is the real reason.
        raise forbidden("not_your_campus" if post.audience == "campus" else "not_a_member")
    return post


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
    return [_feed_post_out(row, membership.user_id) for row in result.all()]


@router.post("/chapters/{chapter_id}/posts", status_code=201)
async def create_post(
    chapter_id: uuid.UUID,
    body: PostCreate,
    membership: models.Membership = Depends(get_current_membership),
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> PostOut:
    """Create a post authored by the caller, inside a chapter they belong to.

    `audience` defaults to 'org' (PostCreate schema default) so a client that
    omits it can never accidentally broadcast a chapter post campus-wide.

    THE CAMPUS GATE APPLIES HERE TOO, and this is the route it was missing (c88). The
    path is chapter-scoped, so it never went through `require_campus_member` and read
    as an org endpoint — but `audience='campus'` makes the post appear on
    GET /campuses/{id}/feed, so this is a campus-wide WRITE wearing a chapter URL.
    Gating the read alone would have left the door open facing the other way: an
    unverified member could not READ the campus feed but could still PUBLISH to it,
    which is the worse half of the pair.

    campus_id is taken from the CHAPTER, never from the author (c71), and the same
    chapter is what the gate is checked against. The two are normally the same, but
    the post belongs where it was made: it must not follow its author onto another
    campus feed later, and reading the campus off the user would let a mismatched
    pair through.

    The chapter is loaded for EVERY post, not just campus ones, because c71 stores
    campus_id on the row itself. That also means an org post naming a chapter that
    does not exist is now a 404 rather than an integrity error later.
    """
    chapter = await session.get(models.Chapter, chapter_id)
    if chapter is None:
        raise not_found("chapter_not_found")
    if body.audience == "campus":
        require_verified_campus(user, chapter.campus_id)
    media_urls = _finalize_media(membership.user_id, body.media_object_names)

    post = models.Post(
        chapter_id=chapter_id,
        campus_id=chapter.campus_id,
        author_id=membership.user_id,
        body=body.body,
        media_urls=media_urls or None,
        audience=body.audience,
        post_type=body.post_type,
        duration_sec=body.duration_sec,
    )
    session.add(post)
    await _commit_or_log_orphaned_media(session, media_urls)
    await session.refresh(post)
    return _post_out(post, membership.user_id)


@router.post("/campuses/{campus_id}/posts", status_code=201)
async def create_campus_post(
    campus_id: uuid.UUID,
    body: CampusPostCreate,
    user: models.User = Depends(require_campus_member),
    session: AsyncSession = Depends(get_session),
) -> PostOut:
    """Post to the campus feed without going through a chapter (c71).

    This is the route for a student who belongs to no org, which the Aug 11
    product decision says is a first-class Chirp user rather than an edge case.
    Members can use it too; it simply produces a post with no org attached.

    Two things are structural rather than checked, which is the point of having a
    separate route instead of relaxing the chapter one:

    - audience is hard-coded to 'campus'. CampusPostCreate has no audience field,
      so no request body can reach this code asking for an org post.
    - chapter_id is left NULL, so the post cannot claim to belong to an org the
      author may not be in. Posting into a chapter still requires the membership
      check on POST /chapters/{chapter_id}/posts, which is unchanged.

    Authorization is `require_campus_member`, the same c88 dependency the campus
    FEED read uses: you may only post to your own campus, and only with a current
    .edu verification. This route is the purest case of what c88 gates - a
    campus-wide write by someone with no org at all - so it must not keep the
    weaker `user.campus_id == campus_id` check this branch was written against.
    """
    media_urls = _finalize_media(user.id, body.media_object_names)
    post = models.Post(
        chapter_id=None,
        campus_id=campus_id,
        author_id=user.id,
        body=body.body,
        media_urls=media_urls or None,
        audience="campus",
        post_type=body.post_type,
        duration_sec=body.duration_sec,
    )
    session.add(post)
    await _commit_or_log_orphaned_media(session, media_urls)
    await session.refresh(post)
    return _post_out(post, user.id)


@router.get("/campuses/{campus_id}/feed")
async def list_campus_feed(
    campus_id: uuid.UUID,
    before: datetime | None = None,
    before_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    user: models.User = Depends(require_campus_member),
    session: AsyncSession = Depends(get_session),
) -> list[FeedPostOut]:
    """Campus-wide feed: only `audience = 'campus'` posts on this campus, newest
    first. 'org' posts NEVER appear here — that is the endpoint's core privacy
    guarantee (board Decisions log, Aug 14), and it is still enforced by the
    audience filter below, which no part of c71 touched.

    Filters posts.campus_id directly rather than joining chapters (c71). The join
    was an inner join on chapter_id, so once a chapter-less post can exist it would
    have been dropped from the feed silently — the exact failure mode where the
    post is created, returns 201, and never appears.

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
        .where(
            models.Post.campus_id == campus_id,
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
    return [_feed_post_out(row, user.id) for row in result.all()]


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
    media_urls: list[str] = []
    if body.media_object_names is not None:
        media_urls = _finalize_media(membership.user_id, body.media_object_names)
        post.media_urls = media_urls or None
    await _commit_or_log_orphaned_media(session, media_urls)
    return _post_out(post, membership.user_id)


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
    await _readable_post(post_id, user, session)
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
    await _readable_post(post_id, user, session)
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
    """List a post's comments, oldest first, excluding soft-deleted and blocked ones.

    c109: blocking was half-implemented. The block filter existed on the POST query
    (_post_counts_select's outerjoin on Post.author_id) and on yaks, but not here — so
    blocking someone hid their posts and left their comments sitting underneath
    everyone else's. The app makes this promise in words: yak/index.tsx's confirm
    dialog says "You won't see posts from this person again". Same family as c76, where
    /terms claimed removal powers the server did not have, and the fix there was making
    the claim true rather than softening the claim.

    READER PATHS ONLY. This filter must never be copied onto a moderation surface: a
    blocked author's reported comment is exactly the row a moderator needs to see, and
    c91's resolve flow would start hiding the evidence it exists to act on. Blocking is
    a reader preference; moderation is not. The moderation routes reach comments by id
    (remove_content) and never through this query, so that separation holds today —
    keep it that way.
    """
    await _readable_post(post_id, user, session)
    result = await session.execute(
        select(models.PostComment)
        .outerjoin(
            models.UserBlock,
            (models.UserBlock.blocked_id == models.PostComment.author_id)
            & (models.UserBlock.blocker_id == user.id),
        )
        .where(
            models.PostComment.post_id == post_id,
            models.PostComment.deleted_at.is_(None),
            models.UserBlock.blocker_id.is_(None),
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
    await _readable_post(post_id, user, session)
    comment = models.PostComment(post_id=post_id, author_id=user.id, body=body.body)
    session.add(comment)
    await session.commit()
    await session.refresh(comment)
    return PostCommentOut.model_validate(comment)
