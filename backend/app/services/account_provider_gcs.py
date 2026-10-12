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
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Protocol
from urllib.parse import unquote
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models


PREFIXES = ("tmp/", "posts/", "avatars/")
MAX_OBJECTS = 10_000
MAX_POSTS = 10_000
MAX_MEDIA_VALUES = 64
MAX_SECONDS = 120.0


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
            type(self.verified) is not bool
            or self.verified is not True
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


def _collect(reader: MediaReader, prefix: str, deadline: float, limit: int) -> list[Any]:
    values: list[Any] = []
    for value in reader.list(prefix, deadline=deadline):
        if time.monotonic() >= deadline:
            raise MediaProviderUnavailable("media_deadline_exceeded")
        values.append(value)
        if len(values) > limit:
            raise MediaProviderUnavailable("media_object_cap_reached")
    return values


async def _list_bounded(reader: MediaReader, prefix: str, *, deadline: float, limit: int) -> list[Any]:
    task = asyncio.create_task(asyncio.to_thread(_collect, reader, prefix, deadline, limit))
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=_remaining(deadline))
    except MediaProviderUnavailable:
        raise
    except asyncio.TimeoutError:
        try:
            await task
        except Exception:
            pass
        raise MediaProviderUnavailable("media_deadline_exceeded") from None
    except asyncio.CancelledError:
        await asyncio.shield(task)
        raise
    except Exception:
        raise MediaProviderUnavailable("media_list_failed") from None


async def _reader_call(reader: MediaReader, operation: str, *args: Any, deadline: float) -> Any:
    task = asyncio.create_task(asyncio.to_thread(getattr(reader, operation), *args, deadline=deadline))
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=_remaining(deadline))
    except MediaProviderUnavailable:
        raise
    except asyncio.TimeoutError:
        try:
            await task
        except Exception:
            pass
        raise MediaProviderUnavailable("media_deadline_exceeded") from None
    except asyncio.CancelledError:
        await asyncio.shield(task)
        raise
    except Exception:
        raise MediaProviderUnavailable(f"media_{operation}_failed") from None


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
    try:
        canonical = str(UUID(str(user_id)))
    except (ValueError, TypeError, AttributeError):
        return False
    parts = name.split("/", 2)
    return len(parts) == 3 and parts[1] == canonical and bool(parts[2]) and parts[0] + "/" in PREFIXES


def _parse_url(value: str, bucket: str) -> str | None:
    if not isinstance(value, str):
        return None
    path = value.split("?", 1)[0]
    prefixes = (
        f"https://storage.googleapis.com/{bucket}/",
        f"https://storage.cloud.google.com/{bucket}/",
        f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/",
    )
    for prefix in prefixes:
        if path.startswith(prefix):
            suffix = path[len(prefix):]
            return unquote(suffix) if "storage/v1/b/" in prefix else suffix
    return None


async def _owned_references(session: AsyncSession, user_id: UUID, bucket: str) -> set[str]:
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
            name = _parse_url(value, bucket)
            if name is None or not _owned_name(name, user_id):
                raise MediaProviderUnavailable("media_reference_scope_invalid")
            names.add(name)
    avatar = (await session.execute(
        select(models.User.avatar_url).where(models.User.id == user_id)
    )).scalar_one_or_none()
    if avatar is not None:
        name = _parse_url(avatar, bucket)
        if name is None or not _owned_name(name, user_id):
            raise MediaProviderUnavailable("media_reference_scope_invalid")
        names.add(name)
    foreign_posts = (await session.execute(
        select(models.Post.media_urls)
        .where(models.Post.author_id != user_id)
        .limit(MAX_POSTS + 1)
    )).scalars().all()
    if len(foreign_posts) > MAX_POSTS:
        raise MediaProviderUnavailable("media_foreign_reference_cap_reached")
    for values in foreign_posts:
        for value in values or []:
            foreign_name = _parse_url(value, bucket)
            if foreign_name in names:
                raise MediaProviderUnavailable("media_reference_foreign_owner")
    foreign_avatars = (await session.execute(
        select(models.User.avatar_url)
        .where(models.User.id != user_id, models.User.avatar_url.is_not(None))
        .limit(MAX_POSTS + 1)
    )).scalars().all()
    if len(foreign_avatars) > MAX_POSTS:
        raise MediaProviderUnavailable("media_foreign_reference_cap_reached")
    if any(_parse_url(value, bucket) in names for value in foreign_avatars):
        raise MediaProviderUnavailable("media_reference_foreign_owner")
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
    await _owned_references(session, user_id, bucket)
    deadline = time.monotonic() + deadline_seconds
    found: dict[tuple[str, str], MediaObject] = {}
    for prefix in PREFIXES:
        scoped_prefix = f"{prefix}{user_id}/"
        rows = await _list_bounded(reader, scoped_prefix, deadline=deadline, limit=max_objects - len(found))
        for raw in rows:
            item = _metadata(raw)
            if not _owned_name(item.name, user_id) or not item.name.startswith(scoped_prefix):
                raise MediaProviderUnavailable("media_object_scope_invalid")
            found[(item.name, item.generation)] = MediaObject(item.name, item.generation)
            if len(found) > max_objects:
                raise MediaProviderUnavailable("media_object_cap_reached")
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

    def __init__(self, reader: MediaReader, *, bucket: str, snapshot: MediaSnapshot, freeze: FreezeReceipt):
        self._reader = reader
        self.bucket = bucket
        self._snapshot = snapshot
        self._freeze = freeze

    def _validate_snapshot(self, user: models.User) -> None:
        try:
            canonical_user = str(UUID(str(user.id)))
        except (ValueError, TypeError, AttributeError):
            raise MediaProviderUnavailable("media_subject_invalid") from None
        if self._snapshot.user_id != canonical_user or self._snapshot.bucket != self.bucket:
            raise MediaProviderUnavailable("media_snapshot_subject_mismatch")
        if self._snapshot.prefixes != PREFIXES:
            raise MediaProviderUnavailable("media_snapshot_scope_invalid")
        if len(self._snapshot.objects) > MAX_OBJECTS:
            raise MediaProviderUnavailable("media_object_cap_reached")
        pairs = {(item.name, item.generation) for item in self._snapshot.objects}
        if len(pairs) != len(self._snapshot.objects):
            raise MediaProviderUnavailable("media_snapshot_invalid")
        for item in self._snapshot.objects:
            if not _owned_name(item.name, canonical_user) or not _generation(item.generation):
                raise MediaProviderUnavailable("media_snapshot_scope_invalid")
        if _digest(self._snapshot.payload()) != self._snapshot.snapshot_digest:
            raise MediaProviderUnavailable("media_snapshot_digest_invalid")
        self._freeze.validate(canonical_user, self.bucket)
        if self._freeze.freeze_id != self._snapshot.freeze_id:
            raise MediaProviderUnavailable("media_freeze_mismatch")

    async def delete_account_data(
        self,
        *,
        user: models.User,
        idempotency_key: str,
        deadline_seconds: float = 120.0,
    ) -> dict[str, object]:
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise MediaProviderUnavailable("media_idempotency_key_invalid")
        self._validate_snapshot(user)
        snapshot = self._snapshot
        freeze = self._freeze
        if not math.isfinite(deadline_seconds) or not 1 <= deadline_seconds <= MAX_SECONDS:
            raise ValueError("media_deadline_invalid")
        deadline = time.monotonic() + deadline_seconds
        expected = {(item.name, item.generation) for item in snapshot.objects}
        current: dict[str, MediaObject] = {}
        for prefix in PREFIXES:
            rows = await _list_bounded(self._reader, f"{prefix}{user.id}/", deadline=deadline, limit=MAX_OBJECTS - len(current))
            for raw in rows:
                item = _metadata(raw)
                current[item.name] = MediaObject(item.name, item.generation)
        expected_names = {name for name, _ in expected}
        if set(current) - expected_names:
            raise MediaProviderUnavailable("media_snapshot_drift")
        if any(
            name in current and generation != current[name].generation
            for name, generation in expected
        ):
            raise MediaProviderUnavailable("media_snapshot_drift")
        for item in snapshot.objects:
            self._freeze.validate(user.id, self.bucket)
            observed = await _reader_call(self._reader, "read", item.name, deadline=deadline)
            if observed is not None and (observed.name, observed.generation) != (item.name, item.generation):
                raise MediaProviderUnavailable("media_generation_mismatch")
        for item in snapshot.objects:
            self._freeze.validate(user.id, self.bucket)
            observed = await _reader_call(self._reader, "read", item.name, deadline=deadline)
            if observed is None:
                continue
            if (observed.name, observed.generation) != (item.name, item.generation):
                raise MediaProviderUnavailable("media_generation_mismatch")
            await _reader_call(self._reader, "delete", item.name, item.generation, deadline=deadline)
            after = await _reader_call(self._reader, "read", item.name, deadline=deadline)
            if after is not None:
                raise MediaProviderUnavailable("media_readback_failed")
        self._freeze.validate(user.id, self.bucket)
        final = {}
        for prefix in PREFIXES:
            for raw in await _list_bounded(self._reader, f"{prefix}{user.id}/", deadline=deadline, limit=MAX_OBJECTS - len(final)):
                item = _metadata(raw)
                final[item.name] = item.generation
        if final:
            raise MediaProviderUnavailable("media_reappeared")
        return {
            "confirmed": True,
            "provider": "media_storage",
            "subject": str(user.id),
            "scope": [f"{prefix}{user.id}/" for prefix in PREFIXES],
            "object_count": len(snapshot.objects),
            "snapshot_digest": snapshot.snapshot_digest,
            "readback": True,
        }
