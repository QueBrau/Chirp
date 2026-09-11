"""Signed upload URLs and capability-gated reads for post media (board cards c70, c140,
c350).

See app.services.storage_service for why uploads are client-direct-to-GCS rather than
proxied, why signing is keyless, and why reads are a capability url in front of a signed
redirect rather than a signed url handed straight to the client. See
app.services.media_entitlement for the redirect-time entitlement re-check c350 added.
"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.core import operational_signals
from app.core.rate_limits import MEDIA_UPLOAD_URL_LIMIT, limit_per_user
from app.db import get_session
from app.middleware.auth import get_current_user
from app.schemas.media import MediaUploadUrlOut, MediaUploadUrlRequest
from app.services.media_entitlement import check_media_entitlement
from app.services.storage_service import (
    generate_upload_url,
    signed_read_url,
    verify_media_token,
)

router = APIRouter(tags=["media"])


@router.post(
    "/media/upload-url",
    status_code=201,
    dependencies=[Depends(limit_per_user("media_upload_url", MEDIA_UPLOAD_URL_LIMIT))],
)
async def create_upload_url(
    body: MediaUploadUrlRequest,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MediaUploadUrlOut:
    """Mint a signed PUT URL for one image, under a tmp/ prefix (c132).

    Not chapter-scoped, like /moderation/... — media is a cross-cutting utility, not
    chapter data, and at upload time there is no post (or chapter) to scope it to yet:
    the client uploads to tmp/, then creates the post with the returned object_name in
    media_object_names — the post-create/update route moves the object to its permanent
    location and is what actually decides whether the resulting post is allowed to
    exist, same as it always has. Any authenticated user may request an upload url.
    """
    # c211: generate_upload_url() is a synchronous call that hits the network
    # (google.auth credentials.refresh + IAM signBlob - see storage_service's module
    # docstring for why signing is keyless). This process runs one uvicorn worker with
    # no --workers (Dockerfile) at concurrency=80, i.e. one event loop serving every
    # in-flight request - calling a blocking function directly here would stall all of
    # them for the duration of the network round trip. to_thread moves the call off
    # the loop onto a worker thread so only this request waits on it.
    user_id, uid = user.id, user.firebase_uid
    await session.commit()  # authentication reads own no capacity while signing
    session.expire_all()
    upload = await asyncio.to_thread(
        generate_upload_url, str(user_id), body.content_type, body.byte_size
    )
    await get_current_user(uid=uid, session=session)  # suspension/removal during signing
    return MediaUploadUrlOut(
        upload_url=upload.upload_url,
        preview_url=upload.preview_url,
        object_name=upload.object_name,
        expires_in_seconds=upload.expires_in_seconds,
    )


@router.get("/media/{token}")
async def read_media(
    token: str, session: AsyncSession = Depends(get_session)
) -> RedirectResponse:
    """Redirect a capability token to a short-lived signed GCS url for that object.

    DELIBERATELY UNAUTHENTICATED, and this is the one route in the app where that is a
    design decision rather than an oversight. React Native's `Image` cannot attach an
    Authorization header - MediaPostCard passes no headers and the api client's bearer
    token never reaches it - so a route that images are fetched from CANNOT authenticate
    its caller. The token itself is the capability, and it is only ever minted for a
    caller the feed routes already authorized (see mint_media_token's docstring).

    Anyone adding an auth dependency here will break every photo in the app, and the
    breakage will look like a caching bug rather than an auth change. Do not.

    ENTITLEMENT IS RE-CHECKED HERE NOW (board c350). A valid signature used to be
    treated as permanent proof of access for the token's whole TTL (up to 12h) -
    removal, suspension, a lapsed campus verification, or the post itself being
    deleted had no effect on a token someone already held. verify_media_token's
    viewer_id (HMAC-proven, not caller-asserted - see that function's docstring) is
    now re-checked against the CURRENT database state via
    app.services.media_entitlement before this route will sign a redirect. A denial
    is 403 media_access_revoked, not 410: this is not an expiry, the token is still
    within its lifetime and the signature still verifies - what changed is that the
    thing it was minted for is no longer true. The check memoizes only POSITIVE
    decisions for a short, separately configured TTL (media_entitlement_memo_seconds,
    default 60s), so a revocation's real-world effect can lag by up to that long on
    top of the token's own window - see media_entitlement.check_media_entitlement's
    docstring and DEPLOY.md's "Media revocation window" section for the honest bound.

    302, not 307/308: this is a "the thing you want is over there right now" redirect
    whose target legitimately changes between windows, which is exactly what 302's
    non-permanent semantics mean. A 308 would invite intermediaries to cache the mapping
    permanently, and the target is anything but permanent.

    Cache-Control is `private, no-store` (board c350; used to be cacheable for the
    remaining life of the memo window). `private` because a capability url is
    per-viewer by construction and must never land in a shared/proxy cache; `no-store`
    so a client that respects Cache-Control cannot keep replaying a REDIRECT past a
    revocation without hitting this route - and therefore the entitlement check -
    again. This closes the gap only for clients whose caching layer honors the header;
    it does not and cannot reach a native Image component's own byte cache once a
    photo has already been fetched and decoded - see storage_service's signed-reads
    section and media_entitlement's module docstring for what remains a documented,
    accepted residual gap rather than something this backend-only change can close.
    """
    object_name, viewer_id = verify_media_token(token)
    allowed = await check_media_entitlement(session, object_name, viewer_id)
    if not allowed:
        # Ids only, never the token or object_name - see operational_signals' own
        # module docstring on why observe() takes no payload argument at all.
        operational_signals.observe("media_access_revoked")
        raise HTTPException(status_code=403, detail="media_access_revoked")
    # c211: same reasoning as create_upload_url() above - signed_read_url() is
    # synchronous and, on a memo miss, makes the same google.auth refresh + IAM
    # signBlob network call. Offload to a worker thread so a cold cache entry cannot
    # stall the single event loop this process serves every other in-flight request
    # on. The signed-url memo (storage_service._signed_read_cache) keeps most calls
    # off this path entirely; see that dict's own lock for what changed once misses
    # can now happen concurrently from multiple worker threads.
    target = await asyncio.to_thread(signed_read_url, object_name)
    return RedirectResponse(
        target,
        status_code=302,
        headers={"Cache-Control": "private, no-store"},
    )
