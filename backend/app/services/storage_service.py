"""Signed GCS upload URLs for post media (board card c70).

The backend never touches image bytes. It issues a short-lived, single-use signed PUT
URL scoped to one object; the client uploads directly to GCS. Cloud Run's own request
limit (32MB) would technically fit a photo either way, so that is not the reason to
avoid proxying — the reason is that proxying ties up a Cloud Run concurrency slot for
the duration of a slow phone upload and buys nothing this design does not already give
for free.

KEYLESS SIGNING, on purpose: this project has never stored a service-account key file
anywhere (DATABASE_URL lives in Secret Manager, not a downloaded credential), and a GCS
signing key would be exactly that kind of secret. Signing here goes through the IAM
Credentials API instead: `google.auth.default()` returns Cloud Run's attached identity,
and `blob.generate_signed_url(service_account_email=..., access_token=...)` uses that
identity's OWN access token to call IAM's signBlob on itself — which is what
`iam.serviceAccountTokenCreator` granted to the service's own account (not a second
account) is for. No key ever exists on disk.

READS ARE NOT SIGNED. MediaPostCard renders `<Image source={{uri}}>` with no custom
headers and no refresh logic (checked, not assumed — app-mobile/src/components/
MediaPostCard.tsx:408) — a post's photo has to be fetchable forever, and GCS signed
READ urls expire, which is the wrong tool for permanent content. The bucket is public-
read instead, so `public_url` is a fixed, deterministic `storage.googleapis.com/...`
string computed here with no GCS call at all — it works before the upload even happens
and never needs re-signing.

SIZE IS ENFORCED TWICE, not once. The app layer rejects an upload-URL REQUEST whose
claimed byte_size exceeds the cap before any signing happens — cheap, and it is the
only check that can produce a clean 400 with a specific reason. But a claimed size is
just a client's word, so the signed URL ALSO carries `X-Goog-Content-Length-Range` as a
required signed header (a real GCS XML API extension GCS itself enforces against the
bytes actually PUT, independent of anything the app claimed) — the second check does
not trust the first one to have been true.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

from fastapi import HTTPException

from app.config import get_settings

ALLOWED_CONTENT_TYPES: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10MB (board c70 decision, Aug 22)
UPLOAD_URL_TTL = timedelta(minutes=15)


@dataclass(frozen=True)
class SignedUpload:
    upload_url: str
    public_url: str
    object_name: str
    expires_in_seconds: int


def _bucket_name() -> str:
    """The configured bucket, or 503 if the deployment has not provisioned one yet.

    Same shape as stripe_service._secret_key(): unset config here means the infra
    step (bucket + IAM grants) has not landed, not that something is broken.
    """
    name = get_settings().media_bucket_name
    if not name:
        raise HTTPException(status_code=503, detail="media_not_configured")
    return name


_client = None


def _storage_client():
    """Lazy GCS client, created on first call — importing this module must never
    touch the network, same rule app.db.get_engine() follows for the DB engine."""
    global _client
    if _client is None:
        from google.cloud import storage  # deferred: only imported once actually used

        _client = storage.Client()
    return _client


def validate_upload_request(content_type: str, byte_size: int) -> str:
    """400 with a specific reason for a bad request; returns the file extension to use.

    Split out from generate_upload_url() so it runs (and can be unit-tested) with no
    GCS client involved at all — a bad content-type or an oversized request should
    never reach the network layer.
    """
    extension = ALLOWED_CONTENT_TYPES.get(content_type)
    if extension is None:
        raise HTTPException(status_code=400, detail="unsupported_content_type")
    if byte_size <= 0 or byte_size > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="file_too_large")
    return extension


def generate_upload_url(user_id: str, content_type: str, byte_size: int) -> SignedUpload:
    """Mint a signed PUT URL for one new object; the caller owns nothing until they
    actually upload to it and the resulting public_url ends up in a post's media_urls.

    object_name is namespaced by user_id (posts/{user_id}/{uuid}.{ext}) rather than by
    chapter or post, because at upload time there IS no post yet — creating the post is
    what happens AFTER a successful upload, using the public_url this returns. An
    object nobody ever attaches to a post is an accepted, cheap cleanup problem for a
    bucket lifecycle rule (infra config), not something this route tracks.
    """
    extension = validate_upload_request(content_type, byte_size)
    bucket_name = _bucket_name()

    object_name = f"posts/{user_id}/{uuid.uuid4().hex}.{extension}"
    bucket = _storage_client().bucket(bucket_name)
    blob = bucket.blob(object_name)

    import google.auth
    from google.auth.transport import requests as google_requests

    credentials, _ = google.auth.default()
    credentials.refresh(google_requests.Request())

    upload_url = blob.generate_signed_url(
        version="v4",
        expiration=UPLOAD_URL_TTL,
        method="PUT",
        content_type=content_type,
        headers={"X-Goog-Content-Length-Range": f"1,{MAX_UPLOAD_BYTES}"},
        service_account_email=credentials.service_account_email,
        access_token=credentials.token,
    )
    public_url = f"https://storage.googleapis.com/{bucket_name}/{object_name}"

    return SignedUpload(
        upload_url=upload_url,
        public_url=public_url,
        object_name=object_name,
        expires_in_seconds=int(UPLOAD_URL_TTL.total_seconds()),
    )
