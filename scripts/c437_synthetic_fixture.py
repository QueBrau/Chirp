#!/usr/bin/env python3
"""Print or explicitly run a tiny, test-owned c437 GCS fixture procedure."""
from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import time
import os
from datetime import datetime, timezone
from pathlib import Path

APPROVAL = "I_UNDERSTAND_SYNTHETIC_ONLY"
AUTHORIZED_BUCKET = "chirps-prod-media"
NONCE_RE = re.compile(r"^[a-z0-9]{8,32}$")
MAX_OBJECTS = 3
MAX_BYTES = 1024 * 1024


def _args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--nonce", default=None)
    parser.add_argument("--receipt", type=Path, default=None)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approval", default=None)
    parser.add_argument("--cleanup", action="store_true")
    return parser.parse_args(argv)


def _validate(args: argparse.Namespace) -> tuple[str, str]:
    nonce = args.nonce or secrets.token_hex(8)
    if not NONCE_RE.fullmatch(nonce):
        raise ValueError("nonce must be 8-32 lowercase alphanumeric characters")
    if not args.project or not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", args.project):
        raise ValueError("project is invalid")
    if args.bucket != AUTHORIZED_BUCKET:
        raise ValueError("bucket is not the authorized synthetic fixture bucket")
    prefix = f"posts/c437-test-{datetime.now(timezone.utc):%Y%m%d}-{nonce}/"
    return nonce, prefix


def _packet(args: argparse.Namespace, nonce: str, prefix: str) -> dict[str, object]:
    names = [prefix + "target.bin", prefix + "copy.bin", prefix + "replacement.bin"]
    return {
        "mode": "EXECUTE" if args.execute else "PRINT_ONLY_NOT_EXECUTED",
        "project": args.project,
        "bucket": args.bucket,
        "prefix": prefix,
        "nonce": nonce,
        "max_objects": MAX_OBJECTS,
        "max_bytes_per_object": MAX_BYTES,
        "synthetic_objects": names,
        "receipt": str(args.receipt) if args.receipt else None,
        "actions": [
            "upload at most two identical public synthetic byte strings with create-only generation preconditions",
            "inventory only the exact posts prefix and review generation-bound manifest",
            "delete only reviewed generations with explicit conditional delete and journal receipt",
            "recreate one name to prove replacement generation is reported and not deleted",
            "cleanup only fixture generations after review; never scan or delete outside the prefix",
        ],
    }


def _write_cleanup_evidence(path: Path, bucket: str, prefix: str, created: list[tuple[str, str]], outcomes: list[dict[str, object]]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {"schema_version": 1, "mode": "fixture_cleanup", "bucket": bucket,
               "prefix": prefix, "created_generations": [{"name": n, "generation": g} for n, g in created],
               "outcomes": outcomes, "complete": all(item["status"] == "deleted" for item in outcomes)}
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


def _cleanup_created_generations(provider: object, bucket: str, prefix: str, path: Path, created: list[tuple[str, str]]) -> list[dict[str, object]]:
    """Best-effort cleanup restricted to exact generations known to this run."""
    outcomes: list[dict[str, object]] = []
    for name, generation in created:
        try:
            provider.delete(name, generation, deadline=time.monotonic() + 30)
            if provider.read(name, deadline=time.monotonic() + 30) is not None:
                raise RuntimeError("cleanup_readback_failed")
            outcomes.append({"name": name, "generation": generation, "status": "deleted"})
        except Exception:
            outcomes.append({"name": name, "generation": generation, "status": "failed", "reason": "cleanup_failed"})
    _write_cleanup_evidence(path, bucket, prefix, created, outcomes)
    return outcomes


def execute(args: argparse.Namespace, prefix: str) -> dict[str, object]:
    if args.approval != APPROVAL or args.receipt is None:
        raise ValueError("execute requires --approval and --receipt")
    from google.cloud import storage
    from app.services import storage_service
    from app.services.known_copy_inventory import GCSReader, ScanLimits, scan_known_copies, write_manifest
    from app.services.known_copy_removal import GCSRemovalProvider, build_removal_plan, execute_removal, verify_removal, write_receipt, _write_plan

    created: list[tuple[str, str]] = []
    cleanup_path = Path(f"{args.receipt}.cleanup.json")

    try:
        client = storage.Client(project=args.project)
        storage_service._client = client
        bucket = client.bucket(args.bucket)
        body = b"CHIRP-C437-SYNTHETIC-PUBLIC-FIXTURE\n"
        target_name = prefix + "target.bin"
        copy_name = prefix + "copy.bin"
        for name in (target_name, copy_name):
            blob = bucket.blob(name)
            blob.upload_from_string(body, content_type="application/octet-stream", if_generation_match=0, retry=None, timeout=10)
            created.append((name, str(blob.generation)))
            blob.reload(timeout=10, retry=None)
        target_generation = created[0][1]
        reader = GCSReader(args.bucket)
        inventory = scan_known_copies(reader, bucket=args.bucket, target_name=target_name, expected_generation=target_generation, prefixes=(prefix,), limits=ScanLimits(max_objects=MAX_OBJECTS, max_object_bytes=MAX_BYTES, max_download_bytes=2 * MAX_BYTES, deadline_seconds=30))
        inventory_path = args.receipt.with_name(args.receipt.name + ".inventory.json")
        write_manifest(inventory_path, inventory)
        plan = build_removal_plan(inventory, allowed_prefixes=(prefix,))
        plan_path = args.receipt.with_name(args.receipt.name + ".plan.json")
        _write_plan(plan_path, plan)
        receipt = execute_removal(plan, GCSRemovalProvider(args.bucket), args.receipt, deadline_seconds=30)
        if not all(item.status in {"removed_verified", "already_absent"} for item in receipt.outcomes):
            raise RuntimeError("original_removal_incomplete")
        absence = verify_removal(receipt, GCSRemovalProvider(args.bucket), deadline_seconds=30)
        if not absence.verification_complete:
            raise RuntimeError("original_absence_unverified")
        replacement = bucket.blob(copy_name)
        replacement.upload_from_string(body, content_type="application/octet-stream", if_generation_match=0, retry=None, timeout=10)
        created.append((copy_name, str(replacement.generation)))
        replacement.reload(timeout=10, retry=None)
        checked = verify_removal(receipt, GCSRemovalProvider(args.bucket), deadline_seconds=30)
        copy_outcome = next(item for item in checked.outcomes if item.object.name == copy_name)
        if copy_outcome.status != "reappeared" or copy_outcome.observed_generation != str(replacement.generation):
            raise RuntimeError("replacement_reappearance_unverified")
        write_receipt(args.receipt, checked)
        result = {"plan_digest": plan.plan_digest, "receipt_digest": checked.receipt_digest, "verification_complete": checked.verification_complete, "replacement_generation": str(replacement.generation), "created_generations": created, "cleanup_required": not args.cleanup}
        if args.cleanup:
            cleanup_provider = GCSRemovalProvider(args.bucket)
            _cleanup_created_generations(cleanup_provider, args.bucket, prefix, cleanup_path, [(copy_name, str(replacement.generation))])
            result["cleanup_replacement_generation"] = str(replacement.generation)
        return result
    except Exception:
        try:
            from app.services.known_copy_removal import GCSRemovalProvider
            cleanup_provider = GCSRemovalProvider(args.bucket)
            _cleanup_created_generations(cleanup_provider, args.bucket, prefix, cleanup_path, created)
        except Exception:
            outcomes: list[dict[str, object]] = []
            outcomes.extend({"name": name, "generation": generation, "status": "failed", "reason": "cleanup_failed"} for name, generation in created)
            _write_cleanup_evidence(cleanup_path, args.bucket, prefix, created, outcomes)
        raise


def main(argv: list[str] | None = None) -> int:
    try:
        args = _args(argv)
        nonce, prefix = _validate(args)
        packet = _packet(args, nonce, prefix)
        if args.execute:
            packet["result"] = execute(args, prefix)
        print(json.dumps(packet, sort_keys=True))
        return 0
    except Exception:
        print(json.dumps({"mode": "ERROR", "error": "invalid_or_unavailable_synthetic_fixture"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
