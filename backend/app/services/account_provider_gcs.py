"""Dormant, generation-bound GCS account-media provider for c436.

The adapter is intentionally not wired into account_fulfillment. An operator must
supply a verified, short-lived account freeze receipt and a repeatable-read DB
session before snapshotting. The snapshot is immutable; any provider scope or
generation drift blocks before the first delete.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.services.storage_service import object_name_from_stored_url


PREFIXES = ("tmp/", "posts/", "avatars/")
MAX_OBJECTS = 10_000
MAX_POSTS = 10_000
MAX_MEDIA_VALUES = 64
MAX_SECONDS = 900.0


class MediaProviderUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class FreezeReceipt:
    user_id: str
    bucket: str
    freeze_id: str
    expires_at: float
    verified: bool = True

    def validate(self, user_id: UUID | str, bucket: str) -> None:
        if (
            not self.verified
            or str(self.user_id) != str(user_id)
            or self.bucket != bucket
            or not self.freeze_id
            or not math.isfinite(self.expires_at)
            or self.expires_at <= time.time()
        ):
            raise MediaProviderUnavailable("media_freeze_invalid")


@dataclass(frozen=True)
class MediaObject:
    name: str
    generation: str

    def payload(self) -> dict[str, str]:
        return {"name": self.name, "generation": self.generation}


@dataclass(frozen=True)
class MediaSnapshot:
    user_id: str
    bucket: str
    prefixes: tuple[str, ...]
    objects: tuple[MediaObject, ...]
    freeze_id: str
    snapshot_digest: str

    def payload(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "bucket": self.bucket,
            "prefixes": list(self.prefixes),
            "objects": [item.payload() for item in self.objects],
            "freeze_id": self.freeze_id,
        }


@dataclass(frozen=True)
class MediaMetadata:
    name: str
    generation: str


class MediaReader(Protocol):
    def list(self, prefix: str, *, deadline: float) -> Iterable[Any]: ...
    def read(self, name: str, *, deadline: float) -> MediaMetadata | None: ...
    def delete(self, name: str, generation: str, *, deadline: float) -> None: ...


def _digest(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise MediaProviderUnavailable("media_deadline_exceeded")
    return max(0.01, remaining)


def _generation(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).isdigit() or int(value) <= 0:
        raise MediaProviderUnavailable("media_generation_invalid")
    return str(value)


def _metadata(raw: Any) -> MediaMetadata:
    name = getattr(raw, "name", raw.get("name") if isinstance(raw, dict) else None)
    generation = getattr(raw, "generation", raw.get("generation") if isinstance(raw, dict) else None)
    if not isinstance(name, str) or not any(name.startswith(prefix) for prefix in PREFIXES):
        raise MediaProviderUnavailable("media_object_scope_invalid")
    user_part = name.split("/", 2)
    if len(user_part) != 3 or not user_part[1]:
        raise MediaProviderUnavailable("media_object_scope_invalid")
    return MediaMetadata(name, _generation(generation))


def _owned_name(name: str, user_id: UUID | str) -> bool:
    return any(name.startswith(f"{prefix}{user_id}/") for prefix in PREFIXES)


async def _owned_references(session: AsyncSession, user_id: UUID) -> set[str]:
    result = await session.execute(
        select(models.Post.media_urls)
        .where(models.Post.author_id == user_id)
        .limit(MAX_POSTS + 1)
    )
    rows = result.scalars().all()
    if len(rows) > MAX_POSTS:
        raise MediaProviderUnavailable("media_post_cap_reached")
    names: set[str] = set()
    for values in rows:
        values = values or []
        if len(values) > MAX_MEDIA_VALUES:
            raise MediaProviderUnavailable("media_reference_cap_reached")
        for value in values:
            if not isinstance(value, str):
                raise MediaProviderUnavailable("media_reference_invalid")
            name = object_name_from_stored_url(value)
            if name is None or not _owned_name(name, user_id):
                raise MediaProviderUnavailable("media_reference_scope_invalid")
            names.add(name)
    avatar = (await session.execute(
        select(models.User.avatar_url).where(models.User.id == user_id)
    )).scalar_one_or_none()
    if avatar is not None:
        name = object_name_from_stored_url(avatar)
        if name is None or not _owned_name(name, user_id):
            raise MediaProviderUnavailable("media_reference_scope_invalid")
        names.add(name)
    return names


async def snapshot_user_media(
    session: AsyncSession,
    user_id: UUID,
    reader: MediaReader,
    freeze: FreezeReceipt,
    *,
    bucket: str,
    max_objects: int = MAX_OBJECTS,
    deadline_seconds: float = 120.0,
) -> MediaSnapshot:
    freeze.validate(user_id, bucket)
    if isinstance(max_objects, bool) or not isinstance(max_objects, int) or not 1 <= max_objects <= MAX_OBJECTS:
        raise ValueError("media_object_cap_invalid")
    if not math.isfinite(deadline_seconds) or not 1 <= deadline_seconds <= MAX_SECONDS:
        raise ValueError("media_deadline_invalid")
    await _owned_references(session, user_id)
    deadline = time.monotonic() + deadline_seconds
    found: dict[tuple[str, str], MediaObject] = {}
    for prefix in PREFIXES:
        scoped_prefix = f"{prefix}{user_id}/"
        try:
            rows = list(reader.list(scoped_prefix, deadline=deadline))
        except MediaProviderUnavailable:
            raise
        except Exception:
            raise MediaProviderUnavailable("media_list_failed") from None
        if len(rows) > max_objects:
            raise MediaProviderUnavailable("media_object_cap_reached")
        for raw in rows:
            item = _metadata(raw)
            if not _owned_name(item.name, user_id) or not item.name.startswith(scoped_prefix):
                raise MediaProviderUnavailable("media_object_scope_invalid")
            found[(item.name, item.generation)] = MediaObject(item.name, item.generation)
    objects = tuple(sorted(found.values(), key=lambda item: (item.name, item.generation)))
    payload = {
        "user_id": str(user_id),
        "bucket": bucket,
        "prefixes": list(PREFIXES),
        "objects": [item.payload() for item in objects],
        "freeze_id": freeze.freeze_id,
    }
    return MediaSnapshot(str(user_id), bucket, PREFIXES, objects, freeze.freeze_id, _digest(payload))


class GCSMediaReader:
    """Small production GCS reader; every operation receives a finite timeout."""

    def __init__(self, bucket: str):
        if not isinstance(bucket, str) or not bucket or "/" in bucket:
            raise ValueError("media_bucket_invalid")
        from app.services import storage_service
        self.bucket_name = bucket
        self._bucket = storage_service._storage_client().bucket(bucket)

    def list(self, prefix: str, *, deadline: float) -> Iterable[Any]:
        return self._bucket.list_blobs(prefix=prefix, timeout=_remaining(deadline), retry=None)

    def read(self, name: str, *, deadline: float) -> MediaMetadata | None:
        blob = self._bucket.blob(name)
        try:
            blob.reload(timeout=_remaining(deadline), retry=None)
        except Exception as exc:
            if getattr(exc, "code", None) == 404:
                return None
            raise MediaProviderUnavailable("media_read_failed") from None
        return MediaMetadata(str(blob.name), _generation(blob.generation))

    def delete(self, name: str, generation: str, *, deadline: float) -> None:
        try:
            self._bucket.blob(name).delete(
                if_generation_match=int(_generation(generation)),
                timeout=_remaining(deadline),
                retry=None,
            )
        except Exception as exc:
            if getattr(exc, "code", None) == 404:
                return
            if getattr(exc, "code", None) == 412:
                raise MediaProviderUnavailable("media_generation_mismatch") from None
            raise MediaProviderUnavailable("media_delete_failed") from None


class GCSMediaProvider:
    """Operator-only provider applying one verified snapshot, never auto-wired."""

    def __init__(self, reader: MediaReader, *, bucket: str):
        self._reader = reader
        self.bucket = bucket

    async def delete_account_data(
        self,
        *,
        user: models.User,
        idempotency_key: str,
        snapshot: MediaSnapshot,
        freeze: FreezeReceipt,
        deadline_seconds: float = 120.0,
    ) -> dict[str, object]:
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise MediaProviderUnavailable("media_idempotency_key_invalid")
        if snapshot.user_id != str(user.id) or snapshot.bucket != self.bucket:
            raise MediaProviderUnavailable("media_snapshot_subject_mismatch")
        freeze.validate(user.id, self.bucket)
        if freeze.freeze_id != snapshot.freeze_id:
            raise MediaProviderUnavailable("media_freeze_mismatch")
        if not math.isfinite(deadline_seconds) or not 1 <= deadline_seconds <= MAX_SECONDS:
            raise ValueError("media_deadline_invalid")
        deadline = time.monotonic() + deadline_seconds
        expected = {(item.name, item.generation) for item in snapshot.objects}
        current: dict[str, MediaObject] = {}
        try:
            for prefix in PREFIXES:
                for raw in self._reader.list(f"{prefix}{user.id}/", deadline=deadline):
                    item = _metadata(raw)
                    current[item.name] = MediaObject(item.name, item.generation)
        except MediaProviderUnavailable:
            raise
        except Exception:
            raise MediaProviderUnavailable("media_list_failed") from None
        expected_names = {name for name, _ in expected}
        if set(current) - expected_names:
            raise MediaProviderUnavailable("media_snapshot_drift")
        if any(
            name in current and generation != current[name].generation
            for name, generation in expected
        ):
            raise MediaProviderUnavailable("media_snapshot_drift")
        for item in snapshot.objects:
            observed = self._reader.read(item.name, deadline=deadline)
            if observed is not None and (observed.name, observed.generation) != (item.name, item.generation):
                raise MediaProviderUnavailable("media_generation_mismatch")
        for item in snapshot.objects:
            observed = self._reader.read(item.name, deadline=deadline)
            if observed is None:
                continue
            if (observed.name, observed.generation) != (item.name, item.generation):
                raise MediaProviderUnavailable("media_generation_mismatch")
            self._reader.delete(item.name, item.generation, deadline=deadline)
            after = self._reader.read(item.name, deadline=deadline)
            if after is not None:
                raise MediaProviderUnavailable("media_readback_failed")
        return {
            "confirmed": True,
            "provider": "media_storage",
            "subject": str(user.id),
            "scope": [f"{prefix}{user.id}/" for prefix in PREFIXES],
            "object_count": len(snapshot.objects),
            "snapshot_digest": snapshot.snapshot_digest,
            "readback": True,
        }
