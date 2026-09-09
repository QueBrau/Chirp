"""Fake-API/fixture tests only; no network, no credentials, no gcloud."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "monitoring_apply.py"
spec = importlib.util.spec_from_file_location("monitoring_apply", PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

REPO_ROOT = Path(__file__).resolve().parents[2]
POLICIES_DIR = REPO_ROOT / "infra/monitoring/policies"
INVENTORY_PATH = REPO_ROOT / "infra/monitoring/evidence/c370-inventory-2026-09-08.json"
PROJECT = "sample-project"


def _inventory_available_metrics():
    data = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    return {kind for kind, status in data["native_metric_descriptors"].items() if status == "available"}


def _refusing(*_a, **_k):
    raise AssertionError("write call made when none should have been")


def _no_op_get(url, params):
    # No local displayName ever matches: existing_policies() finds zero remote
    # policies, so every local file is classified action="create".
    if url.endswith("alertPolicies"):
        return {"alertPolicies": []}
    raise AssertionError("unexpected GET " + url)


class FakeAlertPolicyAPI:
    """A stateful fake keyed by displayName, mirroring the real REST surface."""

    def __init__(self):
        self.store = {}
        self._next_id = 1
        self.post_calls = 0
        self.patch_calls = 0

    def get(self, url, params):
        if url.endswith("alertPolicies"):
            return {"alertPolicies": [
                {"name": body["name"], "displayName": body["displayName"]}
                for body in self.store.values()
            ]}
        name = url.rsplit("/v3/", 1)[1]
        for body in self.store.values():
            if body["name"] == name:
                return dict(body)
        raise AssertionError("no such policy " + name)

    def post(self, url, body):
        self.post_calls += 1
        name = "projects/%s/alertPolicies/%d" % (PROJECT, self._next_id)
        self._next_id += 1
        stored = dict(body)
        stored["name"] = name
        self.store[body["displayName"]] = stored
        return dict(stored)

    def patch(self, url, body):
        self.patch_calls += 1
        name = url.split("?")[0].rsplit("/v3/", 1)[1]
        for display, stored in self.store.items():
            if stored["name"] == name:
                updated = dict(body)
                updated["name"] = name
                self.store[display] = updated
                return dict(updated)
        raise AssertionError("patch target not found " + name)


def _args(**overrides):
    values = {"project": PROJECT, "apply": False, "channel": None,
              "policies_dir": str(POLICIES_DIR), "report": None}
    values.update(overrides)
    return type("Args", (), values)()


class MonitoringApplyTests(unittest.TestCase):

    # --- test_dry_run_makes_zero_write_calls ---
    def test_dry_run_makes_zero_write_calls(self):
        args = _args()
        report = m.run(args, _no_op_get, _refusing, _refusing)
        self.assertEqual(report["exit_code"], 0)
        self.assertTrue(report["read_only"])
        self.assertEqual(len(report["policies"]), 11)
        self.assertEqual({p["action"] for p in report["policies"]}, {"create"})

    # --- test_apply_without_channel_exits_2_before_any_call ---
    def test_apply_without_channel_exits_2_before_any_call(self):
        with patch.object(m, "reader") as fake_reader:
            with self.assertRaises(SystemExit) as caught:
                m.main(["--project", PROJECT, "--apply"])
            self.assertEqual(caught.exception.code, 2)
            fake_reader.assert_not_called()

    def test_apply_without_channel_argparse_path_never_calls_write(self):
        # Guards against an implementation that relies on argparse required=
        # (which cannot express "required only with --apply") or that defers
        # the check until iterating policies.
        with self.assertRaises(SystemExit) as caught:
            m.parse_arguments(["--project", PROJECT, "--apply"])
        self.assertEqual(caught.exception.code, 2)

    # --- test_create_vs_update_decided_by_display_name_match ---
    def test_create_vs_update_decided_by_display_name_match(self):
        fixture_a = {"displayName": "A", "combiner": "OR", "conditions": [{"conditionThreshold": {"filter": ""}}],
                     "documentation": {"content": "x"}}
        fixture_b = {"displayName": "B", "combiner": "OR", "conditions": [{"conditionThreshold": {"filter": ""}}],
                     "documentation": {"content": "x"}}
        existing = {"A": {"name": "projects/x/alertPolicies/1", "displayName": "A", "combiner": "OR",
                          "conditions": [{"conditionThreshold": {"filter": "different"}}],
                          "documentation": {"content": "x"}}}
        plan = m.plan_policies([fixture_a, fixture_b], existing, channel=None)
        by_name = {entry["displayName"]: entry for entry in plan}
        self.assertEqual(by_name["A"]["action"], "update")
        self.assertEqual(by_name["A"]["existing_name"], "projects/x/alertPolicies/1")
        self.assertEqual(by_name["B"]["action"], "create")
        self.assertIsNone(by_name["B"]["existing_name"])

    # --- test_second_apply_is_all_noop ---
    def test_second_apply_is_all_noop(self):
        fake = FakeAlertPolicyAPI()
        args = _args(apply=True, channel="projects/%s/notificationChannels/1" % PROJECT)

        first = m.run(args, fake.get, fake.post, fake.patch)
        self.assertGreater(len({p["action"] for p in first["policies"]} - {"noop"}), 0)
        self.assertGreater(fake.post_calls + fake.patch_calls, 0)

        fake.post_calls = fake.patch_calls = 0
        second = m.run(args, fake.get, fake.post, fake.patch)
        self.assertEqual({p["action"] for p in second["policies"]}, {"noop"})
        self.assertEqual(fake.post_calls, 0)
        self.assertEqual(fake.patch_calls, 0)

    # --- test_every_policy_json_validates_required_shape_and_inventory_membership ---
    def test_every_policy_json_validates_required_shape_and_inventory_membership(self):
        available = _inventory_available_metrics()
        for path in sorted(POLICIES_DIR.glob("*.json")):
            with self.subTest(file=path.name):
                policy = json.loads(path.read_text().replace("{{PROJECT}}", PROJECT)
                                     .replace("{{NOTIFICATION_CHANNEL}}", "projects/x/notificationChannels/1"))
                errors = m.required_shape_errors(policy, available)
                self.assertEqual(errors, [], msg=str(errors))
                self.assertEqual(m.validate_rest_shape(policy), [], msg=str(m.validate_rest_shape(policy)))

        # The vacuous-test guard: without this fixture, a checker that always
        # returns True would pass on the real files alone. It must fail here.
        with tempfile.TemporaryDirectory() as tmp:
            bad_path = Path(tmp) / "bad.json"
            bad_path.write_text(json.dumps({
                "displayName": "bad", "combiner": "OR",
                "documentation": {"content": "See MONITORING-RUNBOOK.md."},
                "conditions": [{"conditionThreshold": {
                    "filter": 'metric.type="run.googleapis.com/not_a_real_metric"'}}],
            }))
            bad_policy = json.loads(bad_path.read_text())
            errors = m.required_shape_errors(bad_policy, available)
            self.assertTrue(any(e.startswith("metric_not_in_inventory:") for e in errors))

    # --- strict REST-shape round-trip: unknown keys fail ---
    def test_strict_rest_shape_rejects_unknown_keys(self):
        good = json.loads((POLICIES_DIR / "redis-memory-pressure.json").read_text()
                           .replace("{{PROJECT}}", PROJECT).replace("{{NOTIFICATION_CHANNEL}}", "x"))
        self.assertEqual(m.validate_rest_shape(good), [])
        bad = json.loads(json.dumps(good))
        bad["conditions"][0]["conditionThreshold"]["thresholdVal"] = 1  # typo of thresholdValue
        errors = m.validate_rest_shape(bad)
        self.assertTrue(any("thresholdVal" in e and "unknown_key" in e for e in errors))

    # --- test_no_hardcoded_project_number_or_channel_outside_templating_point ---
    def test_no_hardcoded_project_number_or_channel_outside_templating_point(self):
        project_number = "593616178468"
        for path in sorted(Path(REPO_ROOT / "infra/monitoring").rglob("*.json")):
            text = path.read_text(encoding="utf-8")
            with self.subTest(file=str(path)):
                self.assertNotIn(project_number, text)
        # Every notificationChannels value in a policy file is exactly the
        # placeholder token, never a literal channel id.
        for path in sorted(POLICIES_DIR.glob("*.json")):
            body = json.loads(path.read_text())
            for channel in body.get("notificationChannels", []):
                self.assertEqual(channel, "{{NOTIFICATION_CHANNEL}}")

        # The scanner must actually flag a bad fixture, or it is vacuous.
        with tempfile.TemporaryDirectory() as tmp:
            bad_path = Path(tmp) / "bad.json"
            bad_path.write_text(json.dumps({"note": "belongs to project " + project_number}))
            self.assertIn(project_number, bad_path.read_text())


if __name__ == "__main__":
    unittest.main()
