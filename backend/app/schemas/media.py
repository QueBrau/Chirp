"""Signed upload URL request/response for post media (board card c70)."""

from pydantic import BaseModel, ConfigDict, Field


class _Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class MediaUploadUrlRequest(_Schema):
    content_type: str = Field(min_length=1)
    byte_size: int = Field(gt=0)


class MediaUploadUrlOut(_Schema):
    upload_url: str
    # tmp/ location (c132) - fetchable for local preview only, never stored on a post.
    # The permanent url a post actually persists comes from finalize_media_object() at
    # create/update time, not from anything this response returns.
    preview_url: str
    object_name: str
    expires_in_seconds: int
