"""Signed upload URLs and capability-gated reads for post media (board cards c70, c140).

See app.services.storage_service for why uploads are client-direct-to-GCS rather than
proxied, why signing is keyless, and why reads are a capability url in front of a signed
redirect rather than a signed url handed straight to the client.
"""

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse

from app import models
from app.middleware.auth import get_current_user
from app.schemas.media import MediaUploadUrlOut, MediaUploadUrlRequest
from app.services.storage_service import (
    MEDIA_TOKEN_WINDOW,
    generate_upload_url,
    signed_read_url,
    verify_media_token,
)

router = APIRouter(tags=["media"])


@router.post("/media/upload-url", status_code=201)
async def create_upload_url(
    body: MediaUploadUrlRequest,
    user: models.User = Depends(get_current_user),
) -> MediaUploadUrlOut:
    """Mint a signed PUT URL for one image, under a tmp/ prefix (c132).

    Not chapter-scoped, like /moderation/... — media is a cross-cutting utility, not
    chapter data, and at upload time there is no post (or chapter) to scope it to yet:
    the client uploads to tmp/, then creates the post with the returned object_name in
    media_object_names — the post-create/update route moves the object to its permanent
    location and is what actually decides whether the resulting post is allowed to
    exist, same as it always has. Any authenticated user may request an upload url.
    """
    upload = generate_upload_url(str(user.id), body.content_type, body.byte_size)
    return MediaUploadUrlOut(
        upload_url=upload.upload_url,
        preview_url=upload.preview_url,
        object_name=upload.object_name,
        expires_in_seconds=upload.expires_in_seconds,
    )


@router.get("/media/{token}")
async def read_media(token: str) -> RedirectResponse:
    """Redirect a capability token to a short-lived signed GCS url for that object.

    DELIBERATELY UNAUTHENTICATED, and this is the one route in the app where that is a
    design decision rather than an oversight. React Native's `Image` cannot attach an
    Authorization header - MediaPostCard passes no headers and the api client's bearer
    token never reaches it - so a route that images are fetched from CANNOT authenticate
    its caller. The token itself is the capability, and it is only ever minted for a
    caller the feed routes already authorized (see mint_media_token's docstring).

    Anyone adding an auth dependency here will break every photo in the app, and the
    breakage will look like a caching bug rather than an auth change. Do not.

    302, not 307/308: this is a "the thing you want is over there right now" redirect
    whose target legitimately changes between windows, which is exactly what 302's
    non-permanent semantics mean. A 308 would invite intermediaries to cache the mapping
    permanently, and the target is anything but permanent.

    Cache-Control is set to the remaining life of the memo window so a client that caches
    the REDIRECT does not re-ask us on every image load. `private` because a capability
    url is per-viewer by construction and must never land in a shared/proxy cache.
    """
    object_name = verify_media_token(token)
    target = signed_read_url(object_name)
    return RedirectResponse(
        target,
        status_code=302,
        headers={
            "Cache-Control": f"private, max-age={int(MEDIA_TOKEN_WINDOW.total_seconds())}",
        },
    )
