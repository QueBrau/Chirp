"""Exercise recovery-check through a fake gcloud without network or credentials."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from typing import Any


# c392: the python source moved to recovery_check.py behind a bash wrapper
# (scripts/recovery-check) shared with every other scripts/ entry point; this
# suite runs the module directly via sys.executable, as it always has, so only
# the path it points at changes.
SCRIPT = Path(__file__).resolve().parents[1] / "recovery_check.py"
SECRET = "do-not-print-this-cloud-secret"
FAKE_GCLOUD = '''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
import time

directory = Path(os.environ["RECOVERY_FAKE_DIR"])
with (directory / "calls.jsonl").open("a") as handle:
    handle.write(json.dumps({"args": sys.argv[1:],
                            "no_logs": os.environ.get("CLOUDSDK_CORE_DISABLE_FILE_LOGGING"),
                            "no_prompts": os.environ.get("CLOUDSDK_CORE_DISABLE_PROMPTS")}) + "\\n")
if sys.argv[1:4] == ["sql", "instances", "describe"]:
    stage = "instance"
elif sys.argv[1:4] == ["sql", "backups", "list"]:
    stage = "backups"
elif sys.argv[1:4] == ["sql", "instances", "get-latest-recovery-time"]:
    stage = "recovery"
else:
    sys.exit(99)
payload = json.loads((directory / "fixtures.json").read_text())[stage]
time.sleep(payload.get("sleep", 0))
print(payload.get("stderr", ""), file=sys.stderr)
print(payload.get("raw", json.dumps(payload.get("data"))))
sys.exit(payload.get("code", 0))
'''


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


class RecoveryCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="recovery-check-test-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.fake = self.path / "gcloud"
        self.fake.write_text(FAKE_GCLOUD)
        self.fake.chmod(0o755)
        self.now = datetime.now(timezone.utc)
        self.fixture: dict[str, Any] = {
            "instance": {"data": {
                "name": "sample-db", "project": "sample-project", "state": "RUNNABLE",
                "databaseVersion": "POSTGRES_16", "settings": {
                    "deletionProtectionEnabled": True,
                    "backupConfiguration": {"enabled": True, "pointInTimeRecoveryEnabled": True,
                                            "transactionLogRetentionDays": 7},
                },
            }},
            "backups": {"data": [self.backup(hours_old=5)]},
            "recovery": {"data": {
                "earliestRecoveryTime": iso(self.now - timedelta(days=7)),
                "latestRecoveryTime": iso(self.now - timedelta(minutes=2)),
            }},
        }

    def backup(self, hours_old: float, kind: str = "AUTOMATED", status: str = "SUCCESSFUL") -> dict[str, Any]:
        return {"instance": "sample-db", "type": kind, "status": status,
                "startTime": iso(self.now - timedelta(hours=hours_old)),
                "endTime": iso(self.now - timedelta(hours=hours_old) + timedelta(minutes=1))}

    def run_check(self, *extra: str, base: bool = True) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        (self.path / "fixtures.json").write_text(json.dumps(self.fixture))
        environment = os.environ.copy()
        environment["RECOVERY_FAKE_DIR"] = str(self.path)
        command = [sys.executable, str(SCRIPT), "--gcloud", str(self.fake)]
        if base:
            command += ["--project", "sample-project", "--instance", "sample-db"]
        result = subprocess.run(command + list(extra), env=environment, text=True, capture_output=True,
                                timeout=10, check=False)
        self.assertNotIn(SECRET, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["metadata_only"])
        self.assertFalse(report["restore_verified"])
        self.assertEqual(result.returncode, report["exit_code"])
        return result, report

    def calls(self) -> list[dict[str, Any]]:
        path = self.path / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def check(self, report: dict[str, Any], name: str) -> dict[str, Any]:
        return next(item for item in report["checks"] if item["check"] == name)

    def test_good_metadata_runs_only_three_explicit_target_reads(self) -> None:
        result, report = self.run_check()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(report["status"], "metadata_policy_met")
        self.assertEqual(result.stderr, "")
        expected = [
            ["sql", "instances", "describe", "sample-db"],
            ["sql", "backups", "list", "--instance=sample-db"],
            ["sql", "instances", "get-latest-recovery-time", "sample-db"],
        ]
        for call, prefix in zip(self.calls(), expected, strict=True):
            self.assertEqual(call["args"], prefix + ["--project=sample-project", "--format=json", "--quiet"])
            self.assertEqual(call["no_logs"], "1")
            self.assertEqual(call["no_prompts"], "1")
        self.assertEqual(self.check(report, "latest_successful_automated_backup")["age_basis"], "startTime")
        self.assertGreater(self.check(report, "observed_recovery_window")["days"], 6.9)

    def test_omitted_or_false_pitr_is_not_enabled(self) -> None:
        config = self.fixture["instance"]["data"]["settings"]["backupConfiguration"]
        for value in (None, False):
            with self.subTest(value=value):
                if value is None:
                    config.pop("pointInTimeRecoveryEnabled")
                else:
                    config["pointInTimeRecoveryEnabled"] = value
                result, report = self.run_check()
                self.assertEqual(result.returncode, 1)
                self.assertFalse(self.check(report, "pitr_explicitly_enabled")["enabled"])
                self.assertEqual(self.check(report, "latest_successful_automated_backup")["status"], "pass")

    def test_false_daily_backup_and_low_retention_are_independent_gaps(self) -> None:
        config = self.fixture["instance"]["data"]["settings"]["backupConfiguration"]
        config.update(enabled=False, transactionLogRetentionDays=2)
        result, report = self.run_check()
        self.assertEqual(result.returncode, 1)
        for name in ("daily_backup_enabled", "configured_log_retention"):
            self.assertEqual(self.check(report, name)["status"], "gap")

    def test_fresh_manual_or_failed_daily_backup_cannot_mask_stale_daily(self) -> None:
        self.fixture["backups"]["data"] = [self.backup(1, "ON_DEMAND"), self.backup(50),
                                             self.backup(1, status="FAILED")]
        result, report = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.check(report, "latest_successful_backup")["status"], "pass")
        self.assertEqual(self.check(report, "latest_successful_automated_backup")["status"], "gap")

    def test_no_successful_backup_is_a_gap(self) -> None:
        self.fixture["backups"]["data"] = [self.backup(2, status="RUNNING")]
        result, report = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.check(report, "successful_backup_inventory")["successful_count"], 0)

    def test_stale_latest_recovery_time_is_a_gap(self) -> None:
        self.fixture["recovery"]["data"]["latestRecoveryTime"] = iso(self.now - timedelta(hours=1))
        result, report = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.check(report, "latest_recovery_lag")["status"], "gap")

    def test_missing_naive_future_and_out_of_order_recovery_times_fail_closed(self) -> None:
        original = self.fixture["recovery"]["data"].copy()
        changes = [None, "2026-09-07T12:00:00", iso(self.now + timedelta(minutes=5)),
                   iso(self.now - timedelta(days=8)), "not-a-time", "1900-01-01T00:00:00Z",
                   "9999-12-31T23:59:59-23:00", "2026-09-07T12:00:00+00:99"]
        for value in changes:
            with self.subTest(value=value):
                self.fixture["recovery"]["data"] = {**original, "latestRecoveryTime": value}
                result, report = self.run_check()
                self.assertEqual(result.returncode, 2)
                self.assertEqual(self.check(report, "recovery_metadata")["status"], "error")

    def test_future_missing_or_reversed_successful_backup_times_fail_closed(self) -> None:
        for patch in ({"endTime": None}, {"startTime": iso(self.now + timedelta(days=1))},
                      {"endTime": iso(self.now - timedelta(days=1))}):
            with self.subTest(patch=patch):
                self.fixture["backups"]["data"] = [{**self.backup(2), **patch}]
                result, report = self.run_check()
                self.assertEqual(result.returncode, 2)
                self.assertEqual(self.check(report, "backups_metadata")["status"], "error")

    def test_clock_skew_is_explicit_small_and_not_negative_age(self) -> None:
        self.fixture["recovery"]["data"]["latestRecoveryTime"] = iso(self.now + timedelta(seconds=20))
        result, report = self.run_check()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(report["clock_skew_tolerance_seconds"], 60)
        self.assertEqual(self.check(report, "latest_recovery_lag")["lag_minutes"], 0)

    def test_fractional_nanoseconds_and_timezone_offsets_are_accepted(self) -> None:
        window = self.fixture["recovery"]["data"]
        window["earliestRecoveryTime"] = (self.now - timedelta(days=7)).astimezone(
            timezone(timedelta(hours=2))).isoformat()
        window["latestRecoveryTime"] = iso(self.now - timedelta(minutes=2)).replace("Z", "123Z")
        result, _ = self.run_check()
        self.assertEqual(result.returncode, 0)

    def test_recovery_query_failure_keeps_independent_backup_evidence_and_hides_raw_errors(self) -> None:
        self.fixture["recovery"] = {"code": 1, "stderr": "403 permission denied " + SECRET,
                                      "raw": SECRET}
        result, report = self.run_check()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.check(report, "recovery_query")["return_code"], 1)
        self.assertEqual(self.check(report, "latest_successful_automated_backup")["status"], "pass")

    def test_query_timeout_is_bounded_and_hides_partial_outputs(self) -> None:
        self.fixture["recovery"].update(sleep=1, stderr=SECRET)
        result, report = self.run_check("--timeout-seconds", "0.2")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.check(report, "recovery_query")["reason"], "query_timeout")

    def test_unavailable_executable_never_passes(self) -> None:
        result, report = self.run_check("--gcloud", str(self.path / "missing"))
        self.assertEqual(result.returncode, 2)
        self.assertTrue(all(item["reason"] == "query_unavailable" for item in report["checks"]))

    def test_malformed_json_duplicate_keys_and_nonfinite_json_never_pass(self) -> None:
        for raw in (SECRET, '{"latestRecoveryTime": 1, "latestRecoveryTime": 2}', '{"lag": NaN}'):
            with self.subTest(raw=raw):
                self.fixture["recovery"] = {"raw": raw}
                result, report = self.run_check()
                self.assertEqual(result.returncode, 2)
                self.assertEqual(self.check(report, "recovery_query")["reason"], "invalid_json")

    def test_missing_metadata_or_wrong_target_never_passes(self) -> None:
        original = self.fixture["instance"]["data"].copy()
        for data in (None, [], {}, {**original, "project": "different-project"},
                     {**original, "settings": {}}, {**original, "state": []}):
            with self.subTest(data=data):
                self.fixture["instance"]["data"] = data
                result, report = self.run_check()
                self.assertEqual(result.returncode, 2)
                self.assertEqual(self.check(report, "instance_metadata")["status"], "error")

    def test_truthy_strings_and_boolean_retention_are_not_valid_configuration(self) -> None:
        self.fixture["instance"]["data"]["settings"]["backupConfiguration"].update(
            pointInTimeRecoveryEnabled="true", transactionLogRetentionDays=True)
        result, report = self.run_check()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.check(report, "pitr_explicitly_enabled")["status"], "error")
        self.assertEqual(self.check(report, "configured_log_retention")["status"], "error")

    def test_custom_policy_controls_freshness_and_configured_retention(self) -> None:
        result, report = self.run_check("--max-backup-age-hours", "4",
                                        "--max-recovery-lag-minutes", "1",
                                        "--min-log-retention-days", "14")
        self.assertEqual(result.returncode, 1)
        for name in ("latest_successful_automated_backup", "latest_recovery_lag", "configured_log_retention"):
            self.assertEqual(self.check(report, name)["status"], "gap")

    def test_foreign_or_malformed_backup_inventory_never_passes(self) -> None:
        for data in ({"items": []}, [None], [{**self.backup(2), "instance": "different-db"}],
                     [{**self.backup(2), "status": []}], [{**self.backup(2), "type": "unknown"}]):
            with self.subTest(data=data):
                self.fixture["backups"]["data"] = data
                result, report = self.run_check()
                self.assertEqual(result.returncode, 2)
                self.assertEqual(self.check(report, "backups_metadata")["status"], "error")

    def test_not_runnable_or_not_postgres_are_gaps_and_protection_is_informational(self) -> None:
        self.fixture["instance"]["data"].update(state="SUSPENDED", databaseVersion="MYSQL_8_0")
        self.fixture["instance"]["data"]["settings"]["deletionProtectionEnabled"] = False
        result, report = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.check(report, "instance_runnable")["status"], "gap")
        self.assertEqual(self.check(report, "postgresql")["status"], "gap")
        self.assertTrue(self.check(report, "deletion_protection")["review_required"])

    def test_invalid_arguments_never_query(self) -> None:
        cases = [("--max-backup-age-hours", "nan"), ("--max-backup-age-hours", "inf"),
                 ("--max-recovery-lag-minutes", "-1"), ("--max-recovery-lag-minutes", "0"),
                 ("--min-log-retention-days", "1.5"), ("--min-log-retention-days", "0"),
                 ("--timeout-seconds", "121"), ("--timeout-seconds", "nan"),
                 ("--project", "--delete"), ("--instance", "name;echo " + SECRET)]
        for extra in cases:
            with self.subTest(extra=extra):
                result, _ = self.run_check(*extra)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(self.calls(), [])
        result, _ = self.run_check(base=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
