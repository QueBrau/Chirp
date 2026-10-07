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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.config import get_settings
from app.services.storage_service import object_name_from_stored_url

_OBJECT_NAME_RE = re.compile(r"^(posts|avatars)/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/([A-Za-z0-9][A-Za-z0-9._-]{0,255})$")
_MAX_OBJECT_NAME_LENGTH = 512
SUPPORTED_SURFACES = ("posts.media_urls", "users.avatar_url")
UNSUPPORTED_MEDIA_SURFACES = ("content_reports.forwarded_plaintext",)


@dataclass(frozen=True)
class Reference:
    surface: str
    row_id: str
    state: str
    slot: int | None = None


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
    complete: bool
    ready_to_apply: bool
    manifest_digest: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {
            "scope": list(self.scope),
            "references": [asdict(item) for item in self.references],
            "unknown_references": [asdict(item) for item in self.unknown_references],
            "unsupported_media_surfaces": list(self.unsupported_media_surfaces),
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


def _owned_url(value: str, bucket: str) -> bool:
    return value.startswith(
        (
            f"https://storage.googleapis.com/{bucket}/",
            f"https://storage.cloud.google.com/{bucket}/",
            f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/",
        )
    )


def _manifest_digest(
    *,
    bucket: str,
    object_name: str,
    references: tuple[Reference, ...],
    unknown_references: tuple[UnknownReference, ...],
) -> str:
    payload = {
        "schema_version": 1,
        "bucket": bucket,
        "object_name": object_name,
        "scope": list(SUPPORTED_SURFACES),
        "references": [asdict(item) for item in references],
        "unknown_references": [asdict(item) for item in unknown_references],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def inventory_media_reference(session: AsyncSession, object_name: str) -> RemovalInventory:
    """Return a deterministic, read-only inventory for ``object_name``.

    ``ready_to_apply`` means only that every value in the supported reference columns
    was classified and the supplied object has at least one reference. It does not
    mean byte-identical copies were found, a provider object exists, or deletion is
    authorized. Unknown owned-bucket formats make the result incomplete and refuse
    readiness. Foreign-host URLs are known out-of-scope values and do not match.
    """
    bucket = get_settings().media_bucket_name or ""
    _validate_object_name(object_name, bucket)
    references: list[Reference] = []
    unknown: list[UnknownReference] = []

    posts = await session.execute(select(models.Post.id, models.Post.deleted_at, models.Post.media_urls))
    for row_id, deleted_at, media_urls in posts:
        for slot, value in enumerate(media_urls or []):
            parsed = object_name_from_stored_url(value)
            if parsed is None or not _OBJECT_NAME_RE.fullmatch(parsed):
                if _owned_url(value, bucket):
                    unknown.append(UnknownReference("posts.media_urls", str(row_id), _value_digest(value), "unsupported_owned_url"))
                continue
            if parsed == object_name:
                references.append(Reference("posts.media_urls", str(row_id), "removed" if deleted_at else "live", slot))

    users = await session.execute(select(models.User.id, models.User.avatar_url))
    for row_id, value in users:
        if value is None:
            continue
        parsed = object_name_from_stored_url(value)
        if parsed is None or not _OBJECT_NAME_RE.fullmatch(parsed):
            if _owned_url(value, bucket):
                unknown.append(UnknownReference("users.avatar_url", str(row_id), _value_digest(value), "unsupported_owned_url"))
            continue
        if parsed == object_name:
            references.append(Reference("users.avatar_url", str(row_id), "active", None))

    references.sort(key=lambda item: (item.surface, item.row_id, item.slot if item.slot is not None else -1, item.state))
    unknown.sort(key=lambda item: (item.surface, item.row_id, item.value_digest, item.reason))
    refs = tuple(references)
    unknown_refs = tuple(unknown)
    digest = _manifest_digest(
        bucket=bucket,
        object_name=object_name,
        references=refs,
        unknown_references=unknown_refs,
    )
    complete = not unknown_refs
    return RemovalInventory(
        schema_version=1,
        bucket=bucket,
        object_name=object_name,
        scope=SUPPORTED_SURFACES,
        references=refs,
        unknown_references=unknown_refs,
        unsupported_media_surfaces=UNSUPPORTED_MEDIA_SURFACES,
        complete=complete,
        ready_to_apply=complete and bool(refs),
        manifest_digest=digest,
    )
