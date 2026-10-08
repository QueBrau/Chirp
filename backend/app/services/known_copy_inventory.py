"""Bounded, read-only inventory of byte-identical media objects in GCS.

Only the permanent ``posts/`` and ``avatars/`` prefixes are in scope. The adapter
uses generation-bound metadata and downloads directly into a bounded hashing sink;
image bytes are never retained in the manifest, logs, or process memory beyond one
provider chunk. This module has no delete or apply operation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol

from app.services import storage_service

SUPPORTED_PREFIXES = ("posts/", "avatars/")
SCHEMA_VERSION = 2
CHUNK_SIZE = 1024 * 1024
_REASONS = frozenset({
    "generation_required", "target_generation_mismatch", "missing_generation",
    "missing_object_size", "target_missing", "target_read_failed",
    "target_size_cap_reached", "object_size_cap_reached", "download_byte_cap_reached",
    "object_cap_reached", "scan_deadline_exceeded", "target_changed_during_scan",
    "target_recheck_failed", "candidate_read_failed", "provider_list_failed",
    "provider_metadata_failed", "provider_hash_failed", "manifest_write_failed",
})


class InventoryReadError(Exception):
    def __init__(self, reason: str, bytes_seen: int = 0):
        super().__init__(reason)
        self.reason = reason if reason in _REASONS else "provider_hash_failed"
        self.bytes_seen = max(0, int(bytes_seen))


@dataclass(frozen=True)
class ScanLimits:
    max_objects: int = 10_000
    max_object_bytes: int = 50 * 1024 * 1024
    max_download_bytes: int = 512 * 1024 * 1024
    deadline_seconds: float = 60.0

    def validate(self) -> None:
        if not (1 <= self.max_objects <= 1_000_000):
            raise ValueError("max_objects_out_of_range")
        if not (1 <= self.max_object_bytes <= 200 * 1024 * 1024):
            raise ValueError("max_object_bytes_out_of_range")
        if not (1 <= self.max_download_bytes <= 2 * 1024 * 1024 * 1024):
            raise ValueError("max_download_bytes_out_of_range")
        if not (0.1 <= self.deadline_seconds <= 900):
            raise ValueError("deadline_seconds_out_of_range")


@dataclass(frozen=True)
class ObjectMetadata:
    name: str
    generation: str | None
    size: int | None
    md5_hash: str | None
    crc32c: str | None

    def binding(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ManifestObject:
    metadata: ObjectMetadata
    sha256: str

    def binding(self) -> dict[str, Any]:
        return {**self.metadata.binding(), "sha256": self.sha256}


@dataclass(frozen=True)
class KnownCopyInventory:
    schema_version: int
    bucket: str
    requested_target_name: str
    requested_generation: str | None
    target: ManifestObject | None
    matches: tuple[ManifestObject, ...]
    scope: tuple[str, ...]
    consistency: str
    limits: ScanLimits
    started_at: str
    finished_at: str
    scanned_objects: int
    downloaded_bytes: int
    complete: bool
    incomplete_reasons: tuple[str, ...]
    deletion_authorized: bool
    manifest_digest: str

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "bucket": self.bucket,
            "requested_target_name": self.requested_target_name,
            "requested_generation": self.requested_generation,
            "target": self.target.binding() if self.target else None,
            "matches": [item.binding() for item in self.matches],
            "scope": list(self.scope),
            "consistency": self.consistency,
            "limits": asdict(self.limits),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "scanned_objects": self.scanned_objects,
            "downloaded_bytes": self.downloaded_bytes,
            "complete": self.complete,
            "incomplete_reasons": list(self.incomplete_reasons),
            "deletion_authorized": self.deletion_authorized,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.payload() | {"manifest_digest": self.manifest_digest}


class Reader(Protocol):
    def get(self, name: str, *, deadline: float, expected_generation: str | None = None) -> Any: ...
    def list(self, prefix: str, *, deadline: float) -> Iterable[Any]: ...
    def metadata(self, blob: Any) -> ObjectMetadata: ...
    def sha256(self, blob: Any, *, deadline: float, max_bytes: int) -> tuple[str, int]: ...


class _HashSink:
    def __init__(self, *, deadline: float, max_bytes: int):
        self.deadline = deadline
        self.max_bytes = max_bytes
        self.total = 0
        self.digest = hashlib.sha256()

    def write(self, chunk: bytes) -> int:
        attempted = self.total + len(chunk)
        if time.monotonic() >= self.deadline:
            self.total = attempted
            raise InventoryReadError("scan_deadline_exceeded", attempted)
        if attempted > self.max_bytes:
            self.total = attempted
            raise InventoryReadError("object_size_cap_reached", attempted)
        self.total = attempted
        self.digest.update(chunk)
        return len(chunk)

    def flush(self) -> None:
        return None


class GCSReader:
    """Production GCS read adapter. No mutation method is intentionally exposed."""

    def __init__(self, bucket: str):
        if not bucket or "/" in bucket:
            raise ValueError("bucket_invalid")
        self.bucket_name = bucket
        self._bucket = storage_service._storage_client().bucket(bucket)

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise InventoryReadError("scan_deadline_exceeded")
        return max(0.01, remaining)

    def get(self, name: str, *, deadline: float, expected_generation: str | None = None) -> Any:
        blob = self._bucket.blob(name)
        try:
            blob.reload(
                if_generation_match=int(expected_generation) if expected_generation is not None else None,
                timeout=self._remaining(deadline),
                retry=None,
            )
            return blob
        except InventoryReadError:
            raise
        except Exception as exc:
            if getattr(exc, "code", None) == 412:
                raise InventoryReadError("target_generation_mismatch") from None
            raise InventoryReadError("target_read_failed") from None

    def list(self, prefix: str, *, deadline: float) -> Iterable[Any]:
        try:
            page_token = None
            while True:
                iterator = self._bucket.list_blobs(
                    prefix=prefix,
                    page_token=page_token,
                    timeout=self._remaining(deadline),
                    retry=None,
                    page_size=250,
                )
                pages = iter(iterator.pages)
                try:
                    page = next(pages)
                except StopIteration:
                    return
                if time.monotonic() >= deadline:
                    raise InventoryReadError("scan_deadline_exceeded")
                yield from page
                page_token = getattr(iterator, "next_page_token", None)
                if not page_token:
                    return
        except InventoryReadError:
            raise
        except Exception:
            raise InventoryReadError("provider_list_failed") from None

    @staticmethod
    def metadata(blob: Any) -> ObjectMetadata:
        try:
            size = getattr(blob, "size", None)
            return ObjectMetadata(
                name=str(blob.name),
                generation=str(blob.generation) if blob.generation is not None else None,
                size=int(size) if size is not None else None,
                md5_hash=getattr(blob, "md5_hash", None),
                crc32c=getattr(blob, "crc32c", None),
            )
        except Exception:
            raise InventoryReadError("provider_metadata_failed") from None

    @staticmethod
    def sha256(blob: Any, *, deadline: float, max_bytes: int) -> tuple[str, int]:
        if blob.size is None:
            raise InventoryReadError("missing_object_size")
        if blob.generation is None or not str(blob.generation).isdigit() or int(blob.generation) <= 0:
            raise InventoryReadError("missing_generation")
        if blob.size > max_bytes:
            raise InventoryReadError("object_size_cap_reached")
        sink = _HashSink(deadline=deadline, max_bytes=max_bytes)
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InventoryReadError("scan_deadline_exceeded")
            blob.download_to_file(
                sink,
                if_generation_match=int(blob.generation),
                timeout=max(0.01, remaining),
                retry=None,
                raw_download=True,
                checksum="auto",
            )
        except InventoryReadError:
            raise
        except Exception:
            raise InventoryReadError("provider_hash_failed", sink.total) from None
        if sink.total != blob.size:
            raise InventoryReadError("provider_hash_failed", sink.total)
        return sink.digest.hexdigest(), sink.total


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _valid_target(name: str) -> bool:
    return isinstance(name, str) and any(name.startswith(prefix) for prefix in SUPPORTED_PREFIXES) and len(name) <= 512 and "?" not in name and "#" not in name


def _generation(value: str | None) -> str | None:
    if value is None or not str(value).isdigit() or int(value) <= 0:
        return None
    return str(value)


def _result(*, bucket: str, target_name: str, requested_generation: str | None, target: ManifestObject | None, matches: tuple[ManifestObject, ...], limits: ScanLimits, started_at: str, finished_at: str, scanned: int, downloaded: int, reasons: set[str], scope: tuple[str, ...]) -> KnownCopyInventory:
    clean = tuple(sorted(reason for reason in reasons if reason in _REASONS))
    result = KnownCopyInventory(SCHEMA_VERSION, bucket, target_name, requested_generation, target, matches, scope, "per-prefix provider listing; not an atomic bucket snapshot", limits, started_at, finished_at, scanned, downloaded, not clean, clean, False, "")
    return replace(result, manifest_digest=_digest(result.payload()))


def scan_known_copies(reader: Reader, *, bucket: str, target_name: str, expected_generation: str | None = None, limits: ScanLimits | None = None, prefixes: tuple[str, ...] = SUPPORTED_PREFIXES) -> KnownCopyInventory:
    limits = limits or ScanLimits()
    limits.validate()
    if not _valid_target(target_name) or not prefixes or any(not prefix.startswith("posts/") and not prefix.startswith("avatars/") for prefix in prefixes) or any("?" in prefix or "#" in prefix or len(prefix) > 512 for prefix in prefixes) or not any(target_name.startswith(prefix) for prefix in prefixes):
        raise ValueError("target_name_invalid")
    requested_generation = _generation(expected_generation)
    started = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    deadline = time.monotonic() + limits.deadline_seconds
    reasons: set[str] = set()
    downloaded = 0
    target: ManifestObject | None = None
    if requested_generation is None:
        reasons.add("generation_required")
        finished = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return _result(bucket=bucket, target_name=target_name, requested_generation=expected_generation, target=None, matches=(), limits=limits, started_at=started, finished_at=finished, scanned=0, downloaded=0, reasons=reasons, scope=prefixes)
    try:
        blob = reader.get(target_name, deadline=deadline, expected_generation=requested_generation)
        meta = reader.metadata(blob)
        if meta.generation is None:
            reasons.add("missing_generation")
        elif meta.generation != requested_generation:
            reasons.add("target_generation_mismatch")
        elif meta.size is None:
            reasons.add("missing_object_size")
        elif meta.size > limits.max_object_bytes:
            reasons.add("target_size_cap_reached")
        elif meta.size > limits.max_download_bytes:
            reasons.add("download_byte_cap_reached")
        else:
            digest, size = reader.sha256(blob, deadline=deadline, max_bytes=min(limits.max_object_bytes, limits.max_download_bytes))
            downloaded += size
            target = ManifestObject(meta, digest)
    except InventoryReadError as exc:
        reason = exc.reason
        if reason == "object_size_cap_reached" and limits.max_download_bytes < limits.max_object_bytes:
            reason = "download_byte_cap_reached"
        reasons.add(reason)
        downloaded += exc.bytes_seen
    except Exception:
        reasons.add("target_read_failed")
    if target is None:
        finished = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return _result(bucket=bucket, target_name=target_name, requested_generation=requested_generation, target=None, matches=(), limits=limits, started_at=started, finished_at=finished, scanned=0, downloaded=downloaded, reasons=reasons, scope=prefixes)

    matches: dict[tuple[str, str], ManifestObject] = {}
    scanned = 0
    for prefix in prefixes:
        if time.monotonic() >= deadline:
            reasons.add("scan_deadline_exceeded")
            break
        try:
            for blob in reader.list(prefix, deadline=deadline):
                if time.monotonic() >= deadline:
                    reasons.add("scan_deadline_exceeded")
                    break
                if scanned >= limits.max_objects:
                    reasons.add("object_cap_reached")
                    break
                scanned += 1
                hash_cap = limits.max_object_bytes
                try:
                    meta = reader.metadata(blob)
                    if meta.generation is None:
                        reasons.add("missing_generation")
                        continue
                    if meta.size is None:
                        reasons.add("missing_object_size")
                        continue
                    if meta.size != target.metadata.size:
                        continue
                    if meta.md5_hash and target.metadata.md5_hash and meta.md5_hash != target.metadata.md5_hash:
                        continue
                    if downloaded + meta.size > limits.max_download_bytes:
                        reasons.add("download_byte_cap_reached")
                        break
                    remaining_budget = limits.max_download_bytes - downloaded
                    if remaining_budget <= 0:
                        reasons.add("download_byte_cap_reached")
                        break
                    hash_cap = min(limits.max_object_bytes, remaining_budget)
                    if meta.size > hash_cap:
                        reasons.add("download_byte_cap_reached" if remaining_budget < limits.max_object_bytes else "object_size_cap_reached")
                        break
                    digest, size = reader.sha256(blob, deadline=deadline, max_bytes=hash_cap)
                    downloaded += size
                    candidate = ManifestObject(meta, digest)
                    if digest == target.sha256:
                        matches[(meta.name, meta.generation)] = candidate
                except InventoryReadError as exc:
                    reason = exc.reason
                    if reason == "object_size_cap_reached" and hash_cap < limits.max_object_bytes:
                        reason = "download_byte_cap_reached"
                    reasons.add(reason)
                    downloaded += exc.bytes_seen
                    if reason in {"scan_deadline_exceeded", "download_byte_cap_reached"}:
                        break
                except Exception:
                    reasons.add("candidate_read_failed")
        except InventoryReadError as exc:
            reasons.add(exc.reason)
            downloaded += exc.bytes_seen
        except Exception:
            reasons.add("provider_list_failed")
        if reasons & {"scan_deadline_exceeded", "object_cap_reached", "download_byte_cap_reached"}:
            break

    try:
        final = reader.get(target_name, deadline=deadline, expected_generation=requested_generation)
        if reader.metadata(final) != target.metadata:
            reasons.add("target_changed_during_scan")
    except InventoryReadError as exc:
        reasons.add("target_changed_during_scan" if exc.reason == "target_generation_mismatch" else "target_recheck_failed")
    except Exception:
        reasons.add("target_recheck_failed")
    finished = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    ordered = tuple(sorted(matches.values(), key=lambda item: (item.metadata.name, item.metadata.generation or "")))
    return _result(bucket=bucket, target_name=target_name, requested_generation=requested_generation, target=target, matches=ordered, limits=limits, started_at=started, finished_at=finished, scanned=scanned, downloaded=downloaded, reasons=reasons, scope=prefixes)


def write_manifest(path: Path, result: KnownCopyInventory) -> None:
    if path.exists() or path.is_symlink():
        raise OSError("manifest_exists")
    parent = path.parent
    if parent.exists() and parent.is_symlink():
        raise OSError("manifest_parent_symlink")
    missing: list[Path] = []
    cursor = parent
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    if cursor.is_symlink():
        raise OSError("manifest_parent_symlink")
    for directory in reversed(missing):
        os.mkdir(directory, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(result.to_dict(), handle, indent=2)
            handle.write("\n")
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only bounded GCS known-copy inventory")
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--object", dest="target_name", required=True)
    parser.add_argument("--generation", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--max-objects", type=int, default=ScanLimits.max_objects)
    parser.add_argument("--max-object-bytes", type=int, default=ScanLimits.max_object_bytes)
    parser.add_argument("--max-download-bytes", type=int, default=ScanLimits.max_download_bytes)
    parser.add_argument("--deadline-seconds", type=float, default=ScanLimits.deadline_seconds)
    args = parser.parse_args(argv)
    try:
        result = scan_known_copies(GCSReader(args.bucket), bucket=args.bucket, target_name=args.target_name, expected_generation=args.generation, limits=ScanLimits(args.max_objects, args.max_object_bytes, args.max_download_bytes, args.deadline_seconds))
        write_manifest(args.manifest, result)
        print(json.dumps({"complete": result.complete, "matches": len(result.matches), "scanned_objects": result.scanned_objects, "downloaded_bytes": result.downloaded_bytes, "manifest_digest": result.manifest_digest, "incomplete_reasons": list(result.incomplete_reasons)}, sort_keys=True))
        return 0 if result.complete else 2
    except Exception:
        print(json.dumps({"complete": False, "error": "invalid_or_unavailable_inventory"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
