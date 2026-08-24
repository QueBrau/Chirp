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

READS: SEE THE SIGNED READS SECTION AT THE BOTTOM OF THIS FILE (board c140). This
docstring used to say reads are never signed, because MediaPostCard renders
`<Image source={{uri}}>` with no custom headers and no refresh logic (still true —
app-mobile/src/components/MediaPostCard.tsx:408) and GCS signed READ urls expire, which
is the wrong tool for permanent content. That reasoning was right about GCS signed urls
and wrong about the conclusion: it only ever considered putting the GCS url itself in the
API response. c140 keeps the bucket private and puts an app-owned, quantized-expiry
CAPABILITY url in the response instead, with GCS signing hidden behind a redirect. What
is stored in `posts.media_urls` is unchanged — still the fixed, deterministic
`storage.googleapis.com/...` string this section always described. Only what gets
SERIALIZED to a client differs.

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
object path loudly (greppable), and accept that this one rare failure mode produces a
benign unreferenced object rather than trade away posts/ immutability to avoid it. Do
not add posts/ delete permission back to "fix" this.

UNREFERENCED posts/ OBJECTS ARE RECLAIMED OUT OF BAND (board c153), which is what keeps
the paragraph above a bounded trade rather than a permanent leak. PATCH clearing or
replacing a photo detaches the old permanent object for exactly the same reason — this
identity cannot delete it — so app.jobs.media_reconcile diffs posts/ against every url
the posts table actually references and removes what nothing points at, running as a
SEPARATE service account whose delete grant is IAM-conditioned to posts/. The runtime
identity's tmp/-only condition is deliberately left alone (manager decision on c153):
the ability to delete a published photo belongs to a scheduled job, never to the account
serving requests.

THIS ALMOST BROKE THE COPY ITSELF, found live against the real bucket (a fake client
cannot surface it): GCS requires storage.objects.delete on the DESTINATION for an
UNCONDITIONAL copy/write, even though it is only ever creating a new object there —
because an unconditional write CAN overwrite an existing object, and overwrite implies
delete. finalize_media_object() passes if_generation_match=0 on the copy specifically to
avoid needing that permission: it asserts "the destination must not exist," which only
requires create. This is not an unrelated workaround; it is the correct way to express
"always creates a new, never-before-seen object" to GCS, and it happens to also make
no-overwrite a server-enforced guarantee instead of a probabilistic one from UUID names.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

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

    preserve_acl is DELIBERATELY LEFT AT ITS DEFAULT (True) - the opposite of what an
    earlier version of this function did, and the opposite of what the parameter's own
    docstring reads like at a glance. Ground truth is the IMPLEMENTATION, not the
    docstring: copy_blob's body ends with `if not preserve_acl: new_blob.acl.save(...)`
    - the ACL API call only happens when preserve_acl is FALSE. True (the default) skips
    it entirely. On a uniform-bucket-level-access bucket, that acl.save call 403s (there
    is no per-object ACL to touch), so passing preserve_acl=False HERE, as this function
    used to, made copy_blob's own destination copy succeed and then fail on the ACL call
    immediately after - manager's real-bucket E2E confirmed this precisely, via SA
    impersonation: the exact conditional copyTo authorized cleanly (412 destination-
    exists, not 403), proving the copy itself was never the problem. Do not set
    preserve_acl=False again without re-reading copy_blob's source first.

    if_generation_match=0 on the copy is ALSO required, not optional, and for a subtler
    reason a fake client cannot surface either: it applies to the DESTINATION generation
    (confirmed against the installed client's own docstring, not assumed), and an
    UNCONDITIONAL copy destination requires storage.objects.delete on posts/ even though
    it is only ever creating a new object there - because an unconditional write CAN
    overwrite an existing one, and overwrite implies delete. Our service account
    deliberately has create-only on posts/ (see below), so an unconditional copy_blob
    was refused with a 403 on the real bucket during the manager's E2E pass, even though
    every fake-backed test here passed. if_generation_match=0 asserts "the destination
    must not already exist," which only requires create - and, as a bonus, turns
    no-overwrite from a probabilistic property of UUID naming into a server-enforced
    guarantee.

    A tmp_blob.delete() failure AFTER a successful copy is NOT fatal and does not raise:
    the post still gets its permanent url from the copy, and the leftover tmp/ object
    becomes the tmp/ lifecycle rule's job, the same safety net an abandoned upload
    already relies on. It is logged as a warning so a persistent delete-permission
    problem is still visible, just not blocking.

    A copy failure because the tmp object doesn't exist (never uploaded, or the tmp/
    lifecycle rule already reclaimed it) is a 400, not a 500 - the caller sent a
    reference to something that isn't there to move. Any OTHER copy failure (permission,
    precondition, transient GCS error) is a 502 media_finalize_failed, not a bare 500 -
    the manager's E2E pass hit exactly this as an unhandled exception before this catch
    existed.

    THIS FUNCTION NEVER DELETES FROM posts/. The service account's delete grant is
    IAM-conditioned to tmp/ only (manager-run infra step) - deliberately, so no bug,
    injected object_name, or compromised route can ever delete a published photo
    through this identity. If the caller's own DB commit fails AFTER this function
    returns successfully, the resulting permanent object cannot be compensated away by
    deleting it; the caller must log the orphaned path loudly instead. Do not add
    posts/ delete permission back to "fix" that rare case - see the module docstring.
    The same constraint is why PATCH cannot delete the photo it replaces; both orphans
    are collected later by app.jobs.media_reconcile (c153), under a different account.
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
        bucket.copy_blob(
            tmp_blob,
            bucket,
            permanent_object_name,
            if_generation_match=0,
        )
    except NotFound:
        raise HTTPException(status_code=400, detail="media_upload_not_found")
    except Exception as exc:
        logger.error(
            "media finalize copy failed tmp_object=%s error=%s",
            tmp_object_name,
            type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail="media_finalize_failed") from exc

    try:
        tmp_blob.delete()
    except Exception:
        logger.warning(
            "failed to delete moved tmp object object=%s (lifecycle rule will reclaim it)",
            tmp_object_name,
        )

    return f"https://storage.googleapis.com/{bucket_name}/{permanent_object_name}"


# ---------------------------------------------------------------------------
# SIGNED READS (board c140)
#
# The module docstring above says "READS ARE NOT SIGNED" and explains why: a post's photo
# has to be fetchable forever and GCS signed READ urls expire. That reasoning was correct
# about GCS signed urls and wrong about the conclusion, because it only considered putting
# the GCS signed url in the API response. c140's ruling: keep the bucket private, and put
# an APP-OWNED capability url in the response instead, with the GCS signing hidden behind
# a redirect where its instability never reaches the client.
#
# WHY NOT JUST PUT A GCS SIGNED URL IN THE RESPONSE. It cannot be made byte-stable, and
# byte-stability is the whole ballgame for image caching. generate_signed_url_v4 derives
# X-Goog-Date from get_v4_now_dtstamps() -> _NOW(_UTC) -> datetime.datetime.now, with no
# public parameter to pin it (google-cloud-storage 3.13.1, read from the installed body,
# not the docs - the c132 standing lesson). There IS a private _request_timestamp kwarg,
# comment-marked "for testing only"; prod behavior does not get built on that. So every
# call returns a DIFFERENT string for the same object and the same expiry.
#
# That matters because app-mobile renders post media with React Native's built-in Image
# (app-mobile/src/components/MediaPostCard.tsx:408 - plain `Image` from "react-native", no
# expo-image, no cache/headers props), and RN keys its image cache on the URL STRING:
# iOS RCTCacheKeyForImage(imageTag, ...) with imageTag = the source url, looked up AND
# stored against the ORIGINAL request url (RCTImageLoader.mm:551 and :870-871); Android
# builds its Fresco request from imageSource.uri. A url that changes every serialization
# is a cache key that changes every serialization - every feed load would re-download
# every photo.
#
# THE FIX IS QUANTIZED EXPIRY. We own this token format, so unlike the GCS client we can
# floor the expiry to a window. Every token minted inside one window, for one
# (object, viewer), is byte-identical - so the cache key is stable and the image cache
# keeps working exactly as it does today.
# ---------------------------------------------------------------------------

MEDIA_TOKEN_WINDOW = timedelta(hours=6)
# TTL is deliberately 2x the window, not 1x: a token minted at the very END of a window
# would otherwise be seconds from expiring. 2x means the worst-case token still has a full
# window of validity left, which covers "app left open overnight" with no client refresh
# path - and there is no such path to lean on (MediaPostCard has no expiry/refresh logic).
MEDIA_TOKEN_TTL = 2 * MEDIA_TOKEN_WINDOW
# The GCS signed url BEHIND the redirect. Only has to outlive its memo entry, which lives
# at most one window - 2x the window again, for the same margin reason.
READ_URL_TTL = MEDIA_TOKEN_TTL


def _signing_secret() -> bytes | None:
    """The capability-token HMAC key, or None if signed reads are not enabled here.

    None is a real, expected state (see config.media_signing_secret) and callers must
    fall back to emitting the stored url unchanged rather than failing - the bucket is
    still public-read until the c140 cutover.
    """
    secret = get_settings().media_signing_secret
    return secret.encode("utf-8") if secret else None


def media_signing_enabled() -> bool:
    """True when this deployment can mint capability urls (secret AND public base url).

    Both are required: without the base url there is nothing to build an ABSOLUTE url
    from, and RN's Image needs an absolute one.
    """
    return _signing_secret() is not None and bool(get_settings().app_public_base_url)


def _window_expiry(now: datetime) -> int:
    """Quantized expiry as a unix timestamp - the reason capability urls are cacheable.

    Floors `now` to the current MEDIA_TOKEN_WINDOW boundary and adds MEDIA_TOKEN_TTL, so
    every call inside one window returns the SAME number. Since the expiry is the only
    time-varying part of the token payload, identical expiry means an identical token
    string, which means an identical RN image-cache key. Do not "improve" this into a
    rolling now+TTL: that reintroduces exactly the per-request churn this whole design
    exists to avoid, and it would do so invisibly - the urls would still work, they would
    just quietly stop being cache hits.
    """
    window = int(MEDIA_TOKEN_WINDOW.total_seconds())
    window_start = (int(now.timestamp()) // window) * window
    return window_start + int(MEDIA_TOKEN_TTL.total_seconds())


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def mint_media_token(object_name: str, viewer_id: str, *, now: datetime | None = None) -> str:
    """Mint a capability token for one object and one viewer, expiring on a window boundary.

    WHAT THIS TOKEN IS, PRECISELY - because "signed url" invites the wrong assumption.
    It is a BEARER capability, not an authenticated request. RN's Image cannot send an
    Authorization header (MediaPostCard passes no headers, and the api client's bearer
    token never reaches it), so the read route cannot authenticate its caller and does not
    try. Whoever holds the url can fetch it until it expires.

    The actual authorization check therefore happens where it already happened before this
    card: at feed-serve time, in list_posts (get_current_membership) and list_campus_feed
    (require_campus_member). A token is only ever MINTED for someone already entitled to
    see that post. That is the security property; the token just carries it to a component
    that cannot present credentials.

    viewer_id is included for leak ATTRIBUTION and per-viewer revocation. It deliberately
    does NOT add enforcement - see above - and nothing downstream should be written as if
    it does.
    """
    secret = _signing_secret()
    if secret is None:  # pragma: no cover - guarded by media_signing_enabled()
        raise HTTPException(status_code=503, detail="media_not_configured")
    expiry = _window_expiry(now or datetime.now(timezone.utc))
    payload = f"{object_name}\x00{viewer_id}\x00{expiry}".encode("utf-8")
    signature = hmac.new(secret, payload, hashlib.sha256).digest()[:16]
    return f"{_b64(payload)}.{_b64(signature)}"


def verify_media_token(token: str, *, now: datetime | None = None) -> str:
    """Return the object name a valid token refers to; 403 on tampering, 410 on expiry.

    Expiry is 410 rather than 403 on purpose: the two are operationally different and get
    confused otherwise. 410 means "this url was genuine and has aged out" - the client
    should refetch the feed to get a fresh one. 403 means the signature did not verify,
    i.e. someone edited a token. Collapsing both into 403 would make a normal, expected
    lifecycle event indistinguishable from an attack in the logs.
    """
    secret = _signing_secret()
    if secret is None:
        raise HTTPException(status_code=503, detail="media_not_configured")
    try:
        encoded_payload, encoded_signature = token.split(".", 1)
        payload = _unb64(encoded_payload)
        signature = _unb64(encoded_signature)
    except (ValueError, binascii.Error):
        raise HTTPException(status_code=403, detail="invalid_media_token")

    expected = hmac.new(secret, payload, hashlib.sha256).digest()[:16]
    # compare_digest, not ==, so a forged token cannot be refined byte-by-byte by timing.
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=403, detail="invalid_media_token")

    try:
        object_name, _viewer_id, expiry_raw = payload.decode("utf-8").split("\x00")
        expiry = int(expiry_raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=403, detail="invalid_media_token")

    current = now or datetime.now(timezone.utc)
    if int(current.timestamp()) >= expiry:
        raise HTTPException(status_code=410, detail="media_token_expired")

    # A VALID SIGNATURE IS NOT A VALID PATH. The token proves we minted it; it does not
    # prove what we minted it FOR is still something this route should serve. Re-check the
    # permanent prefix so that even a bug that minted a tmp/ (or any other) path cannot be
    # redirected to through here - same "validate again where it actually matters"
    # reasoning finalize_media_object() already applies to its own caller's work.
    if not object_name.startswith(f"{PERMANENT_PREFIX}/"):
        raise HTTPException(status_code=403, detail="invalid_media_token")
    return object_name


def media_capability_url(object_name: str, viewer_id: str) -> str:
    """The absolute url that goes into a PostOut's media_urls, in place of the stored one."""
    base = (get_settings().app_public_base_url or "").rstrip("/")
    return f"{base}/media/{mint_media_token(object_name, viewer_id)}"


def object_name_from_stored_url(url: str) -> str | None:
    """Map a STORED media_urls value to its bucket object name, or None if it is foreign.

    Three url shapes can be in that column, because the write path changed twice - see
    board c140's design doc for the full history, but in short:

      c70 -> c139   media_urls was client-supplied and COMPLETELY UNVALIDATED, so a row
                    can point at an arbitrary external host we do not own.
      c139 -> c132  client-supplied, validated against the BUCKET ROOT only - so a row can
                    reference a real object in our bucket in a non-canonical shape.
      c132 -> now   server-assigned, always canonical.

    Returning None means "not ours" and the caller must pass the url through UNCHANGED:
    a foreign host is unaffected by making our bucket private, so signing it is both
    impossible and unnecessary.

    Returning a name means the url references a real object in OUR bucket, and it MUST be
    signed even when the shape is legacy - passing an alternate-form url through would
    leave it pointing at a bucket that is about to stop answering anonymous requests, i.e.
    a broken photo. This is the branch that only exists because of that distinction, and
    it is the one most likely to be absent from any given deployment, so it is tested with
    an explicit fixture rather than left theoretical.
    """
    bucket_name = get_settings().media_bucket_name
    if not bucket_name:
        return None
    path = url.split("?", 1)[0]  # legacy rows may carry query params; the object is the path

    # JSON API form FIRST: its prefix starts with the same host as the canonical form, so
    # checking canonical first would mis-slice it whenever the bucket is literally named
    # "storage". Object name here is PERCENT-ENCODED (posts%2Fuser%2Fuuid.jpg) and must be
    # decoded EXACTLY ONCE - decoding twice would corrupt any object name containing a
    # literal '%'.
    json_api_prefix = f"https://storage.googleapis.com/storage/v1/b/{bucket_name}/o/"
    if path.startswith(json_api_prefix):
        return unquote(path[len(json_api_prefix):])

    # Canonical XML path form (what finalize_media_object writes today).
    canonical_prefix = f"https://storage.googleapis.com/{bucket_name}/"
    if path.startswith(canonical_prefix):
        return path[len(canonical_prefix):]

    # Authenticated-browser-download form. Unlike the JSON API form this arrives with the
    # path UNENCODED, so it takes a different route to the same object name - do not
    # "simplify" the two branches into one shared unquote().
    console_prefix = f"https://storage.cloud.google.com/{bucket_name}/"
    if path.startswith(console_prefix):
        return path[len(console_prefix):]

    return None


_signed_read_cache: dict[tuple[str, int], str] = {}


def signed_read_url(object_name: str, *, now: datetime | None = None) -> str:
    """A signed GCS GET url for one object, memoized per (object, window).

    THIS MEMO IS LOAD-BEARING FOR TWO SEPARATE REASONS. Removing it does not just make
    things slower - it can silently break image caching on one platform. Do not treat it
    as an optional optimization:

    1. COST. Signing goes through IAM's signBlob over the NETWORK (see generate_upload_url
       for why signing is keyless here). Without the memo, every single image request from
       every device would add an IAM round trip before the redirect even leaves.

    2. REDIRECT-TARGET STABILITY. iOS is provably safe either way - it keys its image cache
       on the ORIGINAL request url, so where the 302 points is irrelevant (verified in the
       installed RN source: RCTImageLoader.mm:551 lookup, :870-871 store). Android's Fresco
       is expected to behave the same way via ImageRequest.getSourceUri(), but that is
       standard-library behavior we could NOT verify in-tree (Fresco is a Maven dependency,
       not vendored). If it ever keyed on the FINAL url instead, an unmemoized signer would
       hand out a different target on every request and Android would cache-miss every
       time. The memo makes the target stable within a window, so that failure mode cannot
       happen regardless of which url Fresco keys on.

    Bound, stated honestly: the memo is per-process, so different Cloud Run instances can
    hold different signed urls for the same (object, window). Cross-instance determinism
    would need a pinned signing timestamp, which the client only exposes through a private
    testing-only kwarg (see this section's header). So reason 2 degrades to
    per-instance-stable in the worst case, never to broken.
    """
    current = now or datetime.now(timezone.utc)
    window = int(MEDIA_TOKEN_WINDOW.total_seconds())
    window_index = int(current.timestamp()) // window
    key = (object_name, window_index)
    cached = _signed_read_cache.get(key)
    if cached is not None:
        return cached

    # Drop other windows' entries rather than letting the dict grow forever. Same-window
    # entries for OTHER objects must survive - they are the cache doing its job.
    for stale in [k for k in _signed_read_cache if k[1] != window_index]:
        del _signed_read_cache[stale]

    bucket_name = _bucket_name()
    blob = _storage_client().bucket(bucket_name).blob(object_name)

    import google.auth
    from google.auth.transport import requests as google_requests

    credentials, _ = google.auth.default()
    credentials.refresh(google_requests.Request())

    url = blob.generate_signed_url(
        version="v4",
        expiration=READ_URL_TTL,
        method="GET",
        service_account_email=credentials.service_account_email,
        access_token=credentials.token,
    )
    _signed_read_cache[key] = url
    return url
