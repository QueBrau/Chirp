#!/usr/bin/env python3
"""Check Cloud SQL recovery metadata without changing resources or testing a restore."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
import re
import subprocess
import sys
from typing import Any


CLOCK_SKEW_SECONDS = 60
TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?"
    r"(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)"
)
BACKUP_STATUSES = {
    "ENQUEUED", "OVERDUE", "RUNNING", "FAILED", "SUCCESSFUL", "SKIPPED",
    "DELETION_PENDING", "DELETION_FAILED", "DELETED",
}


class MetadataError(ValueError):
    """An expected metadata field is missing or malformed."""


class SafeParser(argparse.ArgumentParser):
    """Do not echo potentially sensitive command-line values on errors."""

    def error(self, message: str) -> None:
        print(json.dumps({
            "metadata_only": True, "restore_verified": False,
            "status": "error", "exit_code": 2, "error": "invalid_arguments",
            "hint": "Run scripts/recovery-check --help for accepted arguments.",
        }))
        raise SystemExit(2)


def positive_number(value: str) -> float:
    """Reject non-finite, zero, and negative policy values before any query."""
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be positive and finite")
    return number


def positive_integer(value: str) -> int:
    """Accept only positive integer day counts."""
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def identifier(value: str) -> str:
    """Accept resource IDs rather than option text, paths, or shell fragments."""
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,97}[a-z0-9]|[a-z]", value):
        raise argparse.ArgumentTypeError("expected a resource ID")
    return value


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = SafeParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--project", required=True, type=identifier)
    parser.add_argument("--instance", required=True, type=identifier)
    parser.add_argument("--gcloud", default="gcloud", help="Trusted gcloud executable path")
    parser.add_argument("--max-backup-age-hours", type=positive_number, default=36.0)
    parser.add_argument("--max-recovery-lag-minutes", type=positive_number, default=15.0)
    parser.add_argument("--min-log-retention-days", type=positive_integer, default=7)
    parser.add_argument(
        "--timeout-seconds", type=positive_number, default=30.0,
        help="Timeout for each of three metadata queries (maximum 120 seconds)",
    )
    args = parser.parse_args(argv)
    if args.timeout_seconds > 120 or not args.gcloud or "\x00" in args.gcloud:
        parser.error("invalid executable or timeout")
    return args


def object_value(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MetadataError("expected_object")
    return value


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MetadataError("duplicate_json_key")
        result[key] = value
    return result


def reject_constant(value: str) -> None:
    raise MetadataError("non_finite_json")


def timestamp(value: Any, now: datetime) -> datetime:
    if not isinstance(value, str) or not TIMESTAMP.fullmatch(value):
        raise MetadataError("invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, OverflowError) as error:
        raise MetadataError("invalid_timestamp") from error
    if parsed.year < 1970 or (parsed - now).total_seconds() > CLOCK_SKEW_SECONDS:
        raise MetadataError("timestamp_out_of_bounds")
    return parsed


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def check(report: dict[str, Any], name: str, status: str, **facts: Any) -> None:
    report["checks"].append({"check": name, "status": status, **facts})


def boolean_check(report: dict[str, Any], name: str, value: Any) -> None:
    if value is not None and not isinstance(value, bool):
        check(report, name, "error", reason="invalid_boolean")
    else:
        check(report, name, "pass" if value is True else "gap", enabled=value is True,
              field_present=value is not None)


def read_metadata(
    args: argparse.Namespace, stage: str, command: list[str], report: dict[str, Any],
) -> Any:
    environment = os.environ.copy()
    environment["CLOUDSDK_CORE_DISABLE_FILE_LOGGING"] = "1"
    environment["CLOUDSDK_CORE_DISABLE_PROMPTS"] = "1"
    try:
        result = subprocess.run(
            [args.gcloud, *command, f"--project={args.project}", "--format=json", "--quiet"],
            capture_output=True, text=True, encoding="utf-8", env=environment,
            timeout=args.timeout_seconds, check=False,
        )
    except subprocess.TimeoutExpired:
        check(report, stage, "error", reason="query_timeout")
        return None
    except (OSError, UnicodeError):
        check(report, stage, "error", reason="query_unavailable")
        return None
    if result.returncode:
        check(report, stage, "error", reason="query_failed", return_code=result.returncode)
        return None
    try:
        return json.loads(result.stdout, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except (ValueError, RecursionError):
        check(report, stage, "error", reason="invalid_json")
        return None


def inspect_instance(value: Any, args: argparse.Namespace, report: dict[str, Any]) -> None:
    instance = object_value(value)
    # Verify the response belongs to the explicit target before accepting its metadata.
    if instance.get("name") != args.instance or instance.get("project") != args.project:
        raise MetadataError("target_mismatch_or_missing")
    state = instance.get("state")
    version = instance.get("databaseVersion")
    if not isinstance(state, str) or not re.fullmatch(r"[A-Z_]{1,40}", state):
        raise MetadataError("invalid_instance_state")
    if not isinstance(version, str) or not re.fullmatch(r"[A-Z0-9_]{1,60}", version):
        raise MetadataError("invalid_database_version")
    check(report, "instance_runnable", "pass" if state == "RUNNABLE" else "gap", state=state)
    check(report, "postgresql", "pass" if re.fullmatch(r"POSTGRES_\d+", version) else "gap",
          database_version=version)
    settings = object_value(instance.get("settings"))
    backup = object_value(settings.get("backupConfiguration"))
    boolean_check(report, "daily_backup_enabled", backup.get("enabled"))
    boolean_check(report, "pitr_explicitly_enabled", backup.get("pointInTimeRecoveryEnabled"))
    retention = backup.get("transactionLogRetentionDays")
    if retention is None:
        check(report, "configured_log_retention", "gap", days=None)
    elif type(retention) is not int or retention < 0:
        check(report, "configured_log_retention", "error", reason="invalid_retention_days")
    else:
        check(report, "configured_log_retention",
              "pass" if retention >= args.min_log_retention_days else "gap", days=retention)
    protection = settings.get("deletionProtectionEnabled")
    if protection is not None and not isinstance(protection, bool):
        check(report, "deletion_protection", "error", reason="invalid_boolean")
    else:
        check(report, "deletion_protection", "info", enabled=protection,
              review_required=protection is not True)


def inspect_backups(value: Any, args: argparse.Namespace, report: dict[str, Any], now: datetime) -> None:
    if not isinstance(value, list):
        raise MetadataError("expected_backup_list")
    successful: list[tuple[datetime, datetime, str]] = []
    for raw in value:
        backup = object_value(raw)
        if backup.get("instance") != args.instance:
            raise MetadataError("backup_instance_mismatch_or_missing")
        status = backup.get("status")
        if not isinstance(status, str) or status not in BACKUP_STATUSES:
            raise MetadataError("invalid_backup_status")
        if status != "SUCCESSFUL":
            continue
        kind = backup.get("type")
        if kind not in ("AUTOMATED", "ON_DEMAND", "FINAL"):
            raise MetadataError("invalid_backup_type")
        start = timestamp(backup.get("startTime"), now)
        end = timestamp(backup.get("endTime"), now)
        if start > end:
            raise MetadataError("backup_time_order")
        successful.append((start, end, kind))
    check(report, "successful_backup_inventory", "info", successful_count=len(successful),
          other_status_count=len(value) - len(successful))
    for label, candidates in (
        ("latest_successful_backup", successful),
        ("latest_successful_automated_backup", [b for b in successful if b[2] == "AUTOMATED"]),
    ):
        if not candidates:
            check(report, label, "gap", reason="no_successful_backup")
            continue
        start, end, kind = max(candidates, key=lambda item: item[0])
        age = max(0.0, (now - start).total_seconds()) / 3600
        check(report, label, "pass" if age <= args.max_backup_age_hours else "gap",
              type=kind, started_at=iso(start), completed_at=iso(end), age_hours=round(age, 4),
              age_basis="startTime", restore_verified=False)


def inspect_window(value: Any, args: argparse.Namespace, report: dict[str, Any], now: datetime) -> None:
    window = object_value(value)
    earliest = timestamp(window.get("earliestRecoveryTime"), now)
    latest = timestamp(window.get("latestRecoveryTime"), now)
    if earliest > latest:
        raise MetadataError("recovery_time_order")
    lag = max(0.0, (now - latest).total_seconds()) / 60
    check(report, "latest_recovery_lag", "pass" if lag <= args.max_recovery_lag_minutes else "gap",
          earliest_recovery_time=iso(earliest), latest_recovery_time=iso(latest),
          lag_minutes=round(lag, 4))
    check(report, "observed_recovery_window", "info",
          days=round((latest - earliest).total_seconds() / 86400, 6),
          configured_retention_is_not_a_verified_window=True)


def main(argv: list[str] | None = None) -> int:
    args = parse_arguments(argv)
    report: dict[str, Any] = {
        "schema_version": 1, "metadata_only": True, "restore_verified": False,
        "clock_skew_tolerance_seconds": CLOCK_SKEW_SECONDS,
        "policy": {"max_backup_age_hours": args.max_backup_age_hours,
                   "max_recovery_lag_minutes": args.max_recovery_lag_minutes,
                   "min_configured_log_retention_days": args.min_log_retention_days,
                   "query_timeout_seconds": args.timeout_seconds},
        "checks": [],
    }
    instance = read_metadata(args, "instance_query", ["sql", "instances", "describe", args.instance], report)
    backups = read_metadata(args, "backups_query", ["sql", "backups", "list", f"--instance={args.instance}"], report)
    recovery = read_metadata(args, "recovery_query", ["sql", "instances", "get-latest-recovery-time", args.instance], report)
    now = datetime.now(timezone.utc)
    report["observed_at"] = iso(now)
    for stage, value, inspect in (
        ("instance_metadata", instance, lambda item: inspect_instance(item, args, report)),
        ("backups_metadata", backups, lambda item: inspect_backups(item, args, report, now)),
        ("recovery_metadata", recovery, lambda item: inspect_window(item, args, report, now)),
    ):
        # JSON null is not a valid successful response for any of these APIs.
        if value is None and any(c["check"] == stage.replace("_metadata", "_query") for c in report["checks"]):
            continue
        try:
            inspect(value)
        except MetadataError as error:
            check(report, stage, "error", reason=str(error))
    statuses = {item["status"] for item in report["checks"]}
    code = 2 if "error" in statuses else 1 if "gap" in statuses else 0
    report.update(status=("metadata_policy_met", "gaps", "error")[code], exit_code=code)
    print(json.dumps(report, indent=2, allow_nan=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
