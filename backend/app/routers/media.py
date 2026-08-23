"""Signed upload URLs for post media (board card c70). See app.services.storage_service
for why this is client-direct-to-GCS rather than a proxy, why signing is keyless, and
why reads are never signed.
"""

from fastapi import APIRouter, Depends

from app import models
from app.middleware.auth import get_current_user
from app.schemas.media import MediaUploadUrlOut, MediaUploadUrlRequest
from app.services.storage_service import generate_upload_url

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
