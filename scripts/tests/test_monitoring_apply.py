"""Fake-API/fixture tests only; no network, no credentials, no gcloud."""
import importlib.util
from contextlib import chdir, redirect_stdout
from copy import deepcopy
import io
import json
import re
from pathlib import Path
import shutil
import tempfile
import unittest
import urllib.parse
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "monitoring_apply.py"
spec = importlib.util.spec_from_file_location("monitoring_apply", PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

REPO_ROOT = Path(__file__).resolve().parents[2]
POLICIES_DIR = REPO_ROOT / "infra/monitoring/policies"
METRICS_DIR = REPO_ROOT / "infra/monitoring/metrics"
UPTIME_DIR = REPO_ROOT / "infra/monitoring/uptime"
INVENTORY_PATH = REPO_ROOT / "infra/monitoring/evidence/c370-inventory-2026-09-08.json"
PROJECT = "sample-project"
API_HOST = "chirp-api.example.com"
WS_HOST = "chirp-ws.example.com"


def _inventory_available_metrics():
    data = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    return {kind for kind, status in data["native_metric_descriptors"].items() if status == "available"}


def _refusing(*_a, **_k):
    raise AssertionError("write call made when none should have been")


def _scan_for_hardcoded(text: str, project_number: str) -> list[str]:
    """Real scanner, called on both the real files and the bad fixture below
    (the way m.required_shape_errors is called on both), so the vacuous-test
    guard exercises the same function the real-file assertion relies on."""
    problems = []
    if project_number in text:
        problems.append("hardcoded_project_number")
    return problems


def _no_op_get(url, params):
    # Nothing exists remotely yet: existing_metrics() gets a 404 for every
    # wanted name, existing_uptime()/existing_policies() find zero remote
    # entries, so every local file is classified action="create".
    if url.startswith("https://logging.googleapis.com/") and "/metrics/" in url:
        raise m.ApplyError("http_404")
    if url.endswith("uptimeCheckConfigs"):
        return {"uptimeCheckConfigs": []}
    if url.endswith("alertPolicies"):
        return {"alertPolicies": []}
    raise AssertionError("unexpected GET " + url)


class FakeMonitoringAPI:
    """A stateful fake covering LogMetric, UptimeCheckConfig and AlertPolicy,
    keyed the way each kind's real REST surface is keyed: LogMetric by its own
    `name` field, the other two by displayName via a list-then-GET pattern."""

    def __init__(self):
        self.metrics = {}
        self.uptime = {}
        self.policies = {}
        self._next_uptime_id = 1
        self._next_policy_id = 1
        self.post_calls = 0
        self.patch_calls = 0
        self.put_calls = 0

    def get(self, url, params):
        if url.startswith("https://logging.googleapis.com/") and "/metrics/" in url:
            name = urllib.parse.unquote(url.rsplit("/metrics/", 1)[1])
            if name in self.metrics:
                return deepcopy(self.metrics[name])
            raise m.ApplyError("http_404")
        if url.endswith("uptimeCheckConfigs"):
            return {"uptimeCheckConfigs": [
                {"name": body["name"], "displayName": body["displayName"]}
                for body in self.uptime.values()
            ]}
        if url.endswith("alertPolicies"):
            return {"alertPolicies": [
                {"name": body["name"], "displayName": body["displayName"]}
                for body in self.policies.values()
            ]}
        resource_name = url.rsplit("/v3/", 1)[1]
        for body in list(self.uptime.values()) + list(self.policies.values()):
            if body["name"] == resource_name:
                return deepcopy(body)
        raise AssertionError("no such resource " + resource_name)

    def post(self, url, body):
        self.post_calls += 1
        if url.startswith("https://logging.googleapis.com/"):
            stored = deepcopy(body)
            self.metrics[body["name"]] = stored
            return deepcopy(stored)
        if url.endswith("uptimeCheckConfigs"):
            name = "projects/%s/uptimeCheckConfigs/%d" % (PROJECT, self._next_uptime_id)
            self._next_uptime_id += 1
            stored = deepcopy(body)
            stored["name"] = name
            self.uptime[body["displayName"]] = stored
            return deepcopy(stored)
        name = "projects/%s/alertPolicies/%d" % (PROJECT, self._next_policy_id)
        self._next_policy_id += 1
        stored = deepcopy(body)
        stored["name"] = name
        stored.setdefault("enabled", True)
        for index, condition in enumerate(stored.get("conditions", []), 1):
            condition["name"] = name + "/conditions/" + str(index)
        self.policies[body["displayName"]] = stored
        return deepcopy(stored)

    def put(self, url, body):
        self.put_calls += 1
        name = urllib.parse.unquote(url.rsplit("/metrics/", 1)[1])
        if name not in self.metrics:
            raise AssertionError("put target not found " + name)
        stored = deepcopy(body)
        self.metrics[name] = stored
        return deepcopy(stored)

    def patch(self, url, body):
        self.patch_calls += 1
        resource_name = url.split("?")[0].rsplit("/v3/", 1)[1]
        for store in (self.uptime, self.policies):
            for display, stored in store.items():
                if stored["name"] == resource_name:
                    updated = deepcopy(body)
                    updated["name"] = resource_name
                    store[display] = updated
                    return deepcopy(updated)
        raise AssertionError("patch target not found " + resource_name)


def _args(**overrides):
    values = {"project": PROJECT, "apply": False, "channel": None,
              "policies_dir": str(POLICIES_DIR), "metrics_dir": str(METRICS_DIR),
              "uptime_dir": str(UPTIME_DIR), "api_host": API_HOST, "ws_host": WS_HOST,
              "report": None}
    values.update(overrides)
    return type("Args", (), values)()


class MonitoringApplyTests(unittest.TestCase):

    # --- test_dry_run_makes_zero_write_calls ---
    def test_dry_run_makes_zero_write_calls(self):
        args = _args()
        report = m.run(args, _no_op_get, _refusing, _refusing, _refusing)
        self.assertEqual(report["exit_code"], 0)
        self.assertTrue(report["read_only"])
        self.assertEqual(report["skipped_files"], [])
        self.assertEqual(len(report["metrics"]), 7)
        self.assertEqual(len(report["uptime"]), 2)
        self.assertEqual(len(report["policies"]), 20)
        self.assertEqual({p["action"] for p in report["metrics"]}, {"create"})
        self.assertEqual({p["action"] for p in report["uptime"]}, {"create"})
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

    # --- test_uptime_requires_api_host_and_ws_host_before_any_call ---
    def test_uptime_requires_api_host_and_ws_host_before_any_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "one.json").write_text(json.dumps({
                "displayName": "x", "period": "60s", "timeout": "10s",
                "monitoredResource": {"type": "uptime_url",
                                       "labels": {"project_id": PROJECT, "host": "already-real.example.com"}},
                "httpCheck": {"path": "/_health"},
            }))
            with patch.object(m, "reader") as fake_reader:
                with self.assertRaises(SystemExit) as caught:
                    m.main(["--project", PROJECT, "--uptime-dir", tmp])
                self.assertEqual(caught.exception.code, 2)
                fake_reader.assert_not_called()
            # Only one of the two flags supplied is still a failure.
            with self.assertRaises(SystemExit) as caught2:
                m.parse_arguments(["--project", PROJECT, "--uptime-dir", tmp, "--api-host", "a.example.com"])
            self.assertEqual(caught2.exception.code, 2)
            # Both supplied: no longer blocked by this guard.
            args_ok = m.parse_arguments(["--project", PROJECT, "--uptime-dir", tmp,
                                          "--api-host", "a.example.com", "--ws-host", "b.example.com"])
            self.assertEqual(args_ok.api_host, "a.example.com")

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
        fake = FakeMonitoringAPI()
        args = _args(apply=True, channel="projects/%s/notificationChannels/1" % PROJECT)

        first = m.run(args, fake.get, fake.post, fake.patch, fake.put)
        all_actions = {e["action"] for e in first["metrics"] + first["uptime"] + first["policies"]}
        self.assertGreater(len(all_actions - {"noop"}), 0)
        self.assertGreater(fake.post_calls + fake.patch_calls + fake.put_calls, 0)

        fake.post_calls = fake.patch_calls = fake.put_calls = 0
        second = m.run(args, fake.get, fake.post, fake.patch, fake.put)
        second_actions = {e["action"] for e in second["metrics"] + second["uptime"] + second["policies"]}
        self.assertEqual(second_actions, {"noop"})
        self.assertEqual(fake.post_calls, 0)
        self.assertEqual(fake.patch_calls, 0)
        self.assertEqual(fake.put_calls, 0)

    # --- test_apply_order_is_metrics_then_uptime_then_policies ---
    def test_apply_order_is_metrics_then_uptime_then_policies(self):
        order = []
        fake = FakeMonitoringAPI()

        def spying_post(url, body):
            if url.startswith("https://logging.googleapis.com/"):
                order.append("metrics")
            elif url.endswith("uptimeCheckConfigs"):
                order.append("uptime")
            else:
                order.append("policies")
            return fake.post(url, body)

        args = _args(apply=True, channel="projects/%s/notificationChannels/1" % PROJECT)
        m.run(args, fake.get, spying_post, fake.patch, fake.put)
        self.assertIn("metrics", order)
        self.assertIn("uptime", order)
        self.assertIn("policies", order)
        weight = {"metrics": 0, "uptime": 1, "policies": 2}
        self.assertEqual(order, sorted(order, key=lambda kind: weight[kind]))

    # --- test_log_metric_identity_is_name_not_display_name ---
    def test_log_metric_identity_is_name_not_display_name(self):
        calls = []

        def get(url, params):
            calls.append(url)
            if url.endswith("/metrics/metric-one"):
                return {"name": "metric-one", "filter": "a"}
            raise m.ApplyError("http_404")

        result = m.existing_metrics(get, PROJECT, ["metric-one", "metric-two"])
        expected_base = "https://logging.googleapis.com/v2/projects/%s/metrics/" % PROJECT
        self.assertEqual(set(calls), {expected_base + "metric-one", expected_base + "metric-two"})
        self.assertEqual(result, {"metric-one": {"name": "metric-one", "filter": "a"}})
        # Falsification target: a naive generalization of _existing_by_display_name
        # would list the metrics collection and match on displayName; LogMetric
        # has no displayName field at all, so every call here must be a direct
        # by-name GET, never a bare collection listing call.
        for url in calls:
            self.assertTrue(url.startswith(expected_base))
            self.assertNotEqual(url, expected_base.rstrip("/"))

    # --- test_metric_update_uses_put_without_updatemask ---
    def test_metric_update_uses_put_without_updatemask(self):
        captured = []

        def get(url, params):
            if url.endswith("/metrics/chirp_metric"):
                return {"name": "chirp_metric", "filter": "old"}
            raise m.ApplyError("http_404")

        def put(url, body):
            captured.append(("PUT", url))
            return {"name": "chirp_metric"}

        local = [{"name": "chirp_metric", "filter": "new", "description": "d MONITORING-RUNBOOK.md"}]
        existing = m.existing_metrics(get, PROJECT, ["chirp_metric"])
        plan = m.plan_metrics(local, existing)
        self.assertEqual([p["action"] for p in plan], ["update"])
        m.apply_metrics(plan, {"chirp_metric": local[0]}, PROJECT, _refusing, put)
        self.assertEqual(len(captured), 1)
        method, url = captured[0]
        self.assertEqual(method, "PUT")
        self.assertEqual(url, "https://logging.googleapis.com/v2/projects/%s/metrics/chirp_metric" % PROJECT)
        self.assertNotIn("updateMask", url)

    # --- test_update_patches_by_resource_name_with_updatemask_limited_to_owned_fields ---
    def test_update_patches_by_resource_name_with_updatemask_limited_to_owned_fields(self):
        fake = FakeMonitoringAPI()
        channel = "projects/%s/notificationChannels/1" % PROJECT
        args = _args(apply=True, channel=channel)
        first = m.run(args, fake.get, fake.post, fake.patch, fake.put)
        self.assertGreater(fake.post_calls, 0)

        # Force a genuine content diff: copy every real policy file into a
        # temp dir, verbatim, except one field of one file is bumped. Reading
        # the raw text with json.loads works even with the {{PROJECT}}/
        # {{NOTIFICATION_CHANNEL}}/{{API_HOST}}/{{WS_HOST}} placeholders still
        # in place, since they are just string content inside valid JSON.
        target = None
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            for path in sorted(POLICIES_DIR.glob("*.json")):
                raw = path.read_text(encoding="utf-8")
                if target is None:
                    body = json.loads(raw)
                    # c407: this used to nudge alertStrategy.notificationRateLimit,
                    # which no policy carries any more because the API rejects it on
                    # metric policies. documentation.content is an owned field in
                    # UPDATE_MASK_FIELDS and equally arbitrary, so the diff this test
                    # needs is unchanged in kind.
                    body["documentation"]["content"] += " Edited for the update path. See MONITORING-RUNBOOK.md."
                    target = body["displayName"]
                    raw = json.dumps(body)
                (tmp_dir / path.name).write_text(raw, encoding="utf-8")

            existing_name = fake.policies[target]["name"]
            fake.post_calls = fake.patch_calls = 0
            captured_urls = []
            captured_bodies = []
            real_patch = fake.patch

            def spying_patch(url, body):
                captured_urls.append(url)
                captured_bodies.append(body)
                return real_patch(url, body)

            args2 = _args(apply=True, channel=channel, policies_dir=str(tmp_dir))
            second = m.run(args2, fake.get, fake.post, spying_patch, fake.put)

        by_name = {p["displayName"]: p for p in second["policies"]}
        self.assertEqual(by_name[target]["action"], "update")
        self.assertEqual(fake.post_calls, 0)
        self.assertEqual(fake.patch_calls, 1)
        self.assertEqual(len(captured_urls), 1)

        resource_url, _, query = captured_urls[0].partition("?")
        self.assertEqual(resource_url, "https://monitoring.googleapis.com/v3/" + existing_name)
        mask = urllib.parse.parse_qs(query)["updateMask"][0].split(",")
        # c407: the mask names the owned fields PRESENT in the body being sent, and
        # no policy carries alertStrategy any more (the API rejects the only thing we
        # put in it on a metric policy). UPDATE_MASK_FIELDS deliberately still lists
        # it, because ownership is what lets a future removal clear the field
        # remotely, so the assertion is subset-plus-equality-with-what-is-present
        # rather than equality with the whole ownership list.
        # The oracle is the body that was ACTUALLY PATCHed. It used to be read from
        # redis-memory-pressure.json while the test mutated and sent a different file,
        # and passed only because every policy happened to share one key set - found by
        # chirps-36's review, and the same global-versus-local mistake as the rest of
        # this card.
        self.assertEqual(len(captured_bodies), 1)
        sent = captured_bodies[0]
        self.assertEqual(sent["displayName"], target)
        self.assertTrue(set(mask) <= set(m.UPDATE_MASK_FIELDS))
        self.assertEqual(set(mask), {f for f in m.UPDATE_MASK_FIELDS if f in sent} | {"notificationChannels"})
        for owned_field in m.SERVER_ASSIGNED_FIELDS:
            self.assertNotIn(owned_field, mask)

    # --- uptime PATCH uses an explicit updateMask too ---
    def test_uptime_update_patches_with_explicit_updatemask(self):
        fake = FakeMonitoringAPI()
        channel = "projects/%s/notificationChannels/1" % PROJECT
        args = _args(apply=True, channel=channel)
        m.run(args, fake.get, fake.post, fake.patch, fake.put)
        target_display = json.loads((UPTIME_DIR / "chirp-api-health.json").read_text())["displayName"]
        existing_name = fake.uptime[target_display]["name"]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            for path in sorted(UPTIME_DIR.glob("*.json")):
                body = json.loads(path.read_text())
                if body["displayName"] == target_display:
                    body["period"] = "120s"
                (tmp_dir / path.name).write_text(json.dumps(body), encoding="utf-8")

            captured_urls = []
            real_patch = fake.patch

            def spying_patch(url, body):
                captured_urls.append(url)
                return real_patch(url, body)

            fake.patch_calls = fake.post_calls = 0
            args2 = _args(apply=True, channel=channel, uptime_dir=str(tmp_dir))
            second = m.run(args2, fake.get, fake.post, spying_patch, fake.put)

        by_display = {u["displayName"]: u for u in second["uptime"]}
        self.assertEqual(by_display[target_display]["action"], "update")
        self.assertEqual(len(captured_urls), 1)
        resource_url, _, query = captured_urls[0].partition("?")
        self.assertEqual(resource_url, "https://monitoring.googleapis.com/v3/" + existing_name)
        mask = urllib.parse.parse_qs(query)["updateMask"][0].split(",")
        self.assertEqual(set(mask), set(m.UPTIME_UPDATE_MASK_FIELDS) - {"checkerType"})

    def test_uptime_patch_explicitly_restores_materialized_public_checker_default(self):
        local = json.loads((UPTIME_DIR / "chirp-api-health.json").read_text())
        self.assertNotIn("checkerType", local)
        for remote_checker in ("STATIC_IP_CHECKERS", "VPC_CHECKERS"):
            with self.subTest(remote_checker=remote_checker):
                fake = FakeMonitoringAPI()
                args = _args(apply=True, channel="projects/%s/notificationChannels/1" % PROJECT)
                self.assertEqual(m.run(args, fake.get, fake.post, fake.patch, fake.put)["exit_code"], 0)
                remote = fake.uptime[local["displayName"]]
                identity = remote["name"]
                # The provider materializes this field, unlike the basic fake.
                # A normal period repair must retain it; a changed checker must
                # explicitly restore the default rather than clear an enum.
                remote["checkerType"] = remote_checker
                remote["period"] = "300s"
                captured = []

                def patch_uptime(url, body):
                    captured.append((url, deepcopy(body)))
                    return fake.patch(url, body)

                fake.post_calls = fake.patch_calls = fake.put_calls = 0
                report = m.run(args, fake.get, fake.post, patch_uptime, fake.put)
                self.assertEqual(report["exit_code"], 0)
                self.assertEqual((fake.post_calls, fake.patch_calls, fake.put_calls), (0, 1, 0))
                url, body = captured[0]
                resource, _, query = url.partition("?")
                self.assertEqual(resource, "https://monitoring.googleapis.com/v3/" + identity)
                self.assertEqual(body["name"], identity)
                self.assertIn("checkerType", urllib.parse.parse_qs(query)["updateMask"][0].split(","))
                self.assertEqual(body.get("checkerType"), "STATIC_IP_CHECKERS")
                self.assertEqual(body["period"], local["period"])
                self.assertEqual(fake.uptime[local["displayName"]]["checkerType"], "STATIC_IP_CHECKERS")
                again = m.run(args, fake.get, _refusing, _refusing, _refusing)
                self.assertEqual(again["exit_code"], 0)
                self.assertEqual({row["action"] for row in again["uptime"]}, {"noop"})

    def test_remote_rename_between_list_and_full_get_fails_before_any_write(self):
        for kind in ("uptime", "policies"):
            with self.subTest(kind=kind):
                fake = FakeMonitoringAPI()
                args = _args(apply=True, channel="projects/%s/notificationChannels/1" % PROJECT)
                self.assertEqual(m.run(args, fake.get, fake.post, fake.patch, fake.put)["exit_code"], 0)
                remote = next(iter(getattr(fake, kind).values()))
                name, display = remote["name"], remote["displayName"]
                collection = "uptimeCheckConfigs" if kind == "uptime" else "alertPolicies"
                calls = []

                def renamed_get(url, params):
                    body = fake.get(url, params)
                    if url.endswith("/" + collection):
                        self.assertIn({"name": name, "displayName": display}, body[collection])
                        calls.append("list")
                    elif url == "https://monitoring.googleapis.com/v3/" + name:
                        self.assertEqual(calls, ["list"])
                        self.assertEqual(body["name"], name)
                        body["displayName"] = display + " renamed after listing"
                        calls.append("full_get")
                    return body

                # Earlier phases have pending creates; failed identity preflight
                # must prevent those writes as well as the renamed PATCH.
                fake.metrics.clear()
                fake.post_calls = fake.patch_calls = fake.put_calls = 0
                with self.assertRaisesRegex(m.ApplyError, "^invalid_schema$"):
                    m.run(args, renamed_get, fake.post, fake.patch, fake.put)
                self.assertEqual(calls, ["list", "full_get"])
                self.assertEqual((fake.post_calls, fake.patch_calls, fake.put_calls), (0, 0, 0))

    # --- test_every_policy_json_validates_required_shape_and_inventory_membership ---
    def test_every_policy_json_validates_required_shape_and_inventory_membership(self):
        available = _inventory_available_metrics()
        defined_log_metrics = {m.LOG_METRIC_TYPE_PREFIX + "sql_pool_capacity_503",
                                m.LOG_METRIC_TYPE_PREFIX + "rate_limit_fallback",
                                m.LOG_METRIC_TYPE_PREFIX + "chirp_purge_aggregate",
                                m.LOG_METRIC_TYPE_PREFIX + "ws_connect_capacity_rejected",
                                m.LOG_METRIC_TYPE_PREFIX + "ws_connect_suspended_rejected",
                                m.LOG_METRIC_TYPE_PREFIX + "media_reconcile_eligible_runs",
                                m.LOG_METRIC_TYPE_PREFIX + "media_reconcile_unresolved_runs"}
        defined_hosts = {API_HOST, WS_HOST}
        for path in sorted(POLICIES_DIR.glob("*.json")):
            with self.subTest(file=path.name):
                policy = json.loads(path.read_text()
                                     .replace("{{PROJECT}}", PROJECT)
                                     .replace("{{NOTIFICATION_CHANNEL}}", "projects/x/notificationChannels/1")
                                     .replace("{{API_HOST}}", API_HOST)
                                     .replace("{{WS_HOST}}", WS_HOST))
                errors = m.required_shape_errors(policy, available, defined_log_metrics, defined_hosts)
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

        # Same vacuous-test guard, but for a metric type hidden only in a
        # ratio condition's denominatorFilter: a checker that only scanned
        # "filter" would report zero errors here, silently letting a
        # non-inventoried denominator metric through.
        with tempfile.TemporaryDirectory() as tmp:
            bad_ratio_path = Path(tmp) / "bad_ratio.json"
            bad_ratio_path.write_text(json.dumps({
                "displayName": "bad-ratio", "combiner": "OR",
                "documentation": {"content": "See MONITORING-RUNBOOK.md."},
                "conditions": [{"conditionThreshold": {
                    "filter": 'metric.type="run.googleapis.com/request_count"',
                    "denominatorFilter": 'metric.type="totally.fake/not_real_metric"',
                }}],
            }))
            bad_ratio_policy = json.loads(bad_ratio_path.read_text())
            self.assertIn(
                "run.googleapis.com/request_count",
                m.metric_types_referenced(bad_ratio_policy),
            )
            self.assertIn(
                "totally.fake/not_real_metric",
                m.metric_types_referenced(bad_ratio_policy),
            )
            ratio_errors = m.required_shape_errors(bad_ratio_policy, available)
            self.assertTrue(
                any(e == "metric_not_in_inventory:totally.fake/not_real_metric"
                    for e in ratio_errors),
                msg=str(ratio_errors),
            )

    # --- test_media_reconcile_metrics_and_policies_reference_their_own_signal ---
    def test_media_reconcile_metrics_and_policies_reference_their_own_signal(self):
        """T3 (board c414): each of the two new media-reconcile log metrics must
        filter on the job name, the aggregate event, AND its own >0 comparison - a
        metric whose filter forgot the threshold would count every run, not just
        the runs worth a human's attention. Each policy must reference its OWN
        metric type, not its sibling's - eligible firing on unresolved counts (or
        the reverse) would send a manager to review the wrong signal.

        Regression this guards against: drop the >0 comparison from the eligible
        metric's filter.
        """
        cases = {
            "eligible": ("media-reconcile-eligible-runs.json", "media_reconcile_eligible_runs",
                         "jsonPayload.eligible>0"),
            "unresolved": ("media-reconcile-unresolved-runs.json", "media_reconcile_unresolved_runs",
                           "jsonPayload.unresolved>0"),
        }
        for label, (filename, metric_name, own_comparison) in cases.items():
            with self.subTest(kind="metric", label=label):
                metric = json.loads((METRICS_DIR / filename).read_text())
                self.assertEqual(metric["name"], metric_name)
                filt = metric["filter"]
                self.assertIn('resource.type="cloud_run_job"', filt)
                self.assertIn('resource.labels.job_name="chirp-media-reconcile"', filt)
                self.assertIn('jsonPayload.event="media_reconcile_aggregate"', filt)
                self.assertIn(own_comparison, filt)
            with self.subTest(kind="policy", label=label):
                policy = json.loads((POLICIES_DIR / filename).read_text())
                policy_filter = policy["conditions"][0]["conditionThreshold"]["filter"]
                self.assertIn(
                    'metric.type="logging.googleapis.com/user/%s"' % metric_name, policy_filter
                )
                sibling_name = (cases["unresolved"] if label == "eligible" else cases["eligible"])[1]
                self.assertNotIn(sibling_name, policy_filter)

    # --- test_policy_referencing_undefined_uptime_check_or_log_metric_is_skipped ---
    def test_policy_referencing_undefined_uptime_check_or_log_metric_is_skipped(self):
        available = _inventory_available_metrics()
        defined_log_metrics = {m.LOG_METRIC_TYPE_PREFIX + "sql_pool_capacity_503"}
        defined_hosts = {API_HOST}

        good = {
            "displayName": "good-cross-ref", "combiner": "OR",
            "documentation": {"content": "See MONITORING-RUNBOOK.md."},
            "conditions": [{"conditionThreshold": {
                "filter": 'metric.type="logging.googleapis.com/user/sql_pool_capacity_503"'}}],
        }
        bad_metric = {
            "displayName": "bad-log-metric-ref", "combiner": "OR",
            "documentation": {"content": "See MONITORING-RUNBOOK.md."},
            "conditions": [{"conditionThreshold": {
                "filter": 'metric.type="logging.googleapis.com/user/does_not_exist"'}}],
        }
        bad_uptime = {
            "displayName": "bad-uptime-ref", "combiner": "OR",
            "documentation": {"content": "See MONITORING-RUNBOOK.md."},
            "conditions": [{"conditionThreshold": {
                "filter": 'resource.labels.host="nope.example.com" '
                          'AND metric.type="monitoring.googleapis.com/uptime_check/check_passed"'}}],
        }

        self.assertEqual(m.required_shape_errors(good, available, defined_log_metrics, defined_hosts), [])
        bad_metric_errors = m.required_shape_errors(bad_metric, available, defined_log_metrics, defined_hosts)
        self.assertIn("metric_not_in_inventory:logging.googleapis.com/user/does_not_exist", bad_metric_errors)
        bad_uptime_errors = m.required_shape_errors(bad_uptime, available, defined_log_metrics, defined_hosts)
        self.assertIn("uptime_check_host_not_in_repo:nope.example.com", bad_uptime_errors)

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "good.json").write_text(json.dumps(good))
            (Path(tmp) / "bad_metric.json").write_text(json.dumps(bad_metric))
            (Path(tmp) / "bad_uptime.json").write_text(json.dumps(bad_uptime))
            loaded, errors = m.load_local_policies(tmp, PROJECT, None, available,
                                                     defined_log_metrics, defined_hosts)
        self.assertEqual([p["displayName"] for p in loaded], ["good-cross-ref"])
        self.assertEqual({e["file"] for e in errors}, {"bad_metric.json", "bad_uptime.json"})

    # --- purge: no missed-schedule conditionAbsent can exist (c407) ---
    def test_no_condition_absent_can_express_this_job_cadence(self):
        """Why the missed-schedule half is absent, pinned so nobody re-adds it (c407).

        The committed evidence says chirp-purge-daily fires once a day. Cloud
        Monitoring answered INVALID_ARGUMENT to a 24h conditionAbsent duration with
        "Durations longer than 23h30m are not supported" - a real API ceiling BELOW
        this job's cadence. So no absence window the API would accept spans the gap
        between two healthy runs: if the completion metric reports only when an
        execution completes, the condition fires before every scheduled run, and if
        it reports zeros in between, it never fires at all. Which of those holds is
        unverified (c410 checks it); neither is a working alert.
        There is no correct conditionAbsent for a 24h job, which is a property of the
        cadence and the API, not an oversight in this file. The raw error is kept in
        infra/monitoring/evidence/c407-live-create-errors-2026-09-13.json.

        This test pins both halves of that: the schedule really is daily, and the
        policy really carries no conditionAbsent. If someone shortens the job's
        cadence below 23h30m, this test is where they will find out that the
        missed-schedule condition becomes possible again.
        """
        evidence_path = REPO_ROOT / "infra/monitoring/evidence/c398-scheduler-chirp-purge-daily-2026-09-10.json"
        evidence = json.loads(evidence_path.read_text())
        minute, hour, dom, month, dow = evidence["schedule"].split()
        self.assertEqual((dom, month, dow), ("*", "*", "*"))
        self.assertNotIn(",", hour)
        self.assertNotIn("/", hour)
        cadence_seconds = 24 * 60 * 60
        self.assertGreater(cadence_seconds, 23 * 3600 + 30 * 60, "the API ceiling")

        policy = json.loads((POLICIES_DIR / "chirp-purge-job-failure.json").read_text())
        self.assertEqual([c for c in policy["conditions"] if "conditionAbsent" in c], [])
        self.assertTrue(
            any("conditionThreshold" in c for c in policy["conditions"]),
            "the failure-count half must still ship",
        )
        self.assertIn("23h30m", policy["documentation"]["content"])

    def test_uptime_aggregation_reducer_matches_the_aligner_value_type(self):
        """A type rule the field-name validator cannot see (board c407).

        The API rejected REDUCE_COUNT_FALSE with "The reducer cannot be applied to
        metrics with value type DOUBLE": ALIGN_FRACTION_TRUE turns the BOOL
        check_passed series into a fraction, and a count-of-false reducer has nothing
        to count. ALIGN_NEXT_OLDER keeps the BOOL, which is what makes the
        "more than one checker reporting failure" threshold mean what it says.
        """
        for name in ("chirp-api-uptime-failure", "chirp-ws-uptime-failure"):
            with self.subTest(policy=name):
                policy = json.loads((POLICIES_DIR / f"{name}.json").read_text())
                agg = policy["conditions"][0]["conditionThreshold"]["aggregations"][0]
                if agg.get("crossSeriesReducer") == "REDUCE_COUNT_FALSE":
                    self.assertNotEqual(
                        agg.get("perSeriesAligner"), "ALIGN_FRACTION_TRUE",
                        "ALIGN_FRACTION_TRUE produces DOUBLE; REDUCE_COUNT_FALSE needs BOOL",
                    )

    # --- test_purge_aggregate_metric_and_policy_distinguish_healthy_from_unhealthy_status ---
    def test_purge_aggregate_metric_and_policy_distinguish_healthy_from_unhealthy_status(self):
        metric = json.loads((METRICS_DIR / "purge-job-aggregate.json").read_text())
        self.assertEqual(metric.get("labelExtractors", {}).get("status"), "EXTRACT(jsonPayload.status)")

        policy = json.loads((POLICIES_DIR / "purge-backlog-blocked.json").read_text())
        filt = policy["conditions"][0]["conditionThreshold"]["filter"]
        statuses = set(re.findall(r'metric\.labels\.status="([^"]+)"', filt))
        self.assertEqual(statuses, {"blocked", "incomplete", "timed_out", "failed"})
        self.assertNotIn("preview", statuses)
        self.assertNotIn("complete", statuses)

    def test_a_notification_rate_limit_on_a_metric_policy_is_refused(self):
        """The API's rule, not ours, learned the expensive way (board c407).

        Cloud Monitoring answers INVALID_ARGUMENT with "only log-based alert
        policies may specify a notification rate limit" for any policy carrying
        alertStrategy.notificationRateLimit without a conditionMatchedLog
        condition. All 16 shipped policies carried one, every test passed, and the
        first real create failed - because validate_rest_shape only asks whether a
        field NAME is known, which says nothing about when the field is allowed.

        Constructed both ways so the RULE discriminates: it fires for a threshold
        condition and not for a matched-log one. c410 supports the matched-log
        shape; require exact acceptance so an unrelated error cannot conceal a
        broken rule or merely replace the former unknown-key refusal.
        """
        available = _inventory_available_metrics()
        base = {
            "displayName": "rate limited", "combiner": "OR",
            "documentation": {"content": "See MONITORING-RUNBOOK.md."},
            "alertStrategy": {"notificationRateLimit": {"period": "1800s"}},
            "conditions": [{"conditionThreshold": {
                "filter": 'metric.type="run.googleapis.com/request_count"'}}],
        }
        assert "notification_rate_limit_on_non_log_policy" in m.required_shape_errors(base, available, [], [])

        log_based = json.loads(json.dumps(base))
        log_based["conditions"] = [{"conditionMatchedLog": {"filter": 'resource.type="cloud_run_revision"'}}]
        log_errors = m.required_shape_errors(log_based, available, [], [])
        assert "notification_rate_limit_on_non_log_policy" not in log_errors
        assert log_errors == [], log_errors

    def test_no_shipped_policy_carries_a_notification_rate_limit(self):
        """The 16 real files, not a fixture: this is what actually failed in prod."""
        for path in sorted(POLICIES_DIR.glob("*.json")):
            with self.subTest(file=path.name):
                policy = json.loads(path.read_text())
                strategy = policy.get("alertStrategy") or {}
                log_based = any("conditionMatchedLog" in c for c in policy.get("conditions", []))
                self.assertFalse(
                    "notificationRateLimit" in strategy and not log_based,
                    f"{path.name} carries a notification rate limit the API will reject",
                )

    def test_a_bool_only_reducer_after_a_numeric_aligner_is_refused(self):
        """c407 cause 2 as a guard, not just a repaired file (chirps-36's review).

        Constructed both ways so it discriminates: the pairing the API rejected live
        is refused, and the type-preserving pairing the fixed policies use is not.
        """
        available = _inventory_available_metrics()

        def policy(aligner):
            return {
                "displayName": "typed", "combiner": "OR",
                "documentation": {"content": "See MONITORING-RUNBOOK.md."},
                "conditions": [{"conditionThreshold": {
                    "filter": 'metric.type="run.googleapis.com/request_count"',
                    "aggregations": [{"alignmentPeriod": "60s", "perSeriesAligner": aligner,
                                      "crossSeriesReducer": "REDUCE_COUNT_FALSE"}]}}],
            }

        bad = m.required_shape_errors(policy("ALIGN_FRACTION_TRUE"), available, [], [])
        self.assertIn("reducer_cannot_consume_aligner_output:ALIGN_FRACTION_TRUE->REDUCE_COUNT_FALSE", bad)
        good = m.required_shape_errors(policy("ALIGN_NEXT_OLDER"), available, [], [])
        self.assertFalse([e for e in good if e.startswith("reducer_cannot_consume")])

    def test_a_condition_absent_over_the_api_ceiling_is_refused(self):
        """c407 cause 3 as a guard: the API caps conditionAbsent at 23h30m."""
        available = _inventory_available_metrics()

        def policy(duration):
            return {
                "displayName": "absent", "combiner": "OR",
                "documentation": {"content": "See MONITORING-RUNBOOK.md."},
                "conditions": [{"conditionAbsent": {
                    "filter": 'metric.type="run.googleapis.com/request_count"', "duration": duration}}],
            }

        self.assertIn("condition_absent_duration_over_api_ceiling",
                      m.required_shape_errors(policy("86400s"), available, [], []))
        self.assertNotIn("condition_absent_duration_over_api_ceiling",
                         m.required_shape_errors(policy("84600s"), available, [], []))

    def test_durations_must_be_decimal_seconds_on_both_condition_kinds(self):
        """c407 round 3, from chirps-36's re-review of 59de604.

        The round-two parser returned None for anything it could not read and the
        caller skipped None, so "24h" - how a person writes a day - passed while the
        API would refuse it. Constructed in both directions and on both kinds.
        Round four adds a trailing newline and non-ASCII digits, both of which the
        round-three pattern let through (chirps-36's spot-check).
        """
        available = _inventory_available_metrics()

        def policy(kind, duration):
            spec = {"filter": 'metric.type="run.googleapis.com/request_count"', "duration": duration}
            return {
                "displayName": "durations", "combiner": "OR",
                "documentation": {"content": "See MONITORING-RUNBOOK.md."},
                "conditions": [{kind: spec}],
            }

        for kind, duration in (("conditionAbsent", "24h"), ("conditionAbsent", "1d"),
                               ("conditionAbsent", "86400"), ("conditionAbsent", "-5s"),
                               ("conditionThreshold", "5m"), ("conditionThreshold", "1440m"),
                               ("conditionAbsent", "60s\n"), ("conditionThreshold", "٦٠s"),
                               ("conditionAbsent", "1e3s"), ("conditionThreshold", " 60s")):
            with self.subTest(kind=kind, duration=duration):
                self.assertIn(f"duration_not_seconds_format:{kind}",
                              m.required_shape_errors(policy(kind, duration), available, [], []))

        for kind, duration in (("conditionAbsent", "84600s"), ("conditionThreshold", "0s"),
                               ("conditionThreshold", "300s"), ("conditionAbsent", "120.5s")):
            with self.subTest(kind=kind, duration=duration):
                errors = m.required_shape_errors(policy(kind, duration), available, [], [])
                self.assertFalse([e for e in errors if e.startswith("duration_not_seconds_format")], errors)

    def test_the_absence_ceiling_constant_matches_the_captured_api_error(self):
        """The 23h30m ceiling is known only from a live rejection, not from any reference
        text, so the constant is pinned to the verbatim capture rather than to prose
        (chirps-36's evidence ask on c407)."""
        evidence = json.loads((REPO_ROOT / "infra/monitoring/evidence/c407-live-create-errors-2026-09-13.json").read_text())
        lines = [c["stderr"] for c in evidence["captures"] if "condition_absent.duration" in c["stderr"]]
        self.assertEqual(len(lines), 1)
        match = re.search(r"Durations longer than (\d+)h(\d+)m are not supported", lines[0])
        self.assertIsNotNone(match, lines[0])
        self.assertEqual(m.CONDITION_ABSENT_MAX_SECONDS, int(match.group(1)) * 3600 + int(match.group(2)) * 60)

    # --- strict REST-shape round-trip: unknown keys fail ---
    def test_strict_rest_shape_rejects_unknown_keys(self):
        good = json.loads((POLICIES_DIR / "redis-memory-pressure.json").read_text()
                           .replace("{{PROJECT}}", PROJECT).replace("{{NOTIFICATION_CHANNEL}}", "x"))
        self.assertEqual(m.validate_rest_shape(good), [])
        bad = json.loads(json.dumps(good))
        bad["conditions"][0]["conditionThreshold"]["thresholdVal"] = 1  # typo of thresholdValue
        errors = m.validate_rest_shape(bad)
        self.assertTrue(any("thresholdVal" in e and "unknown_key" in e for e in errors))

    # --- LogMetric/UptimeCheckConfig strict shape also rejects unknown keys ---
    def test_metric_and_uptime_rest_shape_reject_unknown_keys(self):
        good_metric = json.loads((METRICS_DIR / "sql-pool-capacity-503.json").read_text())
        self.assertEqual(m.validate_rest_shape(good_metric, m.LOG_METRIC_SCHEMA), [])
        bad_metric = json.loads(json.dumps(good_metric))
        bad_metric["metricDescriptor"]["metricKnd"] = "DELTA"  # typo of metricKind
        errors = m.validate_rest_shape(bad_metric, m.LOG_METRIC_SCHEMA)
        self.assertTrue(any("metricKnd" in e and "unknown_key" in e for e in errors))

        good_uptime = json.loads((UPTIME_DIR / "chirp-api-health.json").read_text())
        self.assertEqual(m.validate_rest_shape(good_uptime, m.UPTIME_CHECK_SCHEMA), [])
        bad_uptime = json.loads(json.dumps(good_uptime))
        bad_uptime["httpCheck"]["reqestMethod"] = "GET"  # typo of requestMethod
        errors2 = m.validate_rest_shape(bad_uptime, m.UPTIME_CHECK_SCHEMA)
        self.assertTrue(any("reqestMethod" in e and "unknown_key" in e for e in errors2))

    # --- the strict validator sits on the real load path, not only in a unit test ---
    def test_run_skips_unknown_key_policy_before_planning(self):
        good = json.loads((POLICIES_DIR / "redis-memory-pressure.json").read_text())
        bad = json.loads(json.dumps(good))
        bad["displayName"] = "bad shape"
        bad["conditions"][0]["conditionThreshold"]["thresholdVal"] = 1  # typo of thresholdValue
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "good.json").write_text(json.dumps(good))
            (Path(tmp) / "bad.json").write_text(json.dumps(bad))
            report = m.run(_args(policies_dir=tmp), get=_no_op_get, post=_refusing, patch=_refusing, put=_refusing)
        self.assertEqual([entry["displayName"] for entry in report["policies"]], [good["displayName"]])
        skipped_names = [entry["file"] for entry in report["skipped_files"]]
        self.assertEqual(skipped_names, ["bad.json"])
        self.assertIn("rest_shape:$.conditions[0].conditionThreshold.thresholdVal: unknown_key",
                      report["skipped_files"][0]["reason"])
        self.assertEqual(report["exit_code"], 1)

    def test_documentation_without_runbook_reference_is_flagged(self):
        policy = {
            "displayName": "no runbook", "combiner": "OR",
            "documentation": {"content": "Wake someone up."},
            "conditions": [{"conditionThreshold": {
                "filter": 'metric.type="run.googleapis.com/request_count"'}}],
        }
        available = _inventory_available_metrics()
        self.assertIn("documentation_missing_runbook_reference", m.required_shape_errors(policy, available))
        policy["documentation"]["content"] = "See MONITORING-RUNBOOK.md."
        self.assertNotIn("documentation_missing_runbook_reference", m.required_shape_errors(policy, available))

    def test_metric_documentation_without_runbook_reference_is_flagged(self):
        metric = {"name": "x", "filter": "f", "description": "Wake someone up."}
        self.assertIn("documentation_missing_runbook_reference", m.required_shape_errors_metric(metric))
        metric["description"] = "See MONITORING-RUNBOOK.md."
        self.assertNotIn("documentation_missing_runbook_reference", m.required_shape_errors_metric(metric))

    def test_uptime_missing_or_unsubstituted_host_is_flagged(self):
        config = {"displayName": "x", "monitoredResource": {"type": "uptime_url",
                                                              "labels": {"host": "{{API_HOST}}"}}}
        self.assertIn("missing_or_unsubstituted_host", m.required_shape_errors_uptime(config))
        config["monitoredResource"]["labels"]["host"] = "real.example.com"
        self.assertNotIn("missing_or_unsubstituted_host", m.required_shape_errors_uptime(config))

    # --- test_no_hardcoded_project_number_or_channel_outside_templating_point ---
    def test_no_hardcoded_project_number_or_channel_outside_templating_point(self):
        project_number = "593616178468"
        for path in sorted(Path(REPO_ROOT / "infra/monitoring").rglob("*.json")):
            text = path.read_text(encoding="utf-8")
            with self.subTest(file=str(path)):
                self.assertEqual(_scan_for_hardcoded(text, project_number), [])
        # Every notificationChannels value in a policy file is exactly the
        # placeholder token, never a literal channel id.
        for path in sorted(POLICIES_DIR.glob("*.json")):
            body = json.loads(path.read_text())
            for channel in body.get("notificationChannels", []):
                self.assertEqual(channel, "{{NOTIFICATION_CHANNEL}}")
        # Every uptime file's host is exactly the {{API_HOST}}/{{WS_HOST}}
        # placeholder, never a literal hostname (which would embed the live
        # project number, the exact leak this scanner exists to catch).
        for path in sorted(UPTIME_DIR.glob("*.json")):
            body = json.loads(path.read_text())
            host = body["monitoredResource"]["labels"]["host"]
            self.assertIn(host, (m.API_HOST_PLACEHOLDER, m.WS_HOST_PLACEHOLDER))

        # The scanner must actually flag a bad fixture, or it is vacuous:
        # call the real _scan_for_hardcoded helper, not just check the fixture
        # contains what it wrote.
        with tempfile.TemporaryDirectory() as tmp:
            bad_path = Path(tmp) / "bad.json"
            bad_path.write_text(json.dumps({"note": "belongs to project " + project_number}))
            self.assertEqual(
                _scan_for_hardcoded(bad_path.read_text(), project_number),
                ["hardcoded_project_number"],
            )

    # --- test_uptime_json_never_contains_the_literal_hostname ---
    def test_uptime_json_never_contains_the_literal_hostname(self):
        project_number = "593616178468"
        real_host = "chirp-api-%s.us-central1.run.app" % project_number
        bad_uptime_json = json.dumps({
            "displayName": "leaked", "monitoredResource": {"type": "uptime_url",
                                                             "labels": {"host": real_host}},
        })
        # Vacuous-test guard: the real committed uptime files never trip this
        # scanner (asserted below); a fixture with the real hostname hardcoded
        # instead of {{API_HOST}} must trip it, or the scanner protects nothing.
        self.assertEqual(_scan_for_hardcoded(bad_uptime_json, project_number), ["hardcoded_project_number"])
        for path in sorted(UPTIME_DIR.glob("*.json")):
            text = path.read_text(encoding="utf-8")
            self.assertEqual(_scan_for_hardcoded(text, project_number), [])

    # --- test_new_kinds_registered_in_backend_ci ---
    def test_new_kinds_registered_in_backend_ci(self):
        # The existing c370 registration file (backend/tests/test_c370_monitoring_apply_collector.py)
        # imports THIS module directly by file path and re-exports its
        # MonitoringApplyTests class; since the c398 methods live in the same
        # class in the same file, they are already pulled into backend CI
        # without a second registration file. A second file mirroring the
        # c370 pattern would just run this same class twice under two names.
        backend_reg_path = REPO_ROOT / "backend/tests/test_c370_monitoring_apply_collector.py"
        spec2 = importlib.util.spec_from_file_location("c370_backend_registration_check", backend_reg_path)
        module2 = importlib.util.module_from_spec(spec2)
        spec2.loader.exec_module(module2)
        suite = unittest.TestLoader().loadTestsFromModule(module2)

        test_ids = set()

        def _collect(container):
            for item in container:
                if isinstance(item, unittest.TestSuite):
                    _collect(item)
                else:
                    test_ids.add(item.id())

        _collect(suite)
        for expected in ("test_apply_order_is_metrics_then_uptime_then_policies",
                          "test_metric_update_uses_put_without_updatemask",
                          "test_log_metric_identity_is_name_not_display_name",
                          "test_cross_checkout_defaults_and_explicit_overrides_read_and_write_expected_bodies",
                          "test_empty_directories_do_not_fall_back_to_another_checkout"):
            self.assertTrue(any(expected in test_id for test_id in test_ids),
                             msg="%s missing from backend CI registration; test_ids=%s" % (expected, test_ids))

    # --- reader() extends monitoring_check's GET-only closure with POST/PATCH/PUT ---
    def test_reader_post_patch_and_put_use_bearer_auth_and_correct_verb(self):
        import io
        import ssl
        args = m.parse_arguments(["--project", PROJECT, "--api-host", "api.example.test", "--ws-host", "ws.example.test"])
        captured = []

        class Opener:
            def open(self, request, timeout):
                captured.append({
                    "method": request.get_method(), "url": request.full_url,
                    "auth": request.get_header("Authorization"),
                    "content_type": request.get_header("Content-type"),
                    "body": json.loads(request.data) if request.data else None,
                })
                return io.BytesIO(b'{"name": "projects/x/alertPolicies/1"}')

        def build(*handlers):
            context = handlers[1]._context
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertIsInstance(handlers[0], m.NoRedirect)
            return Opener()

        with patch.object(m.subprocess, "run", return_value=type("R", (), {"returncode": 0, "stdout": "tok"})()):
            with patch.object(m.urllib.request, "build_opener", side_effect=build):
                get, post, patch_fn, put_fn = m.reader(args)
                post("https://monitoring.googleapis.com/v3/projects/x/alertPolicies", {"displayName": "d"})
                patch_fn("https://monitoring.googleapis.com/v3/projects/x/alertPolicies/1", {"displayName": "d"})
                put_fn("https://logging.googleapis.com/v2/projects/x/metrics/foo", {"displayName": "d"})

        self.assertEqual(captured[0]["method"], "POST")
        self.assertEqual(captured[1]["method"], "PATCH")
        self.assertEqual(captured[2]["method"], "PUT")
        for call in captured:
            self.assertEqual(call["auth"], "Bearer tok")
            self.assertEqual(call["content_type"], "application/json")
            self.assertEqual(call["body"], {"displayName": "d"})


    # c413: execute checkout A's real module from B, patching only the API reader.
    # Keep these in the class exported by the existing backend CI collector.

    def make_checkout(self, parent, label, empty=False):
        root = Path(parent) / ("checkout-" + label)
        (root / "scripts").mkdir(parents=True)
        shutil.copyfile(PATH, root / "scripts/monitoring_apply.py")
        evidence = root / "infra/monitoring/evidence"
        evidence.mkdir(parents=True)
        shutil.copyfile(INVENTORY_PATH, evidence / INVENTORY_PATH.name)
        for kind in ("metrics", "uptime", "policies"):
            (root / "infra/monitoring" / kind).mkdir()
        if not empty:
            metric = json.loads((METRICS_DIR / "sql-pool-capacity-503.json").read_text())
            metric["name"] = "checkout_" + label + "_capacity"
            uptime = json.loads((UPTIME_DIR / "chirp-api-health.json").read_text())
            uptime["displayName"] = "Checkout " + label + " uptime"
            policy = json.loads((POLICIES_DIR / "sql-pool-capacity-503.json").read_text())
            policy["displayName"] = "Checkout " + label + " policy"
            threshold = policy["conditions"][0]["conditionThreshold"]
            threshold["filter"] = threshold["filter"].replace("user/sql_pool_capacity_503", "user/" + metric["name"])
            for kind, body in (("metrics", metric), ("uptime", uptime), ("policies", policy)):
                (root / "infra/monitoring" / kind / "fixture.json").write_text(json.dumps(body))
        return root

    def load_checkout(self, root):
        spec = importlib.util.spec_from_file_location("monitoring_apply_checkout", root / "scripts/monitoring_apply.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def api_calls(self, module, fake):
        def get(url, params):
            try:
                return fake.get(url, params)
            except m.ApplyError as error:
                # Each independently loaded script has its own exception class.
                raise module.ApplyError(str(error)) from None
        return get, fake.post, fake.patch, fake.put

    def directories(self, root):
        return {kind: str((root / "infra/monitoring" / kind).resolve())
                for kind in ("metrics", "uptime", "policies")}

    def arguments(self, apply=False):
        args = ["--project", PROJECT, "--api-host", API_HOST, "--ws-host", WS_HOST]
        if apply:
            args.extend(["--apply", "--channel", "projects/%s/notificationChannels/1" % PROJECT])
        return args

    def test_cross_checkout_defaults_and_explicit_overrides_read_and_write_expected_bodies(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkout_a = self.make_checkout(temporary, "a")
            checkout_b = self.make_checkout(temporary, "b")
            module = self.load_checkout(checkout_a)
            for apply in (False, True):
                for override in (None, "relative", "absolute"):
                    with self.subTest(apply=apply, override=override):
                        fake = FakeMonitoringAPI()
                        args = self.arguments(apply)
                        expected = checkout_a if override is None else checkout_b
                        label = "a" if override is None else "b"
                        if override:
                            for kind, absolute in self.directories(checkout_b).items():
                                directory = "infra/monitoring/" + kind if override == "relative" else absolute
                                args.extend(["--" + kind + "-dir", directory])
                        # Cover both report destinations. A relative report path
                        # retains the usual caller-directory semantics as well.
                        if apply:
                            args.extend(["--report", "report.json"])
                        output = io.StringIO()
                        with chdir(checkout_b), redirect_stdout(output), \
                                patch.object(module, "reader", return_value=self.api_calls(module, fake)):
                            exit_code = module.main(args)
                        report = json.loads((checkout_b / "report.json").read_text() if apply else output.getvalue())
                        self.assertEqual(exit_code, 0)
                        self.assertEqual(report["skipped_files"], [])
                        self.assertEqual(report["input_directories"], self.directories(expected))
                        self.assertEqual([row["name"] for row in report["metrics"]], ["checkout_" + label + "_capacity"])
                        self.assertEqual([row["displayName"] for row in report["uptime"]], ["Checkout " + label + " uptime"])
                        self.assertEqual([row["displayName"] for row in report["policies"]], ["Checkout " + label + " policy"])
                        self.assertEqual(fake.post_calls, 3 if apply else 0)
                        self.assertEqual(fake.patch_calls + fake.put_calls, 0)
                        if apply:
                            self.assertEqual(set(fake.metrics), {"checkout_" + label + "_capacity"})
                            self.assertEqual(set(fake.uptime), {"Checkout " + label + " uptime"})
                            self.assertEqual(set(fake.policies), {"Checkout " + label + " policy"})
                            self.assertFalse((checkout_a / "report.json").exists())

    def test_native_inventory_comes_from_script_checkout_with_conflicting_cwd_inventory(self):
        metric_type = "run.googleapis.com/request_latencies"
        policy = json.loads((POLICIES_DIR / "api-latency-p95.json").read_text())
        with tempfile.TemporaryDirectory() as temporary:
            checkout_a = self.make_checkout(temporary, "a", empty=True)
            checkout_b = self.make_checkout(temporary, "b", empty=True)
            for root, status in ((checkout_a, "available"), (checkout_b, "unavailable")):
                inventory = root / "infra/monitoring/evidence" / INVENTORY_PATH.name
                inventory.write_text(json.dumps({"native_metric_descriptors": {metric_type: status}}))
                (root / "infra/monitoring/policies/fixture.json").write_text(json.dumps(policy))
            for script_root, caller_root, available in (
                    (checkout_a, checkout_b, True), (checkout_b, checkout_a, False)):
                with self.subTest(script_checkout=script_root.name):
                    fake, output = FakeMonitoringAPI(), io.StringIO()
                    # Import after changing directory too: the inventory path
                    # is bound at module initialization, not when main() runs.
                    with chdir(caller_root), redirect_stdout(output):
                        module = self.load_checkout(script_root)
                        with patch.object(module, "reader", return_value=self.api_calls(module, fake)):
                            exit_code = module.main(self.arguments())
                    report = json.loads(output.getvalue())
                    self.assertEqual(report["input_directories"], self.directories(script_root))
                    self.assertEqual(exit_code, 0 if available else 1)
                    if available:
                        self.assertEqual(report["skipped_files"], [])
                        self.assertEqual([row["displayName"] for row in report["policies"]], [policy["displayName"]])
                        self.assertEqual(report["policies"][0]["action"], "create")
                    else:
                        self.assertEqual(report["policies"], [])
                        self.assertEqual(len(report["skipped_files"]), 1)
                        self.assertIn("metric_not_in_inventory:" + metric_type, json.dumps(report["skipped_files"]))
                    self.assertEqual((fake.post_calls, fake.patch_calls, fake.put_calls), (0, 0, 0))

    def test_input_directories_survive_auth_inventory_and_partial_apply_failures(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkout_a = self.make_checkout(temporary, "a")
            checkout_b = self.make_checkout(temporary, "b")
            module = self.load_checkout(checkout_a)
            for failure in ("authentication", "inventory", "uptime_write"):
                with self.subTest(failure=failure):
                    fake = FakeMonitoringAPI()
                    get, post, patch_fn, put = self.api_calls(module, fake)

                    def failing_get(url, params):
                        raise module.ApplyError("http_403")

                    def failing_post(url, body):
                        if url.endswith("uptimeCheckConfigs"):
                            raise module.ApplyError("http_400")
                        return post(url, body)

                    options = {"side_effect": module.ApplyError("authentication_unavailable")} if failure == "authentication" else {
                        "return_value": (failing_get if failure == "inventory" else get,
                                         failing_post if failure == "uptime_write" else post, patch_fn, put)}
                    args = self.arguments(apply=True) + ["--report", "failure.json"]
                    with chdir(checkout_b), patch.object(module, "reader", **options):
                        exit_code = module.main(args)
                    report = json.loads((checkout_b / "failure.json").read_text())
                    self.assertEqual(exit_code, 2)
                    self.assertEqual(report["input_directories"], self.directories(checkout_a))
                    if failure == "uptime_write":
                        self.assertEqual([row["name"] for row in report["metrics"]], ["checkout_a_capacity"])
                        self.assertEqual(report["failed_operation"]["kind"], "uptime")
                        self.assertEqual(fake.post_calls, 1)
                    else:
                        self.assertEqual(fake.post_calls + fake.patch_calls + fake.put_calls, 0)

    def test_post_parse_validation_reports_the_checkout_it_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkout_a = self.make_checkout(temporary, "a")
            checkout_b = self.make_checkout(temporary, "b", empty=True)
            module = self.load_checkout(checkout_a)
            for args in (["--project", PROJECT], self.arguments() + ["--apply"]):
                with self.subTest(args=args):
                    output = io.StringIO()
                    with chdir(checkout_b), redirect_stdout(output), patch.object(module, "reader") as reader:
                        with self.assertRaises(SystemExit) as caught:
                            module.main(args)
                    self.assertEqual(caught.exception.code, 2)
                    reader.assert_not_called()
                    report = json.loads(output.getvalue())
                    self.assertEqual(report["reason"], "invalid_arguments")
                    self.assertEqual(report["input_directories"], self.directories(checkout_a))

    def test_empty_directories_do_not_fall_back_to_another_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkout_a = self.make_checkout(temporary, "a", empty=True)
            checkout_b = self.make_checkout(temporary, "b")
            module = self.load_checkout(checkout_a)
            empty_overrides = checkout_b / "empty"
            empty_overrides.mkdir()
            for apply in (False, True):
                for explicit_empty in (False, True):
                    with self.subTest(apply=apply, explicit_empty=explicit_empty):
                        fake = FakeMonitoringAPI()
                        args = ["--project", PROJECT]
                        if apply:
                            args.extend(["--apply", "--channel", "projects/%s/notificationChannels/1" % PROJECT])
                        expected = self.directories(checkout_a)
                        if explicit_empty:
                            for kind in expected:
                                args.extend(["--" + kind + "-dir", "empty"])
                            expected = dict.fromkeys(expected, str(empty_overrides.resolve()))
                        output = io.StringIO()
                        with chdir(checkout_b), redirect_stdout(output), \
                                patch.object(module, "reader", return_value=self.api_calls(module, fake)):
                            exit_code = module.main(args)
                        report = json.loads(output.getvalue())
                        self.assertEqual(exit_code, 0)
                        self.assertEqual(report["input_directories"], expected)
                        self.assertEqual(report["skipped_files"], [])
                        for kind in expected:
                            self.assertEqual(report[kind], [])
                        self.assertEqual(fake.post_calls + fake.patch_calls + fake.put_calls, 0)


if __name__ == "__main__":
    unittest.main()
