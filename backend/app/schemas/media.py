"""Signed upload URL request/response for post media (board card c70)."""

from pydantic import BaseModel, ConfigDict, Field


class _Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class MediaUploadUrlRequest(_Schema):
    content_type: str = Field(min_length=1)
    byte_size: int = Field(gt=0)


class MediaUploadUrlOut(_Schema):
    upload_url: str
    public_url: str
    expires_in_seconds: int
