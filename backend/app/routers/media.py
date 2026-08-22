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
    """Mint a signed PUT URL for one image.

    Not chapter-scoped, like /moderation/... — media is a cross-cutting utility, not
    chapter data, and at upload time there is no post (or chapter) to scope it to yet:
    the client uploads first, then creates the post with the returned public_url in
    media_urls. Any authenticated user may request one; the existing post-creation
    authorization is what actually decides whether the resulting post is allowed to
    exist, same as it always has.
    """
    upload = generate_upload_url(str(user.id), body.content_type, body.byte_size)
    return MediaUploadUrlOut(
        upload_url=upload.upload_url,
        public_url=upload.public_url,
        expires_in_seconds=upload.expires_in_seconds,
    )
