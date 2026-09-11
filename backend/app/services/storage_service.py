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

THE DESTINATION WRITE IS CONDITIONAL, NOT UNCONDITIONAL, for the same reason a plain
copy would have needed it: GCS requires storage.objects.delete on the DESTINATION for an
UNCONDITIONAL write, even though it is only ever creating a new object there — because
an unconditional write CAN overwrite an existing object, and overwrite implies delete.
finalize_media_object() passes if_generation_match=0 on the upload specifically to avoid
needing that permission: it asserts "the destination must not exist," which only
requires create. This is not an unrelated workaround; it is the correct way to express
"always creates a new, never-before-seen object" to GCS, and it happens to also make
no-overwrite a server-enforced guarantee instead of a probabilistic one from UUID names.
This was originally learned against a plain bucket.copy_blob() (a fake client cannot
surface an IAM permission requirement); it applies unchanged now that the destination
write is a re-encoded derivative's bytes rather than a copy of the source object.

DERIVATIVES, NOT COPIES (board c374). finalize_media_object() used to be a straight
bucket.copy_blob() — the tmp/ bytes became the posts/ (or avatars/) bytes, unexamined.
It now downloads the tmp/ object, decodes it with Pillow, and re-encodes what it writes
to the permanent prefix: EXIF-stripped, downscaled to a 1600px long edge, always JPEG at
quality 82. Three separate reasons converge on one pipeline rather than three bolt-ons:

  - PRIVACY. A phone photo's EXIF routinely carries GPS coordinates. Those coordinates
    have no reason to travel from "someone's camera roll" to "public post byte-for-byte,
    forever" and every reason not to. The strip is unconditional and not configurable.
  - BOUNDED COST. A single unresized photo from a modern phone can be tens of megapixels;
    every post/avatar re-serve was paying for that in bandwidth and in RN's decode cost
    on a phone that will never render it above a feed-card width. 1600px long-edge covers
    every layout this app has today with headroom, not just the current card size.
  - A CONSISTENT, BOUNDED FORMAT. Whatever a client uploaded (jpeg/png/webp, per
    ALLOWED_CONTENT_TYPES), what leaves this function is always one format at one quality
    setting. Nothing downstream needs to branch on it, and Pillow's own decode failing on
    a byte string that merely CLAIMED to be an image at upload time is a free, incidental
    content-type check this design gets without adding one on purpose.

A HEADER pixel-count check (DERIVATIVE_MAX_SOURCE_PIXELS_JPEG / _OTHER, checked before
any pixel data is decoded) is load-bearing precisely because MAX_UPLOAD_BYTES bounds
compressed size, not decoded pixel count: a small, highly-compressible file (a
solid-color PNG, for instance) can still decode to gigapixels well within the 10MB cap.
Decoding is now real work this process does in-process, so this check - and, for JPEG,
image.draft() DCT-scaling the decode itself - is what stands between an accepted upload
and an OOM on the request path, not a defense against a threat that only mattered once.

THIS REJECTS BEFORE image.load() RUNS, not after decoding the full image just to throw
it away. An earlier version of this check ran AFTER image.load() (comparing the decoded
size against Pillow's own Image.MAX_IMAGE_PIXELS) - which meant the process paid the
exact memory cost of decoding the oversized image before rejecting it, on a service
(chirp-api) running at 512Mi with concurrency=80, where one such decode can OOM the
instance and kill every other in-flight request on it. Checking image.size from the
lazy Image.open() header, before load(), closes that: a pathological image is rejected
before it is ever fully decoded. Pillow's own DecompressionBombError (which only raises
above 2x MAX_IMAGE_PIXELS, and only WARNS - does not raise - between 1x and 2x) is kept
as a redundant safety net on the load() call in _decode_and_normalize_image, not the
real enforcement; this is verified against Pillow's actual installed source (Image.py's
_decompression_bomb_check), not assumed from the constant's name.

Everything the module docstring's earlier sections say about tmp/-then-move, the
service account's posts/ delete restriction, and the orphan-on-commit-failure tradeoff
is UNCHANGED by this: the object that lands in posts/ or avatars/ is simply built
differently now. destination_prefix is still the only thing that varies between the
post-media and avatar call sites (see finalize_media_object's own docstring for why
avatars get their own prefix) - the derivative pipeline runs identically either way, on
purpose: an avatar is not exempt from the same privacy and cost reasoning a post photo
gets.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import io
import logging
import threading
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
# Avatars get their OWN permanent prefix rather than sharing posts/ (board c221), and
# this is a data-loss guard, not tidiness. jobs/media_reconcile.py builds its reference
# set from `select(Post.media_urls)` and NOTHING else, then diffs it against everything
# under posts/. users.avatar_url is not in that set, so an avatar finalized to posts/
# would be unreferenced BY DEFINITION, age past min_age_hours, and be collected on the
# next --delete run - roughly a day after the user set it.
#
# A separate prefix is also the safer of the two fixes. Teaching the job about
# users.avatar_url would work, but leaves a version-skew window in which an older image
# of the job deletes live avatars; avatars/ is inert to every version of that job,
# past and present. The job's own stated asymmetry decides it: over-protection leaves an
# orphan uncollected, under-protection destroys someone's photo.
AVATAR_PREFIX = "avatars"


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


# Board c374. See the module docstring's "DERIVATIVES, NOT COPIES" section for why
# these numbers are what they are - this is just where they live.
DERIVATIVE_MAX_DIMENSION = 1600
DERIVATIVE_JPEG_QUALITY = 82
DERIVATIVE_CONTENT_TYPE = "image/jpeg"
DERIVATIVE_EXTENSION = "jpg"

# chirp-api runs at 512Mi with concurrency=80 (infra/deployment.json) - many requests can
# be decoding an image on the same instance at once, so per-decode memory is not a
# theoretical concern. These two caps are checked against the image HEADER, before any
# pixel data is decoded (see _decode_and_normalize_image), and are deliberately format-
# specific rather than one shared Image.MAX_IMAGE_PIXELS-style number:
#
# JPEG gets the higher cap because image.draft() lets libjpeg DCT-scale the decode
# itself for images large enough to clear a discrete scale step. HONEST LIMIT, verified
# empirically against the installed Pillow, not assumed: draft() only has whole-number
# steps (1, 1/2, 1/4, 1/8), gated by the SHORTER source dimension relative to the
# requested box - a source that is not at least ~2x the target box in BOTH dimensions
# gets no reduction at all. A standard 4:3 photo needs roughly an 8-megapixel-plus long
# edge before any step applies; below that, this cap (not draft) is the real worst-case
# memory bound - up to ~150MB for a full-resolution RGB decode at 50 megapixels, before
# the alpha-composite/exif_transpose working copies on top of that. Still strictly
# better than PNG/WebP's fully-undraftable 20-megapixel cap below, and the common case
# (an actual phone photo well above 8MP) does get real draft-time savings.
DERIVATIVE_MAX_SOURCE_PIXELS_JPEG = 50_000_000

# PNG/WebP have no equivalent draft mechanism in Pillow - the full claimed pixel count
# decodes at full resolution with no way to pre-scale, so this cap has to bound memory
# directly: 20 megapixels is roughly 80MB as decoded RGBA, before the alpha-composite
# copy _decode_and_normalize_image makes on top of that.
DERIVATIVE_MAX_SOURCE_PIXELS_OTHER = 20_000_000


def _decode_and_normalize_image(raw_bytes: bytes):
    """Pillow-decode one uploaded object into an EXIF-transposed RGB image, or 400.

    A 400, not a 500 or 502: everything caught here is a property of the BYTES the
    caller's own tmp/ upload contains, not of GCS or this process. That covers three
    distinct cases, deliberately collapsed into one outcome rather than three:

      - Not decodable as an image at all (UnidentifiedImageError) - including a file
        whose declared content-type at upload time (validate_upload_request) was a lie,
        since nothing checked the BYTES against that claim until now. This function is
        that check, arrived at as a side effect of needing to decode anyway.
      - A truncated or otherwise corrupt body that identifies fine from its header but
        fails during the real decode (OSError/ValueError from libjpeg/libpng/libwebp) -
        image.load() forces that decode to happen HERE rather than lazily later, so the
        failure surfaces in this function's own try block instead of downstream.
      - A decompression bomb: a decoded pixel count enormously out of proportion to what
        MAX_UPLOAD_BYTES allows as compressed bytes - a small, highly-compressible file
        can still decode to gigapixels. Checked against the image's HEADER size (from the
        lazy Image.open(), before any pixel data is decoded) against
        DERIVATIVE_MAX_SOURCE_PIXELS_JPEG/_OTHER, and REJECTED BEFORE image.load() runs -
        catching this only after a full decode, the way an earlier version of this
        function did, means paying the exact memory cost being rejected for before
        rejecting it. Pillow's own DecompressionBombError (which only fires above 2x
        Image.MAX_IMAGE_PIXELS, and only WARNS - does not raise - between 1x and 2x) is
        kept as a redundant safety net on the load() call below, not the real
        enforcement.

    JPEG SPECIFICALLY GETS image.draft() BEFORE load(), which is what actually bounds
    JPEG decode memory for large-enough sources (the header-size check above only
    rejects a pathologically large CLAIMED size). draft() tells libjpeg to DCT-scale the
    decode itself instead of decoding full-resolution and downscaling after - an
    8000x6000 (48-megapixel) phone JPEG decodes at 4000x3000, not 48 megapixels. THE
    REQUESTED BOX MUST BE ASPECT-RATIO-CORRECT, not a bare square: verified empirically
    against the installed Pillow, JpegImageFile.draft()'s achievable scale is
    min(width // box_width, height // box_height), so a plain
    (2*DERIVATIVE_MAX_DIMENSION, 2*DERIVATIVE_MAX_DIMENSION) box is gated by whichever
    source dimension is shorter relative to that square and achieves NO reduction at all
    for a standard 4:3 photo, even at 48 megapixels - see the draft() call site in
    _decode_and_normalize_image for the aspect-correct box this module actually uses.
    HONEST LIMIT: a source that is not at least ~2x the draft box in BOTH dimensions
    gets no reduction regardless of box shape - below that, DERIVATIVE_MAX_SOURCE_PIXELS_
    JPEG (not draft) is the real worst-case memory bound. PNG/WebP have no draft
    equivalent in Pillow at all, which is why DERIVATIVE_MAX_SOURCE_PIXELS_OTHER is a
    much tighter cap: for those formats the header check is the ONLY memory bound, since
    the full claimed pixel count decodes at full resolution with no way to pre-scale it.

    exif_transpose() BAKES ORIENTATION INTO THE PIXELS and returns an image with the
    Orientation tag removed - this is what makes a rotated phone photo display right-side
    up without every future reader needing to know EXIF exists. It is not, by itself,
    what strips the rest of the EXIF block (GPS, make/model, timestamp): the returned
    image can still carry a smaller `exif` blob in its .info dict. The actual strip
    happens where this image is later saved - see _build_media_derivative.

    Modes with an alpha channel are composited onto white rather than bluntly
    `.convert("RGB")`-ed, because a plain mode conversion DROPS the alpha channel
    without compositing and leaves whatever the original RGB values under a transparent
    pixel happened to be - not necessarily white, sometimes visibly wrong. JPEG has no
    alpha channel, so something has to be decided for every transparent pixel in a PNG
    upload; white is the least surprising choice for a photo/avatar context.
    """
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        image = Image.open(io.BytesIO(raw_bytes))
    except Image.DecompressionBombError as exc:
        # Verified against the installed source (Image.py's _open_core): Pillow calls
        # _decompression_bomb_check(im.size) INSIDE Image.open() itself, immediately
        # after a format plugin determines size from the header - not deferred to
        # load(). This only catches sizes past Pillow's own 2x-threshold raise; the
        # explicit header check below (DERIVATIVE_MAX_SOURCE_PIXELS_*) is what actually
        # enforces this module's own, tighter bound and closes Pillow's 1x-2x warn-only
        # gap - this except exists so an extreme size doesn't surface as an uncaught 500
        # before this function even reaches its own check.
        logger.warning("media finalize rejected a decompression bomb, size=%s", len(raw_bytes))
        raise HTTPException(status_code=400, detail="invalid_media_content") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="invalid_media_content") from exc

    # image.size here comes from the HEADER only - Image.open() is lazy about pixel
    # data (nothing has been decoded yet, only the header). Reject an oversized source
    # BEFORE ever calling .load(), so a pathological image is never fully decoded into
    # memory just to be thrown away immediately after.
    is_jpeg = image.format == "JPEG"
    max_source_pixels = (
        DERIVATIVE_MAX_SOURCE_PIXELS_JPEG if is_jpeg else DERIVATIVE_MAX_SOURCE_PIXELS_OTHER
    )
    header_pixel_count = image.size[0] * image.size[1]
    if header_pixel_count > max_source_pixels:
        logger.warning(
            "media finalize rejected an oversized image before decode, pixels=%s limit=%s format=%s",
            header_pixel_count,
            max_source_pixels,
            image.format,
        )
        raise HTTPException(status_code=400, detail="invalid_media_content")

    if is_jpeg:
        # DCT-scale the decode itself, before load(). THE REQUESTED BOX MUST BE
        # ASPECT-RATIO-CORRECT, NOT A BARE SQUARE - verified empirically, not assumed:
        # JpegImageFile.draft()'s scale is min(width // box_width, height // box_height),
        # so a square box is gated by whichever source dimension is SHORTER relative to
        # the box. For a standard 4:3 photo, a plain (3200, 3200) box (2x
        # DERIVATIVE_MAX_DIMENSION, matching this module's own long-edge cap) achieves
        # ZERO reduction even at 8000x6000 (48 megapixels - deliberately the same size
        # this module's docstring cites as the target case): min(8000//3200, 6000//3200)
        # = min(2, 1) = 1. The box below scales proportionally to the source's own
        # aspect ratio instead, which brings that same 8000x6000 source down to 4000x3000
        # (a real 4x pixel-count reduction) - confirmed against the installed Pillow
        # source (JpegImagePlugin.draft), not assumed from the method's docstring.
        width, height = image.size
        if width >= height:
            box = (2 * DERIVATIVE_MAX_DIMENSION, round(2 * DERIVATIVE_MAX_DIMENSION * height / width))
        else:
            box = (round(2 * DERIVATIVE_MAX_DIMENSION * width / height), 2 * DERIVATIVE_MAX_DIMENSION)
        image.draft("RGB", box)

    try:
        image.load()
    except Image.DecompressionBombError as exc:
        # Redundant safety net: the header check above already rejects anything past
        # this module's own (tighter) caps, so Pillow's own 2x-Image.MAX_IMAGE_PIXELS
        # raise should not be reachable in practice. Kept anyway as defense in depth.
        logger.warning("media finalize rejected a decompression bomb, size=%s", len(raw_bytes))
        raise HTTPException(status_code=400, detail="invalid_media_content") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="invalid_media_content") from exc

    image = ImageOps.exif_transpose(image)
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        image = background
    elif image.mode != "RGB":
        image = image.convert("RGB")
    return image


def _build_media_derivative(raw_bytes: bytes) -> bytes:
    """Decode -> EXIF-strip -> downscale to a 1600px long edge -> re-encode as JPEG.

    THE STRIP IS "DON'T PASS exif=", NOT AN EXPLICIT CLEAR CALL - confirmed against the
    installed Pillow body, not assumed (the c132 standing lesson, applied again here):
    JpegImagePlugin._save reads `im.encoderinfo.get("exif", ...)` - i.e. ONLY what a
    caller explicitly passes to save() as the `exif=` kwarg. It does NOT fall back to
    image.info["exif"], even though _decode_and_normalize_image's exif_transpose() call
    can leave that key populated (with a smaller, orientation-removed blob) on the image
    object it returns. So the image handed to save() below can still be carrying EXIF
    internally; what guarantees none of it reaches the output is simply that this call
    never mentions `exif=`. Do not "helpfully" thread image.info through as exif= here -
    that would silently undo the strip this function exists to guarantee.

    thumbnail() only ever shrinks, never enlarges - an upload already under 1600px on its
    long edge is re-encoded at its original size (still EXIF-stripped, still requantized
    to quality 82). DERIVATIVE_MAX_DIMENSION is applied to BOTH bounds of thumbnail()'s
    (width, height) argument, which is what makes it a long-edge cap under Pillow's own
    aspect-preserving contract rather than a fixed square.
    """
    from PIL import Image

    image = _decode_and_normalize_image(raw_bytes)
    image.thumbnail(
        (DERIVATIVE_MAX_DIMENSION, DERIVATIVE_MAX_DIMENSION), Image.Resampling.LANCZOS
    )
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=DERIVATIVE_JPEG_QUALITY)
    return buffer.getvalue()


def finalize_media_object(
    user_id: str, tmp_object_name: str, *, destination_prefix: str = PERMANENT_PREFIX
) -> str:
    """Turn one tmp/ upload into a bounded derivative at a permanent location; returns
    the new public url.

    destination_prefix DEFAULTS TO posts/ so every existing caller is unchanged. Avatars
    pass AVATAR_PREFIX (board c221) because media_reconcile diffs posts/ against
    posts.media_urls alone - an avatar under posts/ is unreferenced by definition and
    would be collected about a day after it was set. Keyword-only so a new destination
    is always an explicit, greppable decision at the call site rather than a
    positional argument someone can slip in. The derivative pipeline itself does not
    branch on destination_prefix at all - an avatar gets the identical EXIF-strip,
    downscale, and re-encode a post photo gets (see the module docstring).

    Board c132, reshaped by board c374 from a straight bucket.copy_blob() into a
    download-decode-reencode-upload pipeline (see _decode_and_normalize_image and
    _build_media_derivative for that half). Called once per media_object_names entry,
    at post-create/update time, AFTER validate_media_object_names() has already
    confirmed the shape and ownership prefix — this function re-derives and re-checks
    the same prefix rather than trusting the caller ran that check, on the same
    "validate again where it actually matters" reasoning the double size-enforcement
    already follows.

    THE OUTPUT OBJECT NAME DROPS THE ORIGINAL EXTENSION. The derivative is always a
    JPEG regardless of what was uploaded (jpg/png/webp, per ALLOWED_CONTENT_TYPES), so
    the permanent object name is always {destination_prefix}/{user_id}/{uuid}.jpg even
    when the tmp/ source was {uuid}.png or {uuid}.webp - an object name that still
    ended in .png while holding JPEG bytes would be a lie the extension tells about the
    content.

    if_generation_match=0 on the upload is REQUIRED, not optional, and for a subtler
    reason a fake client cannot surface: it applies to the DESTINATION generation
    (confirmed against the installed client's own docstring, not assumed), and an
    UNCONDITIONAL write to a destination that might already exist requires
    storage.objects.delete on posts/ even though it is only ever creating a new object
    there - because an unconditional write CAN overwrite an existing one, and overwrite
    implies delete. Our service account deliberately has create-only on posts/ (see
    below). if_generation_match=0 asserts "the destination must not already exist,"
    which only requires create - and, as a bonus, turns no-overwrite from a
    probabilistic property of UUID naming into a server-enforced guarantee. This was
    originally learned against copy_blob's destination argument; it applies identically
    to upload_from_string's destination now.

    A tmp_blob.delete() failure AFTER a successful upload is NOT fatal and does not
    raise: the post still gets its permanent url, and the leftover tmp/ object becomes
    the tmp/ lifecycle rule's job, the same safety net an abandoned upload already
    relies on. It is logged as a warning so a persistent delete-permission problem is
    still visible, just not blocking.

    DOWNLOAD and UPLOAD each get their own try/except, both mapping to the same 400/502
    split the old copy had: a download 404 (tmp object never uploaded, or the tmp/
    lifecycle rule already reclaimed it) is a 400 media_upload_not_found - the caller
    referenced something that isn't there to move. Any OTHER GCS failure on either leg
    (permission, precondition, transient error) is a 502 media_finalize_failed, not a
    bare 500. A bad IMAGE (undecodable, corrupt, or a decompression bomb) is a separate
    400 - invalid_media_content - raised by _decode_and_normalize_image between the two
    GCS calls; that path never reaches the upload try/except at all.

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
    stem = suffix.rsplit(".", 1)[0]
    bucket_name = _bucket_name()
    bucket = _storage_client().bucket(bucket_name)
    tmp_blob = bucket.blob(tmp_object_name)
    permanent_object_name = f"{destination_prefix}/{user_id}/{stem}.{DERIVATIVE_EXTENSION}"

    try:
        raw_bytes = tmp_blob.download_as_bytes()
    except NotFound:
        raise HTTPException(status_code=400, detail="media_upload_not_found")
    except Exception as exc:
        logger.error(
            "media finalize download failed tmp_object=%s error=%s",
            tmp_object_name,
            type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail="media_finalize_failed") from exc

    derivative_bytes = _build_media_derivative(raw_bytes)

    permanent_blob = bucket.blob(permanent_object_name)
    try:
        permanent_blob.upload_from_string(
            derivative_bytes,
            content_type=DERIVATIVE_CONTENT_TYPE,
            if_generation_match=0,
        )
    except Exception as exc:
        logger.error(
            "media finalize upload failed tmp_object=%s error=%s",
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
# c211: signed_read_url() is called via asyncio.to_thread() from routers/media.py, so
# this dict can now be touched from more than one worker thread at once - previously
# the caller's own blocking (sync-in-async) call already serialized every access, so
# plain dict mutation was safe by accident. It no longer is: the stale-entry cleanup
# below builds a key list with `[k for k in _signed_read_cache if ...]` and then `del`s
# each one - two threads doing that concurrently can raise KeyError (one thread deletes
# a key the other already deleted) or RuntimeError: dictionary changed size during
# iteration (one thread inserts/deletes while another is mid-comprehension), and either
# one is a real exception on a live request, not a theoretical one. The lock below
# wraps only the dict get/cleanup/set - never the signBlob network call itself, since
# holding it across that call would re-serialize the exact request path this whole
# change (to_thread) exists to stop serializing. The accepted trade: two threads racing
# a COLD entry for the same (object, window) can both call signBlob and the second
# write just overwrites the first - a wasted duplicate call, never a wrong url.
_signed_read_cache_lock = threading.Lock()


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
    with _signed_read_cache_lock:
        cached = _signed_read_cache.get(key)
    if cached is not None:
        return cached

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
    with _signed_read_cache_lock:
        # Drop other windows' entries rather than letting the dict grow forever. Same-
        # window entries for OTHER objects must survive - they are the cache doing its
        # job.
        for stale in [k for k in _signed_read_cache if k[1] != window_index]:
            del _signed_read_cache[stale]
        _signed_read_cache[key] = url
    return url
