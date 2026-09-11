"""Redirect-time entitlement re-check for signed media reads (board card c350).

THE GAP THIS CLOSES. A capability token (app.services.storage_service.mint_media_token)
is minted once, at feed-serve time, for whichever entitlement the caller held AT THAT
MOMENT - and GET /media/{token} used to just verify the token's signature and expiry
and redirect, with zero database access. So being removed from a chapter, suspended, or
having your post deleted had no effect on a token someone already held, for up to the
token's full TTL (2x the revocation window - see storage_service). This module is what
GET /media/{token} now calls BEFORE it signs a redirect, so a revoked viewer's already-
minted token stops working the next time they use it, not merely the next time one gets
minted.

KEPT SEPARATE FROM storage_service.py ON PURPOSE. storage_service is signing-only (HMAC
tokens, GCS signed urls) and deliberately does no DB access anywhere else in that module.
This module is authorization: it reuses the SAME rules the feed read/write routes already
enforce (app.middleware.org_scope.get_current_chapter_member's status predicate,
app.routers.feed._visible_audiences' org_actives rule, app.core.campus_access's verified-
campus rule) rather than re-deriving a fourth copy of any of them - see campus_access.py's
own module docstring for why a second hand-rolled copy of an authorization check is a bug
waiting to happen, not a style preference.

A VALID TOKEN'S viewer_id IS TRUSTWORTHY HERE BECAUSE IT IS TAMPER-PROOF, NOT BECAUSE THE
CALLER IS AUTHENTICATED. GET /media/{token} still takes no Authorization header (RN's
Image cannot send one - see storage_service.mint_media_token's docstring) and still does
not know who is making THIS particular HTTP request. What changed is that
verify_media_token already proved, via HMAC, that the token's viewer_id is the exact
value this backend minted it with - so re-deriving that viewer's CURRENT entitlement and
gating the redirect on it is sound, even though nothing about this request is otherwise
authenticated.

THE REVERSE LOOKUP (posts.media_urls = ANY(...)) IS UNINDEXED. Acceptable for this pass
per manager ruling (c350 R7) - a feed render fans out to 20+ of these, but the positive-
result memo below keeps most of them off the database entirely, and Postgres still does a
sequential scan fine at today's row counts. If GET /media/{token} p95 gets measurably hot
after this ships, this containment query is the first candidate for a GIN index under the
c364 EXPLAIN harness - do not add that index or its migration speculatively in this pass.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.config import get_settings
from app.core.campus_access import is_campus_verified

# Chapter-tier statuses that may still READ 'org' content - mirrors
# app.middleware.org_scope.get_current_chapter_member exactly ('removed' does not
# count; 'active'/'inactive' do). 'org_actives' additionally requires 'active', which
# mirrors app.routers.feed._visible_audiences.
_READABLE_MEMBER_STATUSES = ("active", "inactive")

# Positive-decision memo only (board c350 R5): denials are NEVER cached here, so a
# re-grant after a mistaken revocation is immediate. Keyed by (object_name, viewer_id)
# -> the unix timestamp the entry expires. Size-bounded LRU, same shape as
# storage_service._signed_read_cache's "do not let this grow forever" reasoning, but
# evicted by recency (OrderedDict + move_to_end) rather than by window, since there is
# no natural window boundary for an entitlement decision the way there is for a signed
# url.
_ENTITLEMENT_MEMO_MAX_ENTRIES = 4096
_entitlement_memo: "OrderedDict[tuple[str, str], float]" = OrderedDict()


def _memo_get(key: tuple[str, str], now: float) -> bool:
    expiry = _entitlement_memo.get(key)
    if expiry is None:
        return False
    if now >= expiry:
        # Expired: drop it rather than let a stale entry sit around forever between
        # requests for an object nobody re-checks again.
        del _entitlement_memo[key]
        return False
    _entitlement_memo.move_to_end(key)
    return True


def _memo_set(key: tuple[str, str], expiry: float) -> None:
    if key in _entitlement_memo:
        _entitlement_memo.move_to_end(key)
    _entitlement_memo[key] = expiry
    if len(_entitlement_memo) > _ENTITLEMENT_MEMO_MAX_ENTRIES:
        _entitlement_memo.popitem(last=False)  # evict the least-recently-used entry


def _reset_memo_for_tests() -> None:
    _entitlement_memo.clear()


def _canonical_url(object_name: str) -> str | None:
    """The exact string finalize_media_object() writes into posts.media_urls, or None
    if no bucket is configured (mirrors storage_service._bucket_name's fail-closed
    503 shape, but this module never signs anything, so it just denies instead)."""
    bucket = get_settings().media_bucket_name
    if not bucket:
        return None
    return f"https://storage.googleapis.com/{bucket}/{object_name}"


async def _load_owning_post(session: AsyncSession, object_name: str) -> models.Post | None:
    """The post that references `object_name` in its media_urls, live and not deleted.

    `= ANY(posts.media_urls)` - see the module docstring on why this is unindexed and
    accepted for now. Filtering deleted_at here (rather than after) means "no post
    found" and "the post that had it is gone" collapse into the same, correctly
    fail-closed, None - a caller that only checked "does a row exist" would get this
    right by accident; this makes it structural instead.
    """
    canonical = _canonical_url(object_name)
    if canonical is None:
        return None
    result = await session.execute(
        select(models.Post).where(
            models.Post.media_urls.any(canonical),
            models.Post.deleted_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def _decide(
    session: AsyncSession, object_name: str, viewer_id: str, *, now: datetime
) -> bool:
    """The real (uncached) entitlement decision - always a fresh DB read."""
    try:
        viewer_uuid = uuid.UUID(viewer_id)
    except ValueError:
        return False

    post = await _load_owning_post(session, object_name)
    if post is None:
        return False

    viewer = await session.get(models.User, viewer_uuid)
    if viewer is None or viewer.suspended_at is not None:
        return False

    if post.audience == "campus":
        if viewer.campus_id != post.campus_id:
            return False
        return is_campus_verified(viewer, now=now)

    # 'org' and 'org_actives': both chapter-scoped (ck_posts_org_requires_chapter
    # guarantees chapter_id is set for either), gated on the caller's OWN membership
    # row in that chapter.
    if post.chapter_id is None:  # pragma: no cover - guarded by the DB check constraint
        return False
    result = await session.execute(
        select(models.Membership).where(
            models.Membership.chapter_id == post.chapter_id,
            models.Membership.user_id == viewer_uuid,
            models.Membership.status.in_(_READABLE_MEMBER_STATUSES),
        )
    )
    membership = result.scalar_one_or_none()
    if membership is None:
        return False
    if post.audience == "org_actives" and membership.status != "active":
        return False
    return True


async def check_media_entitlement(
    session: AsyncSession, object_name: str, viewer_id: str, *, now: datetime | None = None
) -> bool:
    """True if `viewer_id` may still be redirected to `object_name` right now.

    Memoizes only a POSITIVE result, for media_entitlement_memo_seconds (Settings,
    default 60s - board c350 R5). This means a revocation's real-world effect can lag
    by up to that many seconds for a viewer who already had a positive decision
    cached; document that alongside the revocation window as the actual effective
    floor. A denial is NEVER cached, so a re-grant (e.g. an accidental removal
    reversed) is visible on the very next request with no wait.
    """
    current = now or datetime.now(timezone.utc)
    key = (object_name, viewer_id)
    if _memo_get(key, current.timestamp()):
        return True

    allowed = await _decide(session, object_name, viewer_id, now=current)
    if allowed:
        memo_seconds = get_settings().media_entitlement_memo_seconds
        _memo_set(key, current.timestamp() + memo_seconds)
    return allowed
