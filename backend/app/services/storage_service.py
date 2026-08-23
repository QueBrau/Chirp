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
read instead, so a permanent url is a fixed, deterministic `storage.googleapis.com/...`
string computed with no GCS call at all — it works the instant the object exists and
never needs re-signing.

SIZE IS ENFORCED TWICE, not once. The app layer rejects an upload-URL REQUEST whose
claimed byte_size exceeds the cap before any signing happens — cheap, and it is the
only check that can produce a clean 400 with a specific reason. But a claimed size is
just a client's word, so the signed URL ALSO carries `X-Goog-Content-Length-Range` as a
required signed header (a real GCS XML API extension GCS itself enforces against the
bytes actually PUT, independent of anything the app claimed) — the second check does
not trust the first one to have been true.

TMP-THEN-MOVE (board c132). generate_upload_url() mints an object under tmp/, not
posts/ — an upload nobody ever attaches to a post stays a tmp/ object forever, which is
what makes an age-based GCS lifecycle rule scoped to tmp/ ONLY safe: age is not
reference-ness (the original c132 finding, from a lifecycle rule that would have eaten
real photos), but "everything under tmp/ is provisional" is a true statement the bucket
layout itself enforces. finalize_media_object() moves one object from tmp/ to its
permanent posts/ location at post-create/update time — after that, `media_urls` on a
post is ENTIRELY server-assigned: nothing a client sends is ever written to that column
directly (validate_media_object_names() below only ever accepts a tmp/{caller's own
user_id}/ prefix, never a posts/ path), which is a real security property riding along
with the orphan-cleanup fix, not just incidental cleanup.

THE SERVICE ACCOUNT CANNOT DELETE FROM posts/, ON PURPOSE. Its delete grant (infra step,
manager-run) is IAM-conditioned to objects under tmp/ only. This means a bug, an
injected object_name, or a compromised route can never delete a published photo through
this identity — a stronger guarantee than "the code doesn't currently do that". The
consequence: if the GCS move for a post succeeds but the DB commit that follows fails
(rare — e.g. an IntegrityError), the resulting permanent object CANNOT be compensated
away by deleting it; finalize_media_object's caller must instead log the orphaned
object path loudly (greppable) for a human to hand-delete, and accept that this one rare
failure mode produces a benign unreferenced object rather than trade away posts/
immutability to avoid it. Do not add posts/ delete permission back to "fix" this.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta

from fastapi import HTTPException
from google.api_core.exceptions import NotFound

from app.config import get_settings

logger = logging.getLogger(__name__)

ALLOWED_CONTENT_TYPES: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10MB (board c70 decision, Aug 22)
UPLOAD_URL_TTL = timedelta(minutes=15)
TMP_PREFIX = "tmp"
PERMANENT_PREFIX = "posts"


@dataclass(frozen=True)
class SignedUpload:
    upload_url: str
    preview_url: str
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


MAX_MEDIA_URLS_PER_POST = 1  # alpha scope: matches the compose UI's single-photo cap
MAX_MEDIA_URL_LENGTH = 512


def validate_media_urls(media_urls: list[str] | None) -> None:
    """400/503 a post write whose media_urls isn't something THIS backend issued.

    Board c139 (security MEDIUM), tightened by c132: since c132, no client input ever
    reaches this function — a post's media_urls is entirely server-assigned by
    finalize_media_object() below, which only ever writes a permanent-prefix url it
    just created. This is retained anyway as a defensive invariant check on what the
    SERVER constructs before it gets committed, on the same "double-check what we just
    built" reasoning MAX_UPLOAD_BYTES's two enforcement points already follow — it
    would catch a bug in finalize_media_object() itself, not a malicious client.

    The fix is an exact-prefix allowlist: a media url is only ever legitimate if it is
    one THIS backend's finalize_media_object() could have produced, i.e.
    https://storage.googleapis.com/{configured bucket}/posts/... — checked against
    settings.media_bucket_name, never a hardcoded string, so a local/test deployment
    validates against its own bucket rather than prod's.

    No-op for None or an empty list — a text-only post must not require a configured
    media bucket to save. Only a post that actually CLAIMS media pays the cost of
    _bucket_name()'s existing fail-closed 503, on the same reasoning as
    generate_upload_url(): a media url cannot be judged legitimate against a bucket
    that does not exist, so this refuses with a clear reason rather than accept blind.
    """
    if not media_urls:
        return
    if len(media_urls) > MAX_MEDIA_URLS_PER_POST:
        raise HTTPException(status_code=400, detail="too_many_media_urls")
    bucket_name = _bucket_name()
    expected_prefix = f"https://storage.googleapis.com/{bucket_name}/{PERMANENT_PREFIX}/"
    for url in media_urls:
        if len(url) > MAX_MEDIA_URL_LENGTH:
            raise HTTPException(status_code=400, detail="media_url_too_long")
        if not url.startswith(expected_prefix):
            raise HTTPException(status_code=400, detail="invalid_media_url")


MAX_MEDIA_OBJECT_NAME_LENGTH = 256


def validate_media_object_names(user_id: str, media_object_names: list[str] | None) -> None:
    """400/503 a post write whose media_object_names isn't the caller's OWN tmp/ upload.

    Board c132: the create/update input shape moved from a client-supplied media_urls
    (validated, but still trusted to have been a real upload the caller made) to a
    client-supplied media_object_names — the tmp/ object_name generate_upload_url()
    returned. This function is the new attack surface's gate: without the user_id-scoped
    prefix check below, one caller could reference ANOTHER caller's tmp/ upload by
    object_name (they are opaque UUIDs, not secret, and the bucket is public-read) and
    have it moved into a post that never uploaded anything. Exact prefix
    tmp/{THIS caller's user_id}/ closes that - a media_object_name for any other user's
    tmp path, or a posts/ path directly, is rejected the same way an external host was
    rejected under the old media_urls check.

    No-op for None or an empty list, same reasoning as validate_media_urls: a text-only
    post, or an update clearing an existing photo, must not require a configured bucket.
    """
    if not media_object_names:
        return
    if len(media_object_names) > MAX_MEDIA_URLS_PER_POST:
        raise HTTPException(status_code=400, detail="too_many_media_urls")
    _bucket_name()  # 503 before trusting a tmp/ reference against no configured bucket
    expected_prefix = f"{TMP_PREFIX}/{user_id}/"
    for object_name in media_object_names:
        if len(object_name) > MAX_MEDIA_OBJECT_NAME_LENGTH:
            raise HTTPException(status_code=400, detail="media_url_too_long")
        if not object_name.startswith(expected_prefix):
            raise HTTPException(status_code=400, detail="invalid_media_url")


def generate_upload_url(user_id: str, content_type: str, byte_size: int) -> SignedUpload:
    """Mint a signed PUT URL for one new tmp/ object; the caller owns nothing until they
    actually upload to it, and the object is not part of any post until
    finalize_media_object() moves it to posts/ at create/update time (c132).

    object_name is namespaced by user_id (tmp/{user_id}/{uuid}.{ext}) rather than by
    chapter or post, because at upload time there IS no post yet. An upload nobody ever
    attaches to a post stays under tmp/ forever, which is what makes an age-based GCS
    lifecycle rule scoped to tmp/ safe (c132) — the object never migrates to a
    "permanent" prefix unless a real post claims it.
    """
    extension = validate_upload_request(content_type, byte_size)
    bucket_name = _bucket_name()

    object_name = f"{TMP_PREFIX}/{user_id}/{uuid.uuid4().hex}.{extension}"
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
    preview_url = f"https://storage.googleapis.com/{bucket_name}/{object_name}"

    return SignedUpload(
        upload_url=upload_url,
        preview_url=preview_url,
        object_name=object_name,
        expires_in_seconds=int(UPLOAD_URL_TTL.total_seconds()),
    )


def finalize_media_object(user_id: str, tmp_object_name: str) -> str:
    """Move one tmp/ upload to its permanent posts/ location; returns the new public url.

    Board c132. Called once per media_object_names entry, at post-create/update time,
    AFTER validate_media_object_names() has already confirmed the shape and ownership
    prefix — this function re-derives and re-checks the same prefix rather than trusting
    the caller ran that check, on the same "validate again where it actually matters"
    reasoning the double size-enforcement already follows.

    preserve_acl=False is required, not optional: the bucket is uniform-bucket-level-
    access (no per-object ACLs exist), and copy_blob's default preserve_acl=True calls
    the object ACL API, which a uniform-access bucket rejects outright. This only shows
    up against the real bucket - no fake client would surface it.

    A tmp_blob.delete() failure AFTER a successful copy is NOT fatal and does not raise:
    the post still gets its permanent url from the copy, and the leftover tmp/ object
    becomes the tmp/ lifecycle rule's job, the same safety net an abandoned upload
    already relies on. It is logged as a warning so a persistent delete-permission
    problem is still visible, just not blocking.

    A copy failure because the tmp object doesn't exist (never uploaded, or the tmp/
    lifecycle rule already reclaimed it) is a 400, not a 500 - the caller sent a
    reference to something that isn't there to move.

    THIS FUNCTION NEVER DELETES FROM posts/. The service account's delete grant is
    IAM-conditioned to tmp/ only (manager-run infra step) - deliberately, so no bug,
    injected object_name, or compromised route can ever delete a published photo
    through this identity. If the caller's own DB commit fails AFTER this function
    returns successfully, the resulting permanent object cannot be compensated away by
    deleting it; the caller must log the orphaned path loudly instead. Do not add
    posts/ delete permission back to "fix" that rare case - see the module docstring.
    """
    expected_prefix = f"{TMP_PREFIX}/{user_id}/"
    if not tmp_object_name.startswith(expected_prefix):
        raise HTTPException(status_code=400, detail="invalid_media_url")
    suffix = tmp_object_name[len(expected_prefix):]  # "{uuid}.{ext}"
    bucket_name = _bucket_name()
    bucket = _storage_client().bucket(bucket_name)
    tmp_blob = bucket.blob(tmp_object_name)
    permanent_object_name = f"{PERMANENT_PREFIX}/{user_id}/{suffix}"

    try:
        bucket.copy_blob(tmp_blob, bucket, permanent_object_name, preserve_acl=False)
    except NotFound:
        raise HTTPException(status_code=400, detail="media_upload_not_found")

    try:
        tmp_blob.delete()
    except Exception:
        logger.warning(
            "failed to delete moved tmp object object=%s (lifecycle rule will reclaim it)",
            tmp_object_name,
        )

    return f"https://storage.googleapis.com/{bucket_name}/{permanent_object_name}"
