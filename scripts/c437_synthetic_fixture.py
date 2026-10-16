#!/usr/bin/env python3
"""Print or explicitly run a tiny c437 fixture in a disposable GCS bucket."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import time
from pathlib import Path
from typing import Any

APPROVAL = "I_UNDERSTAND_SYNTHETIC_ONLY"
EXPECTED_PROJECT = "chirps-prod"
EXPECTED_PROJECT_NUMBER = "593616178468"
BUCKET_PREFIX = "chirps-c437-fixture-"
NONCE_RE = re.compile(r"^[a-z0-9]{8,24}$")
MAX_OBJECTS = 3
MAX_BYTES = 1024 * 1024
FIXTURE_BODY = b"CHIRP-C437-SYNTHETIC-PUBLIC-FIXTURE\n"


class FixtureError(ValueError):
    """A sanitized, stable refusal reason."""

    def __init__(self, code: str, phase: str = "preflight") -> None:
        self.code = code
        self.phase = phase
        super().__init__(code)


def _args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--project-number", required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--confirm-plan-digest")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approval")
    parser.add_argument("--cleanup", action="store_true")
    return parser.parse_args(argv)


def _bucket_name(project_number: str, nonce: str) -> str:
    if project_number != EXPECTED_PROJECT_NUMBER or not NONCE_RE.fullmatch(nonce):
        raise FixtureError("scope_invalid")
    return f"{BUCKET_PREFIX}{project_number}-{nonce}"


def _validate(args: argparse.Namespace) -> tuple[str, str, str]:
    if args.project != EXPECTED_PROJECT or args.project_number != EXPECTED_PROJECT_NUMBER:
        raise FixtureError("project_mismatch")
    bucket = _bucket_name(args.project_number, args.nonce)
    prefix = f"posts/c437-test-{args.nonce}/"
    return bucket, args.nonce, prefix


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _packet(args: argparse.Namespace, bucket: str, nonce: str, prefix: str) -> dict[str, object]:
    names = [prefix + "target.bin", prefix + "copy.bin", prefix + "replacement.bin"]
    plan = {
        "schema_version": 1,
        "project": EXPECTED_PROJECT,
        "project_number": EXPECTED_PROJECT_NUMBER,
        "bucket": bucket,
        "nonce": nonce,
        "prefix": prefix,
        "synthetic_objects": names,
        "body_sha256": hashlib.sha256(FIXTURE_BODY).hexdigest(),
        "max_objects": MAX_OBJECTS,
        "max_bytes_per_object": MAX_BYTES,
        "cleanup": bool(args.cleanup),
    }
    return {
        "mode": "EXECUTE" if args.execute else "PRINT_ONLY_NOT_EXECUTED",
        **plan,
        "plan_digest": _digest(plan),
        "receipt": str(args.receipt) if args.receipt else None,
        "actions": [
            "require an existing empty purpose-labeled disposable bucket owned by the exact project",
            "upload at most two identical synthetic byte strings with create-only generation preconditions",
            "inventory only the exact posts prefix and write a generation-bound private manifest and plan",
            "delete only reviewed generations with explicit conditional delete and journal receipt",
            "recreate one name to prove replacement generation is reported and not deleted",
            "conditionally clean only this run's exact generations",
        ],
    }


def _validate_receipt_path(path: Path | None) -> Path:
    if path is None or not path.is_absolute():
        raise FixtureError("receipt_path_invalid")
    parent = path.parent
    try:
        parent_info = parent.stat(follow_symlinks=False)
        if not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != os.getuid() or parent_info.st_mode & 0o077:
            raise FixtureError("receipt_directory_not_private")
        if parent.resolve(strict=True) != parent:
            raise FixtureError("receipt_directory_not_private")
    except FixtureError:
        raise
    except Exception:
        raise FixtureError("receipt_directory_unavailable") from None

    outputs = (
        path,
        path.with_name(path.name + ".inventory.json"),
        path.with_name(path.name + ".plan.json"),
        Path(f"{path}.cleanup.json"),
        Path(f"{path}.fixture.lock"),
    )
    for candidate in outputs:
        try:
            os.lstat(candidate)
        except FileNotFoundError:
            continue
        except Exception:
            raise FixtureError("receipt_path_invalid") from None
        raise FixtureError("receipt_path_exists")
    return path


def _validate_execution(args: argparse.Namespace, plan_digest: str) -> Path:
    if not args.execute:
        raise FixtureError("execution_not_requested")
    if args.approval != APPROVAL:
        raise FixtureError("synthetic_approval_required")
    if not isinstance(args.confirm_plan_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", args.confirm_plan_digest):
        raise FixtureError("plan_digest_required")
    if args.confirm_plan_digest != plan_digest:
        raise FixtureError("plan_digest_mismatch")
    if not args.cleanup:
        raise FixtureError("cleanup_confirmation_required")
    if os.environ.get("DISABLE_GCS_PYTHON_CLIENT_OTEL_BUCKET_METADATA", "").lower() != "true":
        raise FixtureError("gcs_bucket_metadata_telemetry_must_be_disabled")
    return _validate_receipt_path(args.receipt)


def _new_storage_client(project: str) -> Any:
    # Imported only after all local scope, approval, digest, and output checks pass.
    from google.cloud import storage

    return storage.Client(project=project)


def _preflight_bucket(client: Any, bucket_name: str, project_number: str) -> Any:
    try:
        bucket = client.bucket(bucket_name)
        bucket.reload(timeout=10, retry=None)
    except Exception:
        raise FixtureError("bucket_metadata_unavailable") from None

    try:
        iam = bucket.iam_configuration
        soft_delete = bucket.soft_delete_policy
        retention = bucket.retention_period
        settings_ok = (
            bucket.name == bucket_name
            and str(bucket.project_number) == project_number
            and (bucket.labels or {}).get("purpose") == "c437-synthetic"
            and iam is not None
            and iam.uniform_bucket_level_access_enabled is True
            and iam.public_access_prevention == "enforced"
            and bucket.versioning_enabled is False
            and soft_delete is not None
            and soft_delete.retention_duration_seconds == 0
            and bucket.default_event_based_hold is not True
            and retention in (None, 0)
            and bucket.retention_policy_locked is not True
            and bucket.object_retention_mode in (None, "Disabled")
        )
    except Exception:
        settings_ok = False
    if not settings_ok:
        raise FixtureError("bucket_safety_metadata_mismatch")

    try:
        if next(iter(bucket.list_blobs(max_results=1, timeout=5, retry=None)), None) is not None:
            raise FixtureError("bucket_not_empty")
    except FixtureError:
        raise
    except Exception:
        raise FixtureError("bucket_inventory_unavailable") from None
    return bucket


def _write_cleanup_evidence(path: Path, bucket: str, prefix: str, created: list[tuple[str, str]], outcomes: list[dict[str, object]], uncertain_names: list[str] | None = None) -> None:
    unresolved = sorted(set(uncertain_names or []))
    payload = {"schema_version": 1, "mode": "fixture_cleanup", "bucket": bucket,
               "prefix": prefix, "created_generations": [{"name": n, "generation": g} for n, g in created],
               "unresolved_names": unresolved, "outcomes": outcomes,
               "complete": not unresolved and all(item["status"] == "deleted" for item in outcomes)}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _cleanup_created_generations(provider: object, bucket: str, prefix: str, path: Path, created: list[tuple[str, str]], uncertain_names: list[str] | None = None) -> list[dict[str, object]]:
    """Best-effort cleanup restricted to exact generations known to this run."""
    outcomes: list[dict[str, object]] = []
    for name, generation in created:
        try:
            if not name.startswith(prefix):
                raise FixtureError("cleanup_scope_invalid")
            provider.delete(name, generation, deadline=time.monotonic() + 30)
            if provider.read(name, deadline=time.monotonic() + 30) is not None:
                raise RuntimeError("cleanup_readback_failed")
            outcomes.append({"name": name, "generation": generation, "status": "deleted"})
        except Exception:
            outcomes.append({"name": name, "generation": generation, "status": "failed", "reason": "cleanup_failed"})
    _write_cleanup_evidence(path, bucket, prefix, created, outcomes, uncertain_names)
    return outcomes


def execute(args: argparse.Namespace, bucket_name: str, prefix: str, plan_digest: str, receipt_path: Path) -> dict[str, object]:
    if args.confirm_plan_digest != plan_digest:
        raise FixtureError("plan_digest_mismatch")
    from app.services import storage_service
    from app.services.known_copy_inventory import GCSReader, ScanLimits, scan_known_copies, write_manifest
    from app.services.known_copy_removal import GCSRemovalProvider, build_removal_plan, execute_removal, verify_removal, write_receipt, _write_plan

    created: list[tuple[str, str]] = []
    active_generations: list[tuple[str, str]] = []
    uncertain_names: list[str] = []
    cleanup_path = Path(f"{receipt_path}.cleanup.json")
    previous_client = storage_service._client
    phase = "provider_preflight"
    try:
        client = _new_storage_client(args.project)
        bucket = _preflight_bucket(client, bucket_name, args.project_number)
        storage_service._client = client
        target_name = prefix + "target.bin"
        copy_name = prefix + "copy.bin"

        phase = "initial_upload"
        for name in (target_name, copy_name):
            blob = bucket.blob(name)
            blob.metadata = {"purpose": "c437-synthetic", "run_nonce": args.nonce}
            try:
                blob.upload_from_string(FIXTURE_BODY, content_type="application/octet-stream", if_generation_match=0, retry=None, timeout=10)
            except KeyboardInterrupt:
                uncertain_names.append(name)
                raise
            except Exception:
                # Even matching readback cannot prove this request created the
                # object; do not auto-delete a possibly competing generation.
                uncertain_names.append(name)
                raise FixtureError("upload_outcome_unknown", phase) from None
            generation = str(blob.generation)
            if not generation.isdigit() or int(generation) <= 0:
                uncertain_names.append(name)
                raise FixtureError("upload_generation_unknown", phase)
            created.append((name, generation))
            active_generations.append((name, generation))
            blob.reload(timeout=10, retry=None)
            if (blob.metadata or {}).get("purpose") != "c437-synthetic" or (blob.metadata or {}).get("run_nonce") != args.nonce:
                raise FixtureError("uploaded_object_ownership_mismatch")

        target_generation = created[0][1]
        phase = "inventory_and_plan"
        inventory = scan_known_copies(
            GCSReader(bucket_name), bucket=bucket_name, target_name=target_name,
            expected_generation=target_generation, prefixes=(prefix,),
            limits=ScanLimits(max_objects=MAX_OBJECTS, max_object_bytes=MAX_BYTES,
                              max_download_bytes=2 * MAX_BYTES, deadline_seconds=30),
        )
        expected_initial = set(created)
        observed_matches = {
            (item.metadata.name, item.metadata.generation)
            for item in inventory.matches
        }
        if observed_matches != expected_initial:
            raise FixtureError("fixture_inventory_object_set_mismatch", phase)
        inventory_path = receipt_path.with_name(receipt_path.name + ".inventory.json")
        write_manifest(inventory_path, inventory)
        plan = build_removal_plan(inventory, allowed_prefixes=(prefix,))
        plan_path = receipt_path.with_name(receipt_path.name + ".plan.json")
        _write_plan(plan_path, plan)
        phase = "generation_bound_removal"
        receipt = execute_removal(plan, GCSRemovalProvider(bucket_name), receipt_path, deadline_seconds=30)
        if not all(item.status in {"removed_verified", "already_absent"} for item in receipt.outcomes):
            raise FixtureError("original_removal_incomplete")
        phase = "absence_verification"
        absence = verify_removal(receipt, GCSRemovalProvider(bucket_name), deadline_seconds=30)
        if not absence.verification_complete:
            raise FixtureError("original_absence_unverified")
        # The plan verified these exact generations absent. Keep them in the
        # provenance list, but only the replacement remains eligible for cleanup.
        active_generations.clear()

        phase = "replacement_upload"
        replacement = bucket.blob(copy_name)
        replacement.metadata = {"purpose": "c437-synthetic", "run_nonce": args.nonce}
        try:
            replacement.upload_from_string(FIXTURE_BODY, content_type="application/octet-stream", if_generation_match=0, retry=None, timeout=10)
        except KeyboardInterrupt:
            uncertain_names.append(copy_name)
            raise
        except Exception:
            uncertain_names.append(copy_name)
            raise FixtureError("replacement_upload_outcome_unknown", phase) from None
        replacement_generation = str(replacement.generation)
        if not replacement_generation.isdigit() or int(replacement_generation) <= 0:
            uncertain_names.append(copy_name)
            raise FixtureError("replacement_generation_unknown", phase)
        created.append((copy_name, replacement_generation))
        active_generations.append((copy_name, replacement_generation))
        replacement.reload(timeout=10, retry=None)
        if (replacement.metadata or {}).get("purpose") != "c437-synthetic" or (replacement.metadata or {}).get("run_nonce") != args.nonce:
            raise FixtureError("replacement_ownership_mismatch")
        phase = "replacement_verification"
        checked = verify_removal(receipt, GCSRemovalProvider(bucket_name), deadline_seconds=30)
        copy_outcome = next(item for item in checked.outcomes if item.object.name == copy_name)
        if copy_outcome.status != "reappeared" or copy_outcome.observed_generation != str(replacement.generation):
            raise FixtureError("replacement_reappearance_unverified")
        phase = "receipt_write"
        write_receipt(receipt_path, checked)
        phase = "fixture_cleanup"
        cleanup_outcomes = _cleanup_created_generations(
            GCSRemovalProvider(bucket_name), bucket_name, prefix, cleanup_path, active_generations,
        )
        if not cleanup_outcomes or not all(item["status"] == "deleted" for item in cleanup_outcomes):
            raise FixtureError("cleanup_incomplete")
        result = {
            "plan_digest": plan.plan_digest,
            "fixture_plan_digest": plan_digest,
            "receipt_digest": checked.receipt_digest,
            "verification_complete": checked.verification_complete,
            "replacement_generation": str(replacement.generation),
            "created_generations": created,
            "cleanup_complete": True,
        }
        return result
    except KeyboardInterrupt:
        try:
            if active_generations or uncertain_names:
                _cleanup_created_generations(GCSRemovalProvider(bucket_name), bucket_name, prefix, cleanup_path, active_generations, uncertain_names)
        except Exception:
            outcomes: list[dict[str, object]] = []
            outcomes.extend({"name": name, "generation": generation, "status": "failed", "reason": "cleanup_failed"} for name, generation in active_generations)
            try:
                _write_cleanup_evidence(cleanup_path, bucket_name, prefix, active_generations, outcomes, uncertain_names)
            except Exception:
                pass
        raise
    except Exception as exc:
        try:
            if active_generations or uncertain_names:
                _cleanup_created_generations(GCSRemovalProvider(bucket_name), bucket_name, prefix, cleanup_path, active_generations, uncertain_names)
        except Exception:
            outcomes: list[dict[str, object]] = []
            outcomes.extend({"name": name, "generation": generation, "status": "failed", "reason": "cleanup_failed"} for name, generation in active_generations)
            try:
                _write_cleanup_evidence(cleanup_path, bucket_name, prefix, active_generations, outcomes, uncertain_names)
            except Exception:
                pass
        if isinstance(exc, FixtureError):
            raise
        raise FixtureError("provider_operation_failed", phase) from None
    finally:
        storage_service._client = previous_client


def main(argv: list[str] | None = None) -> int:
    lock_fd: int | None = None
    lock_path: Path | None = None
    try:
        args = _args(argv)
        bucket, nonce, prefix = _validate(args)
        packet = _packet(args, bucket, nonce, prefix)
        if args.execute:
            receipt_path = _validate_execution(args, str(packet["plan_digest"]))
            # Keep the run-wide lock distinct from the removal service's
            # receipt-specific lock, which it acquires while writing receipts.
            lock_path = Path(f"{receipt_path}.fixture.lock")
            try:
                lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                os.fchmod(lock_fd, 0o600)
            except Exception:
                raise FixtureError("receipt_lock_unavailable") from None
            packet["result"] = execute(args, bucket, prefix, str(packet["plan_digest"]), receipt_path)
        elif any((args.receipt is not None, args.approval is not None, args.confirm_plan_digest is not None)):
            raise FixtureError("execution_options_require_execute")
        print(json.dumps(packet, sort_keys=True))
        return 0
    except FixtureError as exc:
        print(json.dumps({"mode": "REFUSED", "phase": exc.phase, "reason": exc.code}, sort_keys=True))
        return 2
    except Exception:
        print(json.dumps({"mode": "ERROR", "reason": "fixture_execution_failed"}, sort_keys=True))
        return 2
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        if lock_path is not None and lock_fd is not None:
            try:
                lock_path.unlink()
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
