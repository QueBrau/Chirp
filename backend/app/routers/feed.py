"""Chapter feed: posts CRUD (soft delete), likes, and comments."""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.core.errors import forbidden, not_found
from app.core.permissions import Role
from app.db import get_session
from app.middleware.auth import get_current_user
from app.middleware.org_scope import get_current_membership
from app.schemas.social import (
    PostCommentCreate,
    PostCommentOut,
    PostCreate,
    PostLikeOut,
    PostOut,
    PostUpdate,
)

router = APIRouter(tags=["feed"])


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
    _membership: models.Membership = Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
) -> list[PostOut]:
    """List the chapter's posts, newest first, excluding soft-deleted ones."""
    result = await session.execute(
        select(models.Post)
        .where(
            models.Post.chapter_id == chapter_id,
            models.Post.deleted_at.is_(None),
        )
        .order_by(models.Post.created_at.desc())
    )
    return [PostOut.model_validate(p) for p in result.scalars().all()]


@router.post("/chapters/{chapter_id}/posts", status_code=201)
async def create_post(
    chapter_id: uuid.UUID,
    body: PostCreate,
    membership: models.Membership = Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
) -> PostOut:
    """Create a post authored by the caller."""
    post = models.Post(
        chapter_id=chapter_id,
        author_id=membership.user_id,
        body=body.body,
        media_urls=body.media_urls,
    )
    session.add(post)
    await session.commit()
    await session.refresh(post)
    return PostOut.model_validate(post)


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
