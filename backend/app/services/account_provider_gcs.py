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
import os
import re
import time
import asyncio
from dataclasses import dataclass
from typing import Any, Iterable, Protocol
from urllib.parse import unquote, urlsplit
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.services.account_fulfillment import ProviderUnavailable
from app.services.account_post_deletion_cleanup import DeferredCleanupReceipt


PREFIXES = ("tmp/", "posts/", "avatars/")
MAX_OBJECTS = 10_000
MAX_POSTS = 10_000
MAX_MEDIA_VALUES = 64
MAX_SECONDS = 120.0
DISABLE_BUCKET_METADATA = "DISABLE_GCS_PYTHON_CLIENT_OTEL_BUCKET_METADATA"


class MediaProviderUnavailable(ProviderUnavailable):
    pass


@dataclass(frozen=True)
class FreezeReceipt:
    user_id: str
    bucket: str
    freeze_id: str
    expires_at: float
    verified: bool = False

    def validate(self, user_id: UUID | str, bucket: str) -> None:
        if (
            type(self.verified) is not bool
            or self.verified is not True
            or str(self.user_id) != str(user_id)
            or self.bucket != bucket
            or not isinstance(self.freeze_id, str)
            or not 1 <= len(self.freeze_id) <= 128
            or isinstance(self.expires_at, bool)
            or not isinstance(self.expires_at, (int, float))
            or not math.isfinite(self.expires_at)
            or self.expires_at <= time.time()
        ):
            raise MediaProviderUnavailable("media_freeze_invalid")


@dataclass(frozen=True)
class MediaRetentionReview:
    """Explicit operator review of bucket policy, not automatic policy discovery."""

    bucket: str
    versioning_enabled: bool
    soft_delete_seconds: int
    verified: bool = False

    def receipt_codes(self, bucket: str) -> list[str]:
        if (
            self.verified is not True
            or self.bucket != bucket
            or self.versioning_enabled is not False
            or type(self.soft_delete_seconds) is not int
            or not 0 <= self.soft_delete_seconds <= 2**31 - 1
        ):
            # This live-object adapter cannot inventory or erase noncurrent
            # versions. Unknown/unreviewed policies never imply full erasure.
            raise MediaProviderUnavailable("media_retention_review_required")
        return ["gcs_soft_deleted_versions"] if self.soft_delete_seconds else []


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
    bucket_name: str
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
    return await _thread_call(_collect, reader, prefix, deadline, limit, deadline=deadline, error="media_list_failed")


async def _thread_call(operation: Any, *args: Any, deadline: float, error: str, **kwargs: Any) -> Any:
    # A running SDK thread cannot be cancelled. Drain it before returning an
    # uncertain result; never leave a delete running behind a released lease.
    timeout = _remaining(deadline)
    task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except MediaProviderUnavailable:
        raise
    except (asyncio.TimeoutError, asyncio.CancelledError) as original:
        # Repeated cancellation must also not detach the worker. SDK calls use
        # finite request timeouts and no retries; remote mutation can nevertheless
        # have succeeded even when its response was lost.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if task.done() and not task.cancelled():
            task.exception()  # consume a sanitized/remote failure without logging it
        if isinstance(original, asyncio.CancelledError):
            raise original
        raise MediaProviderUnavailable("media_deadline_exceeded") from None
    except Exception:
        raise MediaProviderUnavailable(error) from None


async def _reader_call(reader: MediaReader, operation: str, *args: Any, deadline: float) -> Any:
    from functools import partial
    return await _thread_call(partial(getattr(reader, operation), deadline=deadline), *args,
                              deadline=deadline, error=f"media_{operation}_failed")


def _digest(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise MediaProviderUnavailable("media_deadline_exceeded")
    return max(0.01, remaining)


def _generation(value: Any) -> str:
    if (isinstance(value, bool) or not isinstance(value, (str, int))
            or not str(value).isascii() or not 1 <= len(str(value)) <= 20
            or not str(value).isdigit() or int(value) <= 0):
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
    if not isinstance(name, str) or len(name.encode("utf-8")) > 1024:
        return False
    parts = name.split("/", 2)
    return len(parts) == 3 and parts[1] == canonical and bool(parts[2]) and parts[0] + "/" in PREFIXES


def _parse_url(value: str, bucket: str) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.netloc not in {"storage.googleapis.com", "storage.cloud.google.com"}:
        return None
    path = f"https://{parsed.netloc}{parsed.path}"
    prefixes = (
        f"https://storage.googleapis.com/{bucket}/",
        f"https://storage.cloud.google.com/{bucket}/",
        f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/",
    )
    for prefix in prefixes:
        if path.startswith(prefix):
            suffix = path[len(prefix):]
            return unquote(suffix)
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
        if values is not None and len(values) > MAX_MEDIA_VALUES:
            raise MediaProviderUnavailable("media_foreign_reference_cap_reached")
        for value in values or []:
            foreign_name = _parse_url(value, bucket)
            if foreign_name is not None and _owned_name(foreign_name, user_id):
                raise MediaProviderUnavailable("media_reference_foreign_owner")
    foreign_avatars = (await session.execute(
        select(models.User.avatar_url)
        .where(models.User.id != user_id, models.User.avatar_url.is_not(None))
        .limit(MAX_POSTS + 1)
    )).scalars().all()
    if len(foreign_avatars) > MAX_POSTS:
        raise MediaProviderUnavailable("media_foreign_reference_cap_reached")
    if any((name := _parse_url(value, bucket)) is not None and _owned_name(name, user_id) for value in foreign_avatars):
        raise MediaProviderUnavailable("media_reference_foreign_owner")
    return names


async def snapshot_user_media(
    session: AsyncSession,
    user_id: UUID,
    reader: MediaReader,
    freeze: FreezeReceipt | None,
    *,
    bucket: str,
    max_objects: int = MAX_OBJECTS,
    deadline_seconds: float = 120.0,
    deferred_cleanup: bool = False,
    release_db_before_io: bool = False,
) -> MediaSnapshot:
    if not isinstance(bucket, str) or not bucket or getattr(reader, "bucket_name", None) != bucket:
        raise MediaProviderUnavailable("media_reader_bucket_mismatch")
    if type(deferred_cleanup) is not bool:
        raise ValueError("media_deferred_mode_invalid")
    if type(release_db_before_io) is not bool:
        raise ValueError("media_db_release_mode_invalid")
    if release_db_before_io and (session.new or session.dirty or session.deleted):
        raise MediaProviderUnavailable("media_snapshot_session_not_clean")
    if deferred_cleanup:
        if freeze is not None:
            raise MediaProviderUnavailable("media_deferred_freeze_mismatch")
    elif freeze is None:
        raise MediaProviderUnavailable("media_freeze_invalid")
    else:
        freeze.validate(user_id, bucket)
    if isinstance(max_objects, bool) or not isinstance(max_objects, int) or not 1 <= max_objects <= MAX_OBJECTS:
        raise ValueError("media_object_cap_invalid")
    if not math.isfinite(deadline_seconds) or not 1 <= deadline_seconds <= MAX_SECONDS:
        raise ValueError("media_deadline_invalid")
    deadline = time.monotonic() + deadline_seconds
    # DB work is bounded by the same budget; the caller owns rollback on failure.
    async with asyncio.timeout(_remaining(deadline)):
        exists = await session.scalar(select(models.User.id).where(models.User.id == user_id))
        if exists is None:
            raise MediaProviderUnavailable("media_subject_missing")
        await _owned_references(session, user_id, bucket)
    if release_db_before_io:
        # The snapshot reads are complete and hold admission keeps account-owned
        # references stable. Return the connection before the first object-store
        # list request; no SQL transaction spans provider I/O.
        await session.rollback()
    found: dict[tuple[str, str], MediaObject] = {}
    for prefix in PREFIXES:
        if freeze is not None:
            freeze.validate(user_id, bucket)
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
    if len({item.name for item in objects}) != len(objects):
        raise MediaProviderUnavailable("media_snapshot_drift")
    if freeze is not None:
        freeze.validate(user_id, bucket)
    freeze_id = freeze.freeze_id if freeze is not None else "deferred"
    payload = {
        "user_id": str(user_id),
        "bucket": bucket,
        "prefixes": list(PREFIXES),
        "objects": [item.payload() for item in objects],
        "freeze_id": freeze_id,
    }
    return MediaSnapshot(str(user_id), bucket, PREFIXES, objects, freeze_id, _digest(payload))


class GCSMediaReader:
    """Small production GCS reader; every operation receives a finite timeout."""

    def __init__(self, bucket: str, *, project: str | None = None, credentials: Any = None):
        if not isinstance(bucket, str) or not bucket or "/" in bucket:
            raise ValueError("media_bucket_invalid")
        self._require_bounded_sdk()
        from google.cloud import storage
        self.bucket_name = bucket
        # Keep an empty, dedicated SDK metadata cache. A shared runtime client
        # may have cached buckets whose 404 handling queues background requests.
        if project is not None and (not isinstance(project, str) or not project):
            raise MediaProviderUnavailable("media_project_invalid")
        # The worker passes an explicitly selected ADC credential and project.
        # The legacy no-argument form remains available to tests and dormant
        # callers, but is never constructed by the operator job.
        self._bucket = storage.Client(project=project, credentials=credentials).bucket(bucket)

    @staticmethod
    def _require_bounded_sdk() -> None:
        # The SDK otherwise queues daemon bucket-metadata requests with its own
        # default retries, outside the explicit object request deadline. Require
        # process-start configuration; never mutate a shared process environment.
        if os.environ.get(DISABLE_BUCKET_METADATA, "").strip().lower() not in {"1", "true", "yes", "on"}:
            raise MediaProviderUnavailable("media_sdk_background_requests_enabled")

    def reviewed_retention(self, *, expected_soft_delete_seconds: int,
                           expected_versioning_enabled: bool = False) -> MediaRetentionReview:
        """Read the live bucket policy once and require the reviewed values."""
        self._require_bounded_sdk()
        try:
            self._bucket.reload(timeout=10, retry=None)
            policy = getattr(self._bucket, "soft_delete_policy", None)
            actual_soft_delete = getattr(policy, "retention_duration_seconds", 0) if policy else 0
            actual_versioning = bool(getattr(self._bucket, "versioning_enabled", False))
        except Exception:
            raise MediaProviderUnavailable("media_retention_readback_failed") from None
        if (type(expected_soft_delete_seconds) is not int
                or type(expected_versioning_enabled) is not bool
                or actual_soft_delete != expected_soft_delete_seconds
                or actual_versioning != expected_versioning_enabled):
            raise MediaProviderUnavailable("media_retention_policy_mismatch")
        return MediaRetentionReview(
            bucket=self.bucket_name, versioning_enabled=actual_versioning,
            soft_delete_seconds=actual_soft_delete, verified=True,
        )

    def list(self, prefix: str, *, deadline: float) -> Iterable[Any]:
        token = None
        seen_tokens: set[str] = set()
        while True:
            self._require_bounded_sdk()
            # Rebuild each request so a later page cannot reuse an expired
            # timeout calculated before the first page.
            iterator = self._bucket.list_blobs(
                prefix=prefix, max_results=1000, page_size=1000, page_token=token,
                timeout=_remaining(deadline), retry=None,
            )
            page = next(iterator.pages, None)
            if page is not None:
                yield from page
            token = iterator.next_page_token
            if not token:
                return
            if token in seen_tokens:
                raise MediaProviderUnavailable("media_list_failed")
            seen_tokens.add(token)

    def read(self, name: str, *, deadline: float) -> MediaMetadata | None:
        self._require_bounded_sdk()
        blob = self._bucket.blob(name)
        try:
            blob.reload(timeout=_remaining(deadline), retry=None)
        except Exception as exc:
            if getattr(exc, "code", None) == 404:
                return None
            raise MediaProviderUnavailable("media_read_failed") from None
        return MediaMetadata(str(blob.name), _generation(blob.generation))

    def delete(self, name: str, generation: str, *, deadline: float) -> None:
        self._require_bounded_sdk()
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

    def __init__(self, reader: MediaReader, *, bucket: str, snapshot: MediaSnapshot, freeze: FreezeReceipt | None,
                 retention: MediaRetentionReview | None = None, deferred_cleanup: bool = False):
        if not isinstance(bucket, str) or not bucket or getattr(reader, "bucket_name", None) != bucket:
            raise MediaProviderUnavailable("media_reader_bucket_mismatch")
        if not isinstance(retention, MediaRetentionReview):
            raise MediaProviderUnavailable("media_retention_review_required")
        retention.receipt_codes(bucket)
        self._reader = reader
        self.bucket = bucket
        self._snapshot = snapshot
        self._freeze = freeze
        self._retention = retention
        if type(deferred_cleanup) is not bool:
            raise MediaProviderUnavailable("media_deferred_mode_invalid")
        if deferred_cleanup != (freeze is None):
            raise MediaProviderUnavailable("media_deferred_freeze_mismatch")
        self._deferred_cleanup = deferred_cleanup

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
        pairs = {item.name for item in self._snapshot.objects}
        if len(pairs) != len(self._snapshot.objects):
            raise MediaProviderUnavailable("media_snapshot_invalid")
        for item in self._snapshot.objects:
            if not _owned_name(item.name, canonical_user) or not _generation(item.generation):
                raise MediaProviderUnavailable("media_snapshot_scope_invalid")
        if _digest(self._snapshot.payload()) != self._snapshot.snapshot_digest:
            raise MediaProviderUnavailable("media_snapshot_digest_invalid")
        if self._freeze is not None:
            self._freeze.validate(canonical_user, self.bucket)
        elif not self._deferred_cleanup or self._snapshot.freeze_id != "deferred":
            raise MediaProviderUnavailable("media_freeze_invalid")
        if self._freeze is not None and self._freeze.freeze_id != self._snapshot.freeze_id:
            raise MediaProviderUnavailable("media_freeze_mismatch")

    def _check_freeze(self, user_id: UUID | str) -> None:
        if self._freeze is not None:
            self._freeze.validate(user_id, self.bucket)
        elif not self._deferred_cleanup:
            raise MediaProviderUnavailable("media_freeze_invalid")

    @property
    def deferred_cleanup(self) -> bool:
        return self._deferred_cleanup

    async def delete_account_data(
        self,
        *,
        user: models.User,
        idempotency_key: str,
        deadline_seconds: float = 120.0,
    ) -> dict[str, object]:
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise MediaProviderUnavailable("media_idempotency_key_invalid")
        request_id = ""
        if self._deferred_cleanup:
            match = re.fullmatch(r"account-deletion:([0-9a-f-]{36}):media_storage", idempotency_key)
            if match is None:
                raise MediaProviderUnavailable("media_idempotency_key_invalid")
            try:
                request_id = str(UUID(match.group(1)))
            except (ValueError, AttributeError):
                raise MediaProviderUnavailable("media_idempotency_key_invalid") from None
        self._validate_snapshot(user)
        retention_codes = self._retention.receipt_codes(self.bucket)
        snapshot = self._snapshot
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
        added = set(current) - expected_names
        if added and (not self._deferred_cleanup or any(not name.startswith(f"tmp/{user.id}/") for name in added)):
            raise MediaProviderUnavailable("media_snapshot_drift")
        if any(
            name in current and generation != current[name].generation
            for name, generation in expected
            if not self._deferred_cleanup or name.startswith(f"tmp/{user.id}/")
        ):
            raise MediaProviderUnavailable("media_snapshot_drift")
        for item in snapshot.objects:
            self._check_freeze(user.id)
            observed = await _reader_call(self._reader, "read", item.name, deadline=deadline)
            if observed is not None and (observed.name, observed.generation) != (item.name, item.generation):
                raise MediaProviderUnavailable("media_generation_mismatch")
        for item in snapshot.objects:
            self._check_freeze(user.id)
            observed = await _reader_call(self._reader, "read", item.name, deadline=deadline)
            if observed is None:
                continue
            if (observed.name, observed.generation) != (item.name, item.generation):
                raise MediaProviderUnavailable("media_generation_mismatch")
            self._check_freeze(user.id)
            await _reader_call(self._reader, "delete", item.name, item.generation, deadline=deadline)
            after = await _reader_call(self._reader, "read", item.name, deadline=deadline)
            if after is not None:
                raise MediaProviderUnavailable("media_readback_failed")
        self._check_freeze(user.id)
        final = {}
        for prefix in PREFIXES:
            for raw in await _list_bounded(self._reader, f"{prefix}{user.id}/", deadline=deadline, limit=MAX_OBJECTS - len(final)):
                item = _metadata(raw)
                final[item.name] = item.generation
        remaining_tmp = {
            name: generation for name, generation in final.items()
            if name.startswith(f"tmp/{user.id}/")
        }
        if final and (not self._deferred_cleanup or set(final) != set(remaining_tmp)):
            raise MediaProviderUnavailable("media_reappeared")
        self._check_freeze(user.id)
        if self._deferred_cleanup:
            descriptor = {
                "bucket": self.bucket,
                "tmp_prefix": f"tmp/{user.id}/",
                "initial_snapshot_digest": snapshot.snapshot_digest,
            }
            return DeferredCleanupReceipt(
                category="temporary_media", provider="media_storage", subject=str(user.id),
                request_id=request_id,
                scope=descriptor, scope_sha256=_digest(descriptor),
                pass_digest=_digest(sorted(remaining_tmp.items())), readback=True,
                remaining_count=len(remaining_tmp),
                retained_with_reason=tuple(retention_codes),
            )
        return {
            "confirmed": True,
            "provider": "media_storage",
            "subject": str(user.id),
            "scope": [f"{prefix}{user.id}/" for prefix in PREFIXES],
            "object_count": len(snapshot.objects),
            "snapshot_digest": snapshot.snapshot_digest,
            "readback": True,
            "retained_with_reason": retention_codes,
        }
