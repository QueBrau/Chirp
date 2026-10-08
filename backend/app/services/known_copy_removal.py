"""Generation-bound, operator-reviewed removal plans for known-copy receipts.

This module is deliberately separate from discovery.  A complete inventory is
required to make a plan, every delete is conditional on the reviewed generation,
and retries never add objects to the reviewed scope.  Provider calls are explicit
and are never made by the case register or an API request.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from app.services.known_copy_inventory import KnownCopyInventory, ManifestObject, ObjectMetadata
from app.services import storage_service

SCHEMA_VERSION = 1
REVIEWED_PREFIXES = ("posts/",)
_REASONS = frozenset({
    "deadline_exceeded", "provider_delete_failed", "provider_read_failed",
    "generation_mismatch", "reappeared", "receipt_mismatch", "receipt_invalid",
    "scope_invalid", "inventory_incomplete", "object_invalid", "already_absent",
})


class RemovalError(Exception):
    """Fixed-code error safe to expose to an operator."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason if reason in _REASONS else "provider_delete_failed"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _generation(value: str | None) -> str | None:
    if value is None or not str(value).isdigit() or int(value) <= 0:
        return None
    return str(value)


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class RemovalObject:
    name: str
    generation: str
    sha256: str

    def payload(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class RemovalPlan:
    bucket: str
    target_name: str
    target_generation: str
    manifest_digest: str
    allowed_prefixes: tuple[str, ...]
    objects: tuple[RemovalObject, ...]
    created_at: str
    plan_digest: str

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "bucket": self.bucket,
            "target_name": self.target_name,
            "target_generation": self.target_generation,
            "manifest_digest": self.manifest_digest,
            "allowed_prefixes": list(self.allowed_prefixes),
            "objects": [item.payload() for item in self.objects],
            "created_at": self.created_at,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.payload() | {"plan_digest": self.plan_digest}


@dataclass(frozen=True)
class RemovalOutcome:
    object: RemovalObject
    status: str
    reason: str | None
    observed_generation: str | None
    updated_at: str

    def payload(self) -> dict[str, Any]:
        return {
            "object": self.object.payload(),
            "status": self.status,
            "reason": self.reason,
            "observed_generation": self.observed_generation,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class RemovalReceipt:
    plan_digest: str
    bucket: str
    started_at: str
    updated_at: str
    outcomes: tuple[RemovalOutcome, ...]
    receipt_digest: str

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "plan_digest": self.plan_digest,
            "bucket": self.bucket,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "outcomes": [item.payload() for item in self.outcomes],
            "complete": all(item.status in {"removed_verified", "already_absent"} for item in self.outcomes),
        }

    def to_dict(self) -> dict[str, Any]:
        return self.payload() | {"receipt_digest": self.receipt_digest}


class RemovalProvider(Protocol):
    def delete(self, name: str, generation: str, *, deadline: float) -> None: ...
    def read(self, name: str, *, deadline: float) -> ObjectMetadata | None: ...


class GCSRemovalProvider:
    """Explicit GCS adapter; no operation is invoked during plan creation."""

    def __init__(self, bucket: str):
        if not bucket or "/" in bucket:
            raise ValueError("bucket_invalid")
        self._bucket = storage_service._storage_client().bucket(bucket)

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RemovalError("deadline_exceeded")
        return max(0.01, remaining)

    def delete(self, name: str, generation: str, *, deadline: float) -> None:
        if _generation(generation) is None:
            raise RemovalError("object_invalid")
        blob = self._bucket.blob(name)
        try:
            blob.delete(if_generation_match=int(generation), timeout=self._remaining(deadline), retry=None)
        except RemovalError:
            raise
        except Exception as exc:
            if getattr(exc, "code", None) == 404:
                return
            if getattr(exc, "code", None) == 412:
                raise RemovalError("generation_mismatch") from None
            raise RemovalError("provider_delete_failed") from None

    def read(self, name: str, *, deadline: float) -> ObjectMetadata | None:
        blob = self._bucket.blob(name)
        try:
            blob.reload(timeout=self._remaining(deadline), retry=None)
            generation = _generation(str(blob.generation) if blob.generation is not None else None)
            if generation is None:
                raise RemovalError("provider_read_failed")
            return ObjectMetadata(str(blob.name), generation, int(blob.size) if blob.size is not None else None, getattr(blob, "md5_hash", None), getattr(blob, "crc32c", None))
        except RemovalError:
            raise
        except Exception as exc:
            if getattr(exc, "code", None) == 404:
                return None
            raise RemovalError("provider_read_failed") from None


def build_removal_plan(inventory: KnownCopyInventory, *, allowed_prefixes: tuple[str, ...] = REVIEWED_PREFIXES) -> RemovalPlan:
    """Create an immutable plan only from a complete reviewed inventory."""
    if not inventory.complete:
        raise RemovalError("inventory_incomplete")
    if not allowed_prefixes or any(prefix not in inventory.scope for prefix in allowed_prefixes):
        raise RemovalError("scope_invalid")
    target = inventory.target
    generation = _generation(inventory.requested_generation)
    if target is None or generation is None or target.metadata.name != inventory.requested_target_name or target.metadata.generation != generation:
        raise RemovalError("object_invalid")
    if not any(target.metadata.name.startswith(prefix) for prefix in allowed_prefixes):
        raise RemovalError("scope_invalid")
    objects: dict[tuple[str, str], RemovalObject] = {}
    for item in inventory.matches:
        item_generation = _generation(item.metadata.generation)
        if not any(item.metadata.name.startswith(prefix) for prefix in allowed_prefixes):
            continue
        if item_generation is None or not item.sha256 or len(item.sha256) != 64:
            raise RemovalError("object_invalid")
        objects[(item.metadata.name, item_generation)] = RemovalObject(item.metadata.name, item_generation, item.sha256)
    target_key = (target.metadata.name, generation)
    if target_key not in objects:
        raise RemovalError("object_invalid")
    created = _now()
    draft = RemovalPlan(inventory.bucket, inventory.requested_target_name, generation, inventory.manifest_digest, tuple(allowed_prefixes), tuple(sorted(objects.values(), key=lambda item: (item.name, item.generation))), created, "")
    return replace(draft, plan_digest=_digest(draft.payload()))


def _receipt_from_payload(raw: dict[str, Any]) -> RemovalReceipt:
    try:
        outcomes = tuple(RemovalOutcome(RemovalObject(**item["object"]), item["status"], item.get("reason"), item.get("observed_generation"), item["updated_at"]) for item in raw["outcomes"])
        receipt = RemovalReceipt(raw["plan_digest"], raw["bucket"], raw["started_at"], raw["updated_at"], outcomes, raw["receipt_digest"])
        if _digest(receipt.payload()) != receipt.receipt_digest:
            raise RemovalError("receipt_invalid")
        return receipt
    except RemovalError:
        raise
    except Exception:
        raise RemovalError("receipt_invalid") from None


def _write_receipt(path: Path, receipt: RemovalReceipt) -> None:
    parent = path.parent
    if path.is_symlink() or (parent.exists() and parent.is_symlink()):
        raise RemovalError("receipt_invalid")
    missing: list[Path] = []
    cursor = parent
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    if cursor.is_symlink():
        raise RemovalError("receipt_invalid")
    for directory in reversed(missing):
        os.mkdir(directory, 0o700)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(receipt.to_dict(), handle, indent=2)
            handle.write("\n")
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise RemovalError("receipt_invalid") from None


def _load_receipt(path: Path) -> RemovalReceipt | None:
    if not path.exists():
        return None
    if path.is_symlink():
        raise RemovalError("receipt_invalid")
    try:
        with path.open(encoding="utf-8") as handle:
            return _receipt_from_payload(json.load(handle))
    except RemovalError:
        raise
    except Exception:
        raise RemovalError("receipt_invalid") from None


def _new_receipt(plan: RemovalPlan) -> RemovalReceipt:
    receipt = RemovalReceipt(plan.plan_digest, plan.bucket, _now(), _now(), tuple(RemovalOutcome(item, "pending", None, None, _now()) for item in plan.objects), "")
    return replace(receipt, receipt_digest=_digest(receipt.payload()))


def _save(receipt: RemovalReceipt, path: Path) -> RemovalReceipt:
    updated = replace(receipt, updated_at=_now(), receipt_digest="")
    updated = replace(updated, receipt_digest=_digest(updated.payload()))
    _write_receipt(path, updated)
    return updated


def execute_removal(plan: RemovalPlan, provider: RemovalProvider, receipt_path: Path, *, deadline_seconds: float = 60.0) -> RemovalReceipt:
    """Delete only reviewed generations and persist each outcome before continuing."""
    if deadline_seconds <= 0 or deadline_seconds > 900:
        raise ValueError("deadline_seconds_out_of_range")
    receipt = _load_receipt(receipt_path)
    if receipt is not None and (receipt.plan_digest != plan.plan_digest or receipt.bucket != plan.bucket or tuple(item.object for item in receipt.outcomes) != plan.objects):
        raise RemovalError("receipt_mismatch")
    receipt = receipt or _new_receipt(plan)
    deadline = time.monotonic() + deadline_seconds
    outcomes = list(receipt.outcomes)
    for index, item in enumerate(plan.objects):
        if time.monotonic() >= deadline:
            outcomes[index] = replace(outcomes[index], status="failed", reason="deadline_exceeded", updated_at=_now())
            receipt = _save(replace(receipt, outcomes=tuple(outcomes)), receipt_path)
            break
        current = outcomes[index]
        try:
            observed = provider.read(item.name, deadline=deadline)
            if observed is not None and observed.generation != item.generation:
                outcomes[index] = replace(current, status="reappeared", reason="reappeared", observed_generation=observed.generation, updated_at=_now())
                receipt = _save(replace(receipt, outcomes=tuple(outcomes)), receipt_path)
                continue
            if observed is None and current.status in {"removed_verified", "already_absent"}:
                continue
            was_present = observed is not None
            provider.delete(item.name, item.generation, deadline=deadline)
            observed = provider.read(item.name, deadline=deadline)
            if observed is None:
                status = "removed_verified" if was_present else "already_absent"
                outcomes[index] = replace(current, status=status, reason=None, observed_generation=None, updated_at=_now())
            elif observed.generation != item.generation:
                outcomes[index] = replace(current, status="reappeared", reason="reappeared", observed_generation=observed.generation, updated_at=_now())
            else:
                outcomes[index] = replace(current, status="failed", reason="generation_mismatch", observed_generation=observed.generation, updated_at=_now())
        except RemovalError as exc:
            outcomes[index] = replace(current, status="failed", reason=exc.reason, updated_at=_now())
        receipt = _save(replace(receipt, outcomes=tuple(outcomes)), receipt_path)
    return receipt


def verify_removal(receipt: RemovalReceipt, provider: RemovalProvider, *, deadline_seconds: float = 60.0) -> RemovalReceipt:
    """Read only the reviewed names and flag any replacement generation."""
    if deadline_seconds <= 0 or deadline_seconds > 900:
        raise ValueError("deadline_seconds_out_of_range")
    deadline = time.monotonic() + deadline_seconds
    outcomes = list(receipt.outcomes)
    for index, current in enumerate(outcomes):
        try:
            observed = provider.read(current.object.name, deadline=deadline)
            if observed is not None:
                outcomes[index] = replace(current, status="reappeared", reason="reappeared", observed_generation=observed.generation, updated_at=_now())
        except RemovalError as exc:
            outcomes[index] = replace(current, status="failed", reason=exc.reason, updated_at=_now())
    updated = replace(receipt, outcomes=tuple(outcomes), updated_at=_now(), receipt_digest="")
    return replace(updated, receipt_digest=_digest(updated.payload()))


def receipt_reference(receipt: RemovalReceipt) -> str:
    """Return the only case-tool reference accepted for a complete removal receipt."""
    if not all(item.status in {"removed_verified", "already_absent"} for item in receipt.outcomes):
        raise RemovalError("receipt_invalid")
    return f"c437-removal:{receipt.receipt_digest}"
