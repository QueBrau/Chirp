"""Generation-bound, operator-reviewed removal plans for known-copy receipts.

This module is deliberately separate from discovery.  A complete inventory is
required to make a plan, every delete is conditional on the reviewed generation,
and retries never add objects to the reviewed scope.  Provider calls are explicit
and are never made by the case register or an API request.
"""
from __future__ import annotations

import hashlib
import argparse
import json
import os
import math
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from app.services.known_copy_inventory import KnownCopyInventory, ManifestObject, ObjectMetadata, ScanLimits
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
    verification_complete: bool
    verified_at: str | None
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
            "verification_complete": self.verification_complete,
            "verified_at": self.verified_at,
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
    if allowed_prefixes != REVIEWED_PREFIXES or any(prefix not in inventory.scope for prefix in allowed_prefixes):
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


def load_inventory(path: Path) -> KnownCopyInventory:
    """Load and digest-check a private inventory manifest produced by discovery."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            raise RemovalError("receipt_invalid")
        with path.open(encoding="utf-8") as handle:
            raw = json.load(handle)
        digest = raw.pop("manifest_digest")
        def manifest_object(item: dict[str, Any]) -> ManifestObject:
            metadata = ObjectMetadata(**{key: item[key] for key in ("name", "generation", "size", "md5_hash", "crc32c")})
            return ManifestObject(metadata, item["sha256"])

        target_raw = raw.get("target")
        target = manifest_object(target_raw) if target_raw else None
        matches = tuple(manifest_object(item) for item in raw["matches"])
        inventory = KnownCopyInventory(raw["schema_version"], raw["bucket"], raw["requested_target_name"], raw["requested_generation"], target, matches, tuple(raw["scope"]), raw["consistency"], ScanLimits(**raw["limits"]), raw["started_at"], raw["finished_at"], raw["scanned_objects"], raw["downloaded_bytes"], raw["complete"], tuple(raw["incomplete_reasons"]), raw["deletion_authorized"], digest)
        if _digest(inventory.payload()) != digest:
            raise RemovalError("receipt_invalid")
        return inventory
    except RemovalError:
        raise
    except Exception:
        raise RemovalError("receipt_invalid") from None


def load_plan(path: Path) -> RemovalPlan:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            raise RemovalError("receipt_invalid")
        with path.open(encoding="utf-8") as handle:
            raw = json.load(handle)
        plan = RemovalPlan(raw["bucket"], raw["target_name"], raw["target_generation"], raw["manifest_digest"], tuple(raw["allowed_prefixes"]), tuple(RemovalObject(**item) for item in raw["objects"]), raw["created_at"], raw["plan_digest"])
        validate_plan(plan)
        return plan
    except RemovalError:
        raise
    except Exception:
        raise RemovalError("receipt_invalid") from None


def _is_hex_digest(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def validate_plan(plan: RemovalPlan) -> None:
    """Revalidate all reviewed bindings before every provider mutation."""
    if not isinstance(plan.bucket, str) or not plan.bucket or "/" in plan.bucket:
        raise RemovalError("scope_invalid")
    if plan.allowed_prefixes != REVIEWED_PREFIXES or not isinstance(plan.target_name, str) or not plan.target_name.startswith("posts/"):
        raise RemovalError("scope_invalid")
    if _generation(plan.target_generation) != plan.target_generation or not _is_hex_digest(plan.manifest_digest):
        raise RemovalError("object_invalid")
    if not plan.objects or len({(item.name, item.generation) for item in plan.objects}) != len(plan.objects):
        raise RemovalError("object_invalid")
    target = None
    for item in plan.objects:
        if not isinstance(item.name, str) or not item.name.startswith("posts/") or len(item.name) > 512 or _generation(item.generation) != item.generation or not _is_hex_digest(item.sha256):
            raise RemovalError("object_invalid")
        if (item.name, item.generation) == (plan.target_name, plan.target_generation):
            target = item
    if target is None:
        raise RemovalError("object_invalid")
    if not isinstance(plan.created_at, str) or not plan.plan_digest == _digest(plan.payload()):
        raise RemovalError("receipt_invalid")


def _receipt_from_payload(raw: dict[str, Any]) -> RemovalReceipt:
    try:
        outcomes = tuple(RemovalOutcome(RemovalObject(**item["object"]), item["status"], item.get("reason"), item.get("observed_generation"), item["updated_at"]) for item in raw["outcomes"])
        if not outcomes or len({(item.object.name, item.object.generation) for item in outcomes}) != len(outcomes):
            raise RemovalError("receipt_invalid")
        if any(item.status not in {"pending", "removed_verified", "already_absent", "failed", "reappeared"} for item in outcomes):
            raise RemovalError("receipt_invalid")
        receipt = RemovalReceipt(raw["plan_digest"], raw["bucket"], raw["started_at"], raw["updated_at"], outcomes, bool(raw.get("verification_complete", False)), raw.get("verified_at"), raw["receipt_digest"])
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
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise RemovalError("receipt_invalid") from None


def write_receipt(path: Path, receipt: RemovalReceipt) -> None:
    """Persist an operator-reviewed receipt with a restrictive mode."""
    if not receipt.outcomes or any(item.status not in {"pending", "removed_verified", "already_absent", "failed", "reappeared"} for item in receipt.outcomes) or _digest(receipt.payload()) != receipt.receipt_digest:
        raise RemovalError("receipt_invalid")
    _write_receipt(path, receipt)


@contextmanager
def _receipt_lock(path: Path):
    lock_path = Path(f"{path}.lock")
    parent = lock_path.parent
    missing: list[Path] = []
    cursor = parent
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    if cursor.is_symlink():
        raise RemovalError("receipt_invalid")
    for directory in reversed(missing):
        os.mkdir(directory, 0o700)
    try:
        fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError:
        raise RemovalError("receipt_invalid") from None
    try:
        os.fsync(fd)
        os.close(fd)
        yield
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass


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
    receipt = RemovalReceipt(plan.plan_digest, plan.bucket, _now(), _now(), tuple(RemovalOutcome(item, "pending", None, None, _now()) for item in plan.objects), False, None, "")
    return replace(receipt, receipt_digest=_digest(receipt.payload()))


def _save(receipt: RemovalReceipt, path: Path) -> RemovalReceipt:
    updated = replace(receipt, updated_at=_now(), receipt_digest="")
    updated = replace(updated, receipt_digest=_digest(updated.payload()))
    _write_receipt(path, updated)
    return updated


def execute_removal(plan: RemovalPlan, provider: RemovalProvider, receipt_path: Path, *, deadline_seconds: float = 60.0) -> RemovalReceipt:
    with _receipt_lock(receipt_path):
        return _execute_removal(plan, provider, receipt_path, deadline_seconds=deadline_seconds)


def _execute_removal(plan: RemovalPlan, provider: RemovalProvider, receipt_path: Path, *, deadline_seconds: float = 60.0) -> RemovalReceipt:
    """Delete only reviewed generations and persist each outcome before continuing."""
    validate_plan(plan)
    if not math.isfinite(deadline_seconds) or deadline_seconds <= 0 or deadline_seconds > 900:
        raise ValueError("deadline_seconds_out_of_range")
    receipt = _load_receipt(receipt_path)
    if receipt is not None and (receipt.plan_digest != plan.plan_digest or receipt.bucket != plan.bucket or tuple(item.object for item in receipt.outcomes) != plan.objects):
        raise RemovalError("receipt_mismatch")
    if receipt is None:
        receipt = _save(_new_receipt(plan), receipt_path)
    deadline = time.monotonic() + deadline_seconds
    outcomes = list(receipt.outcomes)
    for index, item in enumerate(plan.objects):
        if time.monotonic() >= deadline:
            outcomes[index] = replace(outcomes[index], status="failed", reason="deadline_exceeded", updated_at=_now())
            receipt = _save(replace(receipt, outcomes=tuple(outcomes), verification_complete=False, verified_at=None), receipt_path)
            break
        current = outcomes[index]
        try:
            observed = provider.read(item.name, deadline=deadline)
            if observed is not None and observed.generation != item.generation:
                outcomes[index] = replace(current, status="reappeared", reason="reappeared", observed_generation=observed.generation, updated_at=_now())
                receipt = _save(replace(receipt, outcomes=tuple(outcomes), verification_complete=False, verified_at=None), receipt_path)
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
        receipt = _save(replace(receipt, outcomes=tuple(outcomes), verification_complete=False, verified_at=None), receipt_path)
    return receipt


def verify_removal(receipt: RemovalReceipt, provider: RemovalProvider, *, deadline_seconds: float = 60.0) -> RemovalReceipt:
    """Read only the reviewed names and flag any replacement generation."""
    if not math.isfinite(deadline_seconds) or deadline_seconds <= 0 or deadline_seconds > 900:
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
    complete = all(item.status in {"removed_verified", "already_absent"} for item in outcomes)
    updated = replace(receipt, outcomes=tuple(outcomes), updated_at=_now(), verification_complete=complete, verified_at=_now() if complete else None, receipt_digest="")
    return replace(updated, receipt_digest=_digest(updated.payload()))


def receipt_reference(receipt: RemovalReceipt) -> str:
    """Return the only case-tool reference accepted for a complete removal receipt."""
    if not receipt.verification_complete or not receipt.verified_at or not receipt.outcomes or _digest(receipt.payload()) != receipt.receipt_digest or not all(item.status in {"removed_verified", "already_absent"} for item in receipt.outcomes):
        raise RemovalError("receipt_invalid")
    return f"c437-removal:{receipt.receipt_digest}"


def _write_plan(path: Path, plan: RemovalPlan) -> None:
    if path.exists() or path.is_symlink():
        raise RemovalError("receipt_invalid")
    parent = path.parent
    missing: list[Path] = []
    cursor = parent
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    if cursor.is_symlink():
        raise RemovalError("receipt_invalid")
    for directory in reversed(missing):
        os.mkdir(directory, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(plan.to_dict(), handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise RemovalError("receipt_invalid") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reviewed, generation-bound c437 media removal")
    sub = parser.add_subparsers(dest="command", required=True)
    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("--inventory", type=Path, required=True)
    plan_parser.add_argument("--output", type=Path, required=True)
    execute_parser = sub.add_parser("execute")
    execute_parser.add_argument("--plan", type=Path, required=True)
    execute_parser.add_argument("--receipt", type=Path, required=True)
    execute_parser.add_argument("--confirm-plan-digest", required=True)
    execute_parser.add_argument("--allow-provider-delete", action="store_true")
    execute_parser.add_argument("--deadline-seconds", type=float, default=60.0)
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--plan", type=Path, required=True)
    verify_parser.add_argument("--receipt", type=Path, required=True)
    verify_parser.add_argument("--deadline-seconds", type=float, default=60.0)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            plan = build_removal_plan(load_inventory(args.inventory))
            _write_plan(args.output, plan)
            print(json.dumps({"plan_digest": plan.plan_digest, "objects": len(plan.objects), "mode": "review_only"}, sort_keys=True))
            return 0
        plan = load_plan(args.plan)
        if args.command == "execute":
            if not args.allow_provider_delete or args.confirm_plan_digest != plan.plan_digest:
                raise RemovalError("receipt_mismatch")
            receipt = execute_removal(plan, GCSRemovalProvider(plan.bucket), args.receipt, deadline_seconds=args.deadline_seconds)
        else:
            with _receipt_lock(args.receipt):
                receipt = _load_receipt(args.receipt)
                if receipt is None or receipt.plan_digest != plan.plan_digest or tuple(item.object for item in receipt.outcomes) != plan.objects:
                    raise RemovalError("receipt_mismatch")
                receipt = verify_removal(receipt, GCSRemovalProvider(plan.bucket), deadline_seconds=args.deadline_seconds)
                write_receipt(args.receipt, receipt)
        print(json.dumps({"complete": receipt.verification_complete, "receipt_digest": receipt.receipt_digest, "outcomes": len(receipt.outcomes)}, sort_keys=True))
        return 0 if receipt.verification_complete else 2
    except Exception:
        print(json.dumps({"complete": False, "error": "invalid_or_unavailable_removal"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
