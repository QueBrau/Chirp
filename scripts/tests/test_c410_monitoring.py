"""Network-free contracts for c410's real policy files and apply path.

These tests prove authored intent and guards, not Google's evaluator or delivery.
The separate disabled/no-channel API probe is required for provider acceptance.
"""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

_path = Path(__file__).with_name("test_monitoring_apply.py")
_spec = importlib.util.spec_from_file_location("c410_apply_fixtures", _path)
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)
m = base.m
FILES = ("cloud-run-job-failure.json", "chirp-purge-overdue-success.json")


def policy(index=0):
    return json.loads((base.POLICIES_DIR / FILES[index]).read_text()
                      .replace("{{PROJECT}}", base.PROJECT)
                      .replace("{{NOTIFICATION_CHANNEL}}", "projects/sample-project/notificationChannels/123"))


def errors(body):
    return m.required_shape_errors(body, base._inventory_available_metrics())


class C410MonitoringTests(unittest.TestCase):
    def test_authored_policies_validate_without_skips(self):
        for index in range(2):
            self.assertEqual(errors(policy(index)), [])

    def test_failure_targets_every_job_and_only_failed_run_system_events(self):
        body = policy()
        self.assertEqual(len(body["conditions"]), 1)
        log = body["conditions"][0]["conditionMatchedLog"]
        # This exact conjunction protects the evidence-backed signal. In
        # particular, no job restriction or started-task metric may replace it.
        self.assertEqual(log["filter"].split(" AND "), [
            'logName="projects/sample-project/logs/cloudaudit.googleapis.com%2Fsystem_event"',
            'resource.type="cloud_run_job"',
            'protoPayload.methodName="/Jobs.RunJob"',
            'severity>=ERROR',
        ])
        self.assertEqual(log["labelExtractors"], {"job_name": "EXTRACT(resource.labels.job_name)"})
        self.assertEqual(body["alertStrategy"], {
            "notificationRateLimit": {"period": "300s"},
            "notificationPrompts": ["OPENED"], "autoClose": "1800s",
        })

    def test_overdue_selects_success_not_failure_without_assuming_zero_fill(self):
        threshold = policy(1)["conditions"][0]["conditionThreshold"]
        self.assertEqual(threshold["filter"].split(" AND "), [
            'resource.type="cloud_run_job"', 'resource.labels.project_id="sample-project"',
            'resource.labels.job_name="chirp-purge"',
            'metric.type="run.googleapis.com/job/completed_execution_count"',
            'metric.labels.result="succeeded"',
        ])
        self.assertEqual(threshold["comparison"], "COMPARISON_LT")
        self.assertEqual(threshold["thresholdValue"], 1)
        self.assertEqual(threshold["evaluationMissingData"], "EVALUATION_MISSING_DATA_ACTIVE")
        self.assertEqual(threshold["duration"], "300s")
        self.assertEqual(threshold["aggregations"], [{"alignmentPeriod": "89100s",
                        "perSeriesAligner": "ALIGN_SUM", "crossSeriesReducer": "REDUCE_SUM"}])
        # The daily schedule itself is an independently captured operating fact.
        cadence = json.loads((base.REPO_ROOT / "infra/monitoring/evidence/c398-scheduler-chirp-purge-daily-2026-09-10.json").read_text())
        self.assertIn("0 9 * * *", json.dumps(cadence))
        self.assertGreater(89100, 24 * 3600)
        self.assertLessEqual(89100 + 300, m.CONDITION_THRESHOLD_MAX_SECONDS)

    def test_existing_native_failure_policy_is_retained(self):
        body = json.loads((base.POLICIES_DIR / "chirp-purge-job-failure.json").read_text())
        threshold = body["conditions"][0]["conditionThreshold"]
        self.assertIn('metric.labels.result!="succeeded"', threshold["filter"])
        self.assertEqual(threshold["comparison"], "COMPARISON_GT")

    def test_log_match_cannot_share_policy_with_other_conditions(self):
        body = policy()
        body["conditions"].append(policy(1)["conditions"][0])
        self.assertIn("log_match_requires_single_condition", errors(body))

    def test_condition_union_cannot_hide_threshold_inside_log_condition(self):
        body = policy()
        body["conditions"][0]["conditionThreshold"] = policy(1)["conditions"][0]["conditionThreshold"]
        self.assertIn("condition_requires_one_type", errors(body))

    def test_log_filter_and_extractor_types_are_required(self):
        for value in (None, "", " ", 4):
            body = policy()
            body["conditions"][0]["conditionMatchedLog"]["filter"] = value
            self.assertIn("log_match_requires_filter", errors(body))
        for value in (None, [], {"job": None}, {"job": 1}, {"": "EXTRACT(x)"}, {"job": " "}):
            body = policy()
            body["conditions"][0]["conditionMatchedLog"]["labelExtractors"] = value
            self.assertIn("log_match_invalid_label_extractors", errors(body))

    def test_log_notification_period_required_and_bounded(self):
        for value in (None, {}, {"period": "299s"}, {"period": "5m"}, {"period": "9" * 400 + "s"}):
            body = policy()
            body["alertStrategy"]["notificationRateLimit"] = value
            self.assertIn("log_match_notification_period_below_300s_or_invalid", errors(body))
        body = policy()
        del body["alertStrategy"]
        self.assertIn("log_match_notification_period_below_300s_or_invalid", errors(body))

    def test_log_auto_close_and_prompts_reject_incompatible_values(self):
        for value in ("1799s", "604801s", "30m", None):
            body = policy()
            body["alertStrategy"]["autoClose"] = value
            self.assertIn("log_match_auto_close_out_of_range", errors(body))
        for value in ([], ["CLOSED"], ["OPENED", "CLOSED"], "OPENED"):
            body = policy()
            body["alertStrategy"]["notificationPrompts"] = value
            self.assertIn("log_match_notification_prompts_must_be_opened", errors(body))

    def test_missing_data_requires_known_mode_and_positive_retest(self):
        for value in ("ACTIVE", {}, [], None):
            body = policy(1)
            body["conditions"][0]["conditionThreshold"]["evaluationMissingData"] = value
            self.assertIn("invalid_evaluation_missing_data", errors(body))
        for value in (None, "0s", "59s", "1m"):
            body = policy(1)
            body["conditions"][0]["conditionThreshold"]["duration"] = value
            self.assertIn("evaluation_missing_data_requires_duration_60s", errors(body))
        body = policy(1)
        body["conditions"][0]["conditionThreshold"]["duration"] = "60s"
        self.assertEqual(errors(body), [])

    def test_threshold_retest_uses_whole_minutes(self):
        for duration in ("61s", "60.5s", "9" * 400 + "s"):
            body = policy(1)
            body["conditions"][0]["conditionThreshold"]["duration"] = duration
            self.assertIn("condition_threshold_duration_not_whole_minutes", errors(body))

    def test_threshold_combined_window_boundary_and_denominator(self):
        for key in ("aggregations", "denominatorAggregations"):
            body = policy(1)
            threshold = body["conditions"][0]["conditionThreshold"]
            threshold[key] = [{"alignmentPeriod": "89700s", "perSeriesAligner": "ALIGN_SUM"}]
            self.assertEqual(errors(body), [])  # 89700 + 300 == 25h
            threshold[key][0]["alignmentPeriod"] = "89701s"
            self.assertIn("condition_threshold_window_over_api_ceiling", errors(body))
        for duration in ("59s", "25h", None):
            body = policy(1)
            body["conditions"][0]["conditionThreshold"]["aggregations"][0]["alignmentPeriod"] = duration
            self.assertIn("invalid_threshold_alignment_period", errors(body))

    def test_metric_policy_rate_limit_remains_rejected(self):
        body = policy(1)
        body["alertStrategy"] = policy()["alertStrategy"]
        self.assertIn("notification_rate_limit_on_non_log_policy", errors(body))

    def test_unknown_fields_fail_on_new_shapes(self):
        for index, kind in ((0, "conditionMatchedLog"), (1, "conditionThreshold")):
            body = policy(index)
            body["conditions"][0][kind]["evaluationMissingDatum"] = "ACTIVE"
            self.assertTrue(any("unknown_key" in item for item in errors(body)))

    def test_actual_loader_skips_invalid_log_policy_and_performs_no_writes(self):
        body = policy()
        del body["alertStrategy"]["notificationRateLimit"]
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "bad.json").write_text(json.dumps(body))
            report = m.run(base._args(policies_dir=tmp), get=base._no_op_get,
                           post=base._refusing, patch=base._refusing, put=base._refusing)
        self.assertEqual(report["policies"], [])
        self.assertEqual(len(report["skipped_files"]), 1)
        self.assertEqual(report["exit_code"], 1)

    def test_pair_apply_converges_and_condition_update_preserves_identity(self):
        fake = base.FakeMonitoringAPI()
        with tempfile.TemporaryDirectory() as tmp:
            for index, name in enumerate(FILES):
                (Path(tmp) / name).write_text(json.dumps(policy(index)))
            args = base._args(policies_dir=tmp, apply=True,
                              channel="projects/sample-project/notificationChannels/123")
            first = m.run(args, get=fake.get, post=fake.post, patch=fake.patch, put=fake.put)
            self.assertEqual(first["exit_code"], 0)
            self.assertEqual([x["action"] for x in first["policies"]], ["create", "create"])
            second = m.run(args, get=fake.get, post=base._refusing,
                           patch=base._refusing, put=base._refusing)
            self.assertEqual(second["exit_code"], 0)
            self.assertEqual([x["action"] for x in second["policies"]], ["noop", "noop"])
            actual = fake.policies[policy()["displayName"]]
            condition_name = actual["conditions"][0]["name"]
            changed = policy()
            changed["conditions"][0]["conditionMatchedLog"]["filter"] += ' AND resource.labels.location="us-central1"'
            (Path(tmp) / FILES[0]).write_text(json.dumps(changed))
            third = m.run(args, get=fake.get, post=base._refusing,
                          patch=fake.patch, put=base._refusing)
            self.assertEqual(third["exit_code"], 0)
            self.assertEqual(fake.policies[changed["displayName"]]["conditions"][0]["name"], condition_name)


if __name__ == "__main__":
    unittest.main()
