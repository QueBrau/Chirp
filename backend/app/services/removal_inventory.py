"""Read-only, digest-bound inventory for one controlled media object reference.

This module deliberately inventories *shared object references* in the database. It
does not inspect image bytes, list a provider bucket, compare hashes, mutate rows, or
delete objects. The supported schema surfaces are ``posts.media_urls`` and
``users.avatar_url``; forwarded report text is not a media-reference schema and is
explicitly outside this manifest's claim.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.config import get_settings
from app.services.storage_service import object_name_from_stored_url

_OBJECT_NAME_RE = re.compile(r"^(posts|avatars)/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/([A-Za-z0-9][A-Za-z0-9._-]{0,255})$")
_MAX_OBJECT_NAME_LENGTH = 512
POST_PAGE_SIZE = 250
USER_PAGE_SIZE = 250
MAX_POST_ROWS = 10_000
MAX_USER_ROWS = 10_000
MAX_MEDIA_VALUES_PER_ROW = 64
STATEMENT_TIMEOUT_MS = 2_000
SUPPORTED_SURFACES = ("posts.media_urls", "users.avatar_url")
UNSUPPORTED_MEDIA_SURFACES = ("content_reports.forwarded_plaintext",)


@dataclass(frozen=True)
class Reference:
    surface: str
    row_id: str
    state: str
    slot: int | None = None
    row_generation: str | None = None


@dataclass(frozen=True)
class UnknownReference:
    surface: str
    row_id: str
    value_digest: str
    reason: str


@dataclass(frozen=True)
class RemovalInventory:
    schema_version: int
    bucket: str
    object_name: str
    scope: tuple[str, ...]
    references: tuple[Reference, ...]
    unknown_references: tuple[UnknownReference, ...]
    unsupported_media_surfaces: tuple[str, ...]
    supported_reference_scan_complete: bool
    incomplete_reasons: tuple[str, ...]
    ready_for_review: bool
    deletion_authorized: bool
    manifest_digest: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {
            "scope": list(self.scope),
            "references": [asdict(item) for item in self.references],
            "unknown_references": [asdict(item) for item in self.unknown_references],
            "unsupported_media_surfaces": list(self.unsupported_media_surfaces),
            "incomplete_reasons": list(self.incomplete_reasons),
        }


def _validate_object_name(object_name: str, bucket: str) -> None:
    if not isinstance(object_name, str) or len(object_name) > _MAX_OBJECT_NAME_LENGTH:
        raise ValueError("object_name_invalid")
    if object_name != object_name.strip() or "?" in object_name or "#" in object_name:
        raise ValueError("object_name_invalid")
    if not _OBJECT_NAME_RE.fullmatch(object_name):
        raise ValueError("object_name_invalid")
    if not bucket:
        raise ValueError("media_bucket_unconfigured")


def _value_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _known_foreign_url(value: str) -> bool:
    """Classify only plainly foreign HTTP(S) URLs as out of scope.

    ``gs://``, virtual-hosted storage URLs, malformed local values, and unknown
    storage.google.com shapes remain ambiguous and therefore block completeness.
    """
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    if host in {"storage.googleapis.com", "storage.cloud.google.com"} or host.endswith(".storage.googleapis.com"):
        return False
    return True


def _manifest_digest(
    *,
    bucket: str,
    object_name: str,
    references: tuple[Reference, ...],
    unknown_references: tuple[UnknownReference, ...],
    incomplete_reasons: tuple[str, ...],
    supported_reference_scan_complete: bool,
) -> str:
    payload = {
        "schema_version": 1,
        "bucket": bucket,
        "object_name": object_name,
        "scope": list(SUPPORTED_SURFACES),
        "unsupported_media_surfaces": list(UNSUPPORTED_MEDIA_SURFACES),
        "limits": {
            "post_page_size": POST_PAGE_SIZE,
            "user_page_size": USER_PAGE_SIZE,
            "max_post_rows": MAX_POST_ROWS,
            "max_user_rows": MAX_USER_ROWS,
            "max_media_values_per_row": MAX_MEDIA_VALUES_PER_ROW,
            "statement_timeout_ms": STATEMENT_TIMEOUT_MS,
        },
        "supported_reference_scan_complete": supported_reference_scan_complete,
        "incomplete_reasons": list(incomplete_reasons),
        "references": [asdict(item) for item in references],
        "unknown_references": [asdict(item) for item in unknown_references],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def inventory_media_reference(session: AsyncSession, object_name: str) -> RemovalInventory:
    """Return a deterministic, read-only inventory for ``object_name``.

    ``ready_for_review`` means only that every scanned value was classified and the
    supplied object has at least one reference. It does not mean byte-identical copies
    were found, a provider object exists, or deletion is authorized. This function
    never authorizes deletion. Ambiguous formats and scan caps make the result
    incomplete; plainly foreign HTTP(S) URLs are known out-of-scope values.
    """
    bucket = get_settings().media_bucket_name or ""
    _validate_object_name(object_name, bucket)
    references: list[Reference] = []
    unknown: list[UnknownReference] = []
    incomplete_reasons: list[str] = []
    # A caller that already opened a transaction cannot retroactively request a
    # repeatable-read snapshot. Keep scanning for an operator diagnostic, but bind
    # that fact into the manifest so it can never look apply-ready.
    if session.in_transaction():
        incomplete_reasons.append("repeatable_read_snapshot_unavailable")
    else:
        try:
            # This must be the first command in a fresh transaction. A caller that
            # already used its session cannot retroactively provide a snapshot; the
            # branch above marks that manifest incomplete instead.
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        except SQLAlchemyError:
            incomplete_reasons.append("repeatable_read_snapshot_unavailable")
    await session.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_MS}ms'"))

    scanned_posts = 0
    last_post_id = None
    while scanned_posts < MAX_POST_ROWS:
        remaining = MAX_POST_ROWS - scanned_posts
        stmt = (
            select(models.Post.id, models.Post.created_at, models.Post.deleted_at, models.Post.media_urls)
            .order_by(models.Post.id)
            .limit(min(POST_PAGE_SIZE, remaining + 1))
        )
        if last_post_id is not None:
            stmt = stmt.where(models.Post.id > last_post_id)
        rows = list((await session.execute(stmt)).all())
        if not rows:
            break
        if len(rows) > remaining:
            rows = rows[:remaining]
            incomplete_reasons.append("post_row_cap_reached")
        for row_id, created_at, deleted_at, media_urls in rows:
            values = media_urls or []
            if len(values) > MAX_MEDIA_VALUES_PER_ROW:
                unknown.append(UnknownReference("posts.media_urls", str(row_id), _value_digest(str(len(values))), "media_value_cap_reached"))
                values = values[:MAX_MEDIA_VALUES_PER_ROW]
            for slot, value in enumerate(values):
                parsed = object_name_from_stored_url(value)
                if parsed is None or not _OBJECT_NAME_RE.fullmatch(parsed):
                    if not _known_foreign_url(value):
                        unknown.append(UnknownReference("posts.media_urls", str(row_id), _value_digest(value), "ambiguous_reference"))
                    continue
                if parsed == object_name:
                    references.append(Reference("posts.media_urls", str(row_id), "removed" if deleted_at else "live", slot, created_at.isoformat()))
        scanned_posts += len(rows)
        last_post_id = rows[-1][0]
        if len(rows) < min(POST_PAGE_SIZE, remaining + 1) or "post_row_cap_reached" in incomplete_reasons:
            break
    if scanned_posts >= MAX_POST_ROWS:
        # Conservative by design: an exact page-multiple gives no proof that a
        # subsequent row did not appear between pages, so it is never apply-ready.
        incomplete_reasons.append("post_row_cap_reached")

    scanned_users = 0
    last_user_id = None
    while scanned_users < MAX_USER_ROWS:
        remaining = MAX_USER_ROWS - scanned_users
        stmt = select(models.User.id, models.User.avatar_url).order_by(models.User.id).limit(min(USER_PAGE_SIZE, remaining + 1))
        if last_user_id is not None:
            stmt = stmt.where(models.User.id > last_user_id)
        rows = list((await session.execute(stmt)).all())
        if not rows:
            break
        if len(rows) > remaining:
            rows = rows[:remaining]
            incomplete_reasons.append("user_row_cap_reached")
        for row_id, value in rows:
            if value is None:
                continue
            parsed = object_name_from_stored_url(value)
            if parsed is None or not _OBJECT_NAME_RE.fullmatch(parsed):
                if not _known_foreign_url(value):
                    unknown.append(UnknownReference("users.avatar_url", str(row_id), _value_digest(value), "ambiguous_reference"))
                continue
            if parsed == object_name:
                references.append(Reference("users.avatar_url", str(row_id), "active", None, None))
        scanned_users += len(rows)
        last_user_id = rows[-1][0]
        if len(rows) < min(USER_PAGE_SIZE, remaining + 1) or "user_row_cap_reached" in incomplete_reasons:
            break
    if scanned_users >= MAX_USER_ROWS:
        incomplete_reasons.append("user_row_cap_reached")

    references.sort(key=lambda item: (item.surface, item.row_id, item.slot if item.slot is not None else -1, item.state))
    unknown.sort(key=lambda item: (item.surface, item.row_id, item.value_digest, item.reason))
    refs = tuple(references)
    unknown_refs = tuple(unknown)
    incomplete = tuple(sorted(set(incomplete_reasons)))
    scan_complete = not unknown_refs and not incomplete
    digest = _manifest_digest(
        bucket=bucket,
        object_name=object_name,
        references=refs,
        unknown_references=unknown_refs,
        incomplete_reasons=incomplete,
        supported_reference_scan_complete=scan_complete,
    )
    return RemovalInventory(
        schema_version=1,
        bucket=bucket,
        object_name=object_name,
        scope=SUPPORTED_SURFACES,
        references=refs,
        unknown_references=unknown_refs,
        unsupported_media_surfaces=UNSUPPORTED_MEDIA_SURFACES,
        supported_reference_scan_complete=scan_complete,
        incomplete_reasons=incomplete,
        ready_for_review=scan_complete and bool(refs),
        deletion_authorized=False,
        manifest_digest=digest,
    )
