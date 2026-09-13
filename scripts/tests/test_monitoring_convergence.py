"""c408: REST-shaped round trips and recoverable apply failures, without network."""
from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.parse
from unittest.mock import patch

_spec = importlib.util.spec_from_file_location(
    "c408_apply_fixtures", Path(__file__).with_name("test_monitoring_apply.py"))
fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixtures)
m = fixtures.m
KINDS = ("metrics", "uptime", "policies")
CHANNEL = "projects/sample-project/notificationChannels/1"


class RoundTripAPI(fixtures.FakeMonitoringAPI):
    """Model server-generated identities, protobuf omissions, and masked PATCHes.

    This deliberately differs from storing the submitted JSON verbatim. Defaults
    follow the REST references linked in MONITORING-RUNBOOK.md, including enabled
    on policies, STATIC_IP_CHECKERS, and output-only LogMetric descriptor fields.
    Unknown/nondefault settings are preserved, so a too-broad normalizer fails
    the drift tests. This fake does not establish that a policy is live-API valid.
    """

    def __init__(self):
        super().__init__()
        self.requests = []
        self.fail_at = None
        self._condition_id = 100

    def _before_write(self, method, url, body):
        self.requests.append((method, url, deepcopy(body)))
        if self.fail_at == len(self.requests):
            raise m.ApplyError("http_400")

    def _shape(self, body, kind):
        body = deepcopy(body)
        if kind == "metrics":
            body["resourceName"] = "projects/sample-project/metrics/" + body["name"]
            body["createTime"] = "2026-09-13T00:00:00Z"
            body["updateTime"] = "2026-09-13T01:00:00Z"
            body["version"] = "V2"
            if body.get("disabled") is False:
                del body["disabled"]
            descriptor = body["metricDescriptor"]
            descriptor.update(name="generated/descriptor/" + body["name"],
                              type="logging.googleapis.com/user/" + body["name"],
                              description=body["description"],
                              monitoredResourceTypes=["cloud_run_revision"])
            descriptor.setdefault("labels", [])
            for label in descriptor["labels"]:
                if label.get("valueType") == "STRING":
                    del label["valueType"]
            body.setdefault("labelExtractors", {})
        elif kind == "uptime":
            body.setdefault("checkerType", "STATIC_IP_CHECKERS")
            for key, default in {"isInternal": False, "disabled": False,
                                 "logCheckFailures": False, "internalCheckers": [],
                                 "userLabels": {}}.items():
                body.setdefault(key, default)
            for key, default in {"maskHeaders": False, "headers": {}, "authInfo": {},
                                 "contentType": "TYPE_UNSPECIFIED", "body": ""}.items():
                body["httpCheck"].setdefault(key, default)
            body["httpCheck"].setdefault("acceptedResponseStatusCodes",
                                         [{"statusClass": "STATUS_CLASS_2XX"}])
        else:
            body.setdefault("enabled", True)
            body["creationRecord"] = {"mutateTime": "2026-09-13T00:00:00Z"}
            body["mutationRecord"] = {"mutateTime": "2026-09-13T01:00:00Z"}
            body.setdefault("userLabels", {})
            for condition in body["conditions"]:
                if "name" not in condition:
                    self._condition_id += 1
                    condition["name"] = body["name"] + "/conditions/" + str(self._condition_id)
                for key in ("conditionThreshold", "conditionAbsent"):
                    spec = condition.get(key, {})
                    if spec.get("thresholdValue") == 0:
                        del spec["thresholdValue"]
                    for aggregation_key in ("aggregations", "denominatorAggregations"):
                        for aggregation in spec.get(aggregation_key, []):
                            if aggregation.get("groupByFields") == []:
                                del aggregation["groupByFields"]
        return body

    def post(self, url, body):
        self._before_write("POST", url, body)
        kind = ("metrics" if "logging.googleapis.com" in url else
                "uptime" if url.endswith("uptimeCheckConfigs") else "policies")
        if kind != "metrics":
            assert "name" not in body, "server identity must not be sent on create"
        if kind == "policies":
            assert all("name" not in condition for condition in body["conditions"])
        response = self._shape(super().post(url, body), kind)
        identity = body["name"] if kind == "metrics" else body["displayName"]
        getattr(self, kind)[identity] = response
        return deepcopy(response)

    def patch(self, url, body):
        self._before_write("PATCH", url, body)
        resource_name = url.split("?", 1)[0].split("/v3/", 1)[1]
        assert body.get("name") == resource_name, "PATCH requires the existing resource name"
        kind = "uptime" if "/uptimeCheckConfigs/" in resource_name else "policies"
        store = getattr(self, kind)
        previous = next(item for item in store.values() if item["name"] == resource_name)
        if kind == "policies":
            condition_names = {item["displayName"]: item["name"] for item in previous["conditions"]}
            for condition in body["conditions"]:
                if condition["displayName"] in condition_names:
                    assert condition.get("name") == condition_names[condition["displayName"]], \
                        "existing condition identities must survive PATCH"
                else:
                    assert "name" not in condition, "new conditions need a new server identity"
        mask = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["updateMask"][0].split(",")
        owned = m.UPTIME_UPDATE_MASK_FIELDS if kind == "uptime" else m.UPDATE_MASK_FIELDS
        assert mask and set(mask) <= set(owned)
        updated = deepcopy(previous)
        for field in mask:
            if field in body:
                updated[field] = deepcopy(body[field])
            else:
                updated.pop(field, None)
        updated = self._shape(updated, kind)
        store[body["displayName"]] = updated
        self.patch_calls += 1
        return deepcopy(updated)

    def put(self, url, body):
        self._before_write("PUT", url, body)
        assert "updateMask" not in url
        response = self._shape(super().put(url, body), "metrics")
        self.metrics[body["name"]] = response
        return deepcopy(response)


@contextmanager
def edited_files(directory, edit):
    with tempfile.TemporaryDirectory() as temporary:
        destination = Path(temporary)
        for source in sorted(directory.glob("*.json")):
            body = json.loads(source.read_text())
            edit(body)
            (destination / source.name).write_text(json.dumps(body), encoding="utf-8")
        yield str(destination)


class MonitoringConvergenceTests(unittest.TestCase):
    def run_apply(self, fake, **overrides):
        args = fixtures._args(apply=True, channel=CHANNEL, **overrides)
        return m.run(args, fake.get, fake.post, fake.patch, fake.put)

    def all_results(self, report):
        return [entry for kind in KINDS for entry in report[kind]]

    def test_real_files_second_apply_is_all_noop_with_server_fields_and_defaults(self):
        fake = RoundTripAPI()
        first = self.run_apply(fake)
        self.assertEqual(first["exit_code"], 0)
        self.assertTrue(all(first[kind] for kind in KINDS))
        original_remote = deepcopy((fake.metrics, fake.uptime, fake.policies))
        second = m.run(fixtures._args(apply=True, channel=CHANNEL), fake.get,
                       fixtures._refusing, fixtures._refusing, fixtures._refusing)
        self.assertEqual(second["exit_code"], 0)
        self.assertEqual({item["action"] for item in self.all_results(second)}, {"noop"})
        self.assertEqual((fake.metrics, fake.uptime, fake.policies), original_remote,
                         "normalization must not mutate the observed inventory")

    def test_dry_run_remains_read_only_and_ignores_only_unsupplied_channels(self):
        fake = RoundTripAPI()
        self.run_apply(fake)
        for policy in fake.policies.values():
            policy["notificationChannels"] = ["projects/sample-project/notificationChannels/other"]
        report = m.run(fixtures._args(), fake.get, fixtures._refusing,
                       fixtures._refusing, fixtures._refusing)
        self.assertEqual({entry["action"] for entry in self.all_results(report)}, {"noop"})
        report = self.run_apply(fake)
        self.assertEqual({entry["action"] for entry in report["policies"]}, {"update"})

    def test_changed_reordered_conditions_patch_once_and_keep_names_by_display_name(self):
        fake = RoundTripAPI()
        self.run_apply(fake)
        target = next(policy for policy in fake.policies.values()
                      if len(policy["conditions"]) > 1
                      and all("conditionThreshold" in c for c in policy["conditions"]))
        identities = {condition["displayName"]: condition["name"] for condition in target["conditions"]}
        fake.requests.clear()

        def edit(body):
            if body["displayName"] == target["displayName"]:
                body["conditions"].reverse()
                body["conditions"][0]["conditionThreshold"]["thresholdValue"] = 0.97

        with edited_files(fixtures.POLICIES_DIR, edit) as directory:
            report = self.run_apply(fake, policies_dir=directory)
            self.assertEqual(len(fake.requests), 1)
            method, url, body = fake.requests[0]
            self.assertEqual(method, "PATCH")
            self.assertIn(target["name"], url)
            self.assertEqual(body["name"], target["name"])
            self.assertEqual({c["displayName"]: c["name"] for c in body["conditions"]}, identities)
            self.assertEqual(body["conditions"][0]["conditionThreshold"]["thresholdValue"], 0.97)
            self.assertEqual(sum(e["action"] == "update" for e in report["policies"]), 1)
            fake.requests.clear()
            self.run_apply(fake, policies_dir=directory)
            self.assertEqual(fake.requests, [])

    def test_new_condition_omits_identity_and_retained_condition_keeps_its_identity(self):
        fake = RoundTripAPI()
        self.run_apply(fake)
        target = next(policy for policy in fake.policies.values() if len(policy["conditions"]) == 1)
        old_name = target["conditions"][0]["name"]

        def edit(body):
            if body["displayName"] == target["displayName"]:
                extra = deepcopy(body["conditions"][0])
                extra["displayName"] += " new condition"
                body["conditions"].append(extra)

        with edited_files(fixtures.POLICIES_DIR, edit) as directory:
            fake.requests.clear()
            self.run_apply(fake, policies_dir=directory)
            sent = fake.requests[0][2]["conditions"]
            self.assertEqual(sent[0]["name"], old_name)
            self.assertNotIn("name", sent[1])
            fake.requests.clear()
            self.run_apply(fake, policies_dir=directory)
            self.assertEqual(fake.requests, [])

    def test_enabled_default_is_write_only_and_explicit_false_is_preserved(self):
        for desired_enabled in (None, False):
            for observed_enabled in (None, True, False):
                with self.subTest(desired=desired_enabled, observed=observed_enabled):
                    fake = RoundTripAPI()
                    self.run_apply(fake)
                    target = next(iter(fake.policies))

                    def edit(body):
                        if body["displayName"] == target:
                            if desired_enabled is None:
                                body.pop("enabled")
                            else:
                                body["enabled"] = desired_enabled

                    if observed_enabled is None:
                        fake.policies[target].pop("enabled")
                    else:
                        fake.policies[target]["enabled"] = observed_enabled
                    fake.requests.clear()
                    expected = True if desired_enabled is None else desired_enabled
                    with edited_files(fixtures.POLICIES_DIR, edit) as directory:
                        report = self.run_apply(fake, policies_dir=directory)
                        entry = next(e for e in report["policies"] if e["displayName"] == target)
                        self.assertEqual(entry["action"], "noop" if observed_enabled is expected else "update")
                        self.assertIs(fake.policies[target]["enabled"], expected)

    def test_nondefault_and_unrecognized_settings_remain_drift(self):
        cases = (
            ("metrics", lambda b: b.update(bucketName="projects/sample-project/locations/global/buckets/other")),
            ("metrics", lambda b: b.update(disabled=True)),
            ("metrics", lambda b: b["metricDescriptor"].update(unrecognized="changed")),
            ("uptime", lambda b: b.update(disabled=True)),
            ("uptime", lambda b: b.update(checkerType="VPC_CHECKERS")),
            ("uptime", lambda b: b["httpCheck"].update(validateSsl=False)),
            ("uptime", lambda b: b["httpCheck"].update(headers={"X-Test": "changed"})),
            ("policies", lambda b: b.update(userLabels={"owner": "other"})),
            ("policies", lambda b: b.update(validity={"code": 3})),
            ("policies", lambda b: b["conditions"][0]["conditionThreshold"].update(thresholdValue=999)),
        )
        for kind, change in cases:
            with self.subTest(kind=kind, change=change):
                fake = RoundTripAPI()
                self.run_apply(fake)
                target = next(iter(getattr(fake, kind).values()))
                change(target)
                report = m.run(fixtures._args(), fake.get, fixtures._refusing,
                               fixtures._refusing, fixtures._refusing)
                self.assertEqual(sum(entry["action"] == "update" for entry in report[kind]), 1)

    def test_metric_filter_update_and_uptime_period_update_use_existing_identity(self):
        for kind in ("metrics", "uptime"):
            with self.subTest(kind=kind):
                fake = RoundTripAPI()
                self.run_apply(fake)
                target = next(iter(getattr(fake, kind).values()))
                if kind == "metrics":
                    target["filter"] = 'resource.type="wrong_resource"'
                else:
                    target["period"] = "300s"
                fake.requests.clear()
                self.run_apply(fake)
                self.assertEqual(len(fake.requests), 1)
                method, url, body = fake.requests[0]
                self.assertEqual(method, "PUT" if kind == "metrics" else "PATCH")
                self.assertEqual(body["name"], target["name"])
                if kind == "metrics":
                    self.assertNotIn("updateMask", url)
                fake.requests.clear()
                self.run_apply(fake)
                self.assertEqual(fake.requests, [])

    def test_unmanaged_drift_is_named_in_plan_and_blocks_apply_before_all_writes(self):
        for kind, key, value in (
            ("metrics", "bucketName", "projects/sample-project/locations/global/buckets/other"),
            ("metrics", "unrecognized", None),
            ("uptime", "disabled", True),
            ("uptime", "userLabels", {"owner": "manual"}),
            ("policies", "userLabels", {"owner": "manual"}),
            ("policies", "validity", {"code": 3}),
        ):
            with self.subTest(kind=kind, field=key):
                fake = RoundTripAPI()
                self.run_apply(fake)
                target = list(getattr(fake, kind).values())[-1]
                target[key] = value
                fake.metrics.pop(next(iter(fake.metrics)))  # An earlier create must not happen.
                fake.requests.clear()
                plan = m.run(fixtures._args(), fake.get, fixtures._refusing,
                             fixtures._refusing, fixtures._refusing)
                drift = next(e for e in plan[kind] if e.get("unsupported_fields"))
                self.assertEqual(drift["action"], "update")
                self.assertEqual(drift["unsupported_fields"], [key])
                with self.assertRaisesRegex(m.ApplyError, "unsupported_configuration_drift"):
                    self.run_apply(fake)
                self.assertEqual(fake.requests, [])
                self.assertEqual(target[key], value, "manual configuration must remain untouched")

    def test_removed_owned_policy_field_is_in_update_mask_and_cleared(self):
        fake = RoundTripAPI()
        self.run_apply(fake)
        target = next(iter(fake.policies.values()))
        # c407 removes every local alertStrategy; this fixture remains useful on
        # either side of that merge and proves omission clears an owned field.
        target["alertStrategy"] = {"autoClose": "3600s"}

        def edit(body):
            if body["displayName"] == target["displayName"]:
                body.pop("alertStrategy", None)

        with edited_files(fixtures.POLICIES_DIR, edit) as directory:
            fake.requests.clear()
            self.run_apply(fake, policies_dir=directory)
            self.assertIn("alertStrategy", fake.requests[0][1])
            self.assertNotIn("alertStrategy", fake.requests[0][2])
            fake.requests.clear()
            self.run_apply(fake, policies_dir=directory)
            self.assertEqual(fake.requests, [])

    def test_failure_on_each_write_retains_all_prior_successes_and_retry_converges(self):
        plan = m.run(fixtures._args(), fixtures._no_op_get)
        count = len(self.all_results(plan))
        for successful_count in range(count):
            with self.subTest(successful_count=successful_count):
                fake = RoundTripAPI()
                fake.fail_at = successful_count + 1
                report = self.run_apply(fake)
                self.assertEqual(report["exit_code"], 2)
                self.assertEqual(report["status"], "error")
                self.assertEqual(report["reason"], "http_400")
                self.assertEqual(len(self.all_results(report)), successful_count)
                self.assertEqual({e["action"] for e in self.all_results(report)} - {"create"}, set())
                self.assertTrue(all(e["resource_name"] for e in self.all_results(report)))
                failed = report["failed_operation"]
                expected_failed = self.all_results(plan)[successful_count]
                self.assertEqual(failed["action"], expected_failed["action"])
                self.assertEqual(failed.get("displayName", failed.get("name")),
                                 expected_failed.get("displayName", expected_failed.get("name")))
                self.assertEqual(failed["outcome"], "unconfirmed")
                self.assertEqual(sum(map(len, report["pending"].values())), count - successful_count - 1)
                self.assertEqual(len(fake.requests), successful_count + 1, "stop at the first failed write")
                fake.fail_at = None
                retry = self.run_apply(fake)
                self.assertEqual(retry["exit_code"], 0)
                self.assertEqual(sum(e["action"] == "noop" for e in self.all_results(retry)), successful_count)
                self.assertEqual(sum(len(getattr(fake, kind)) for kind in KINDS), count)
                fake.requests.clear()
                self.run_apply(fake)
                self.assertEqual(fake.requests, [])

    def test_failed_patch_retains_updates_from_previous_phases(self):
        fake = RoundTripAPI()
        first = self.run_apply(fake)
        for metric in fake.metrics.values():
            metric["filter"] = "wrong"
        for uptime in fake.uptime.values():
            uptime["period"] = "300s"
        for policy in fake.policies.values():
            policy["documentation"]["content"] = "outdated"
        fake.requests.clear()
        successes = len(first["metrics"]) + len(first["uptime"]) + 1
        fake.fail_at = successes + 1
        report = self.run_apply(fake)
        self.assertEqual(len(self.all_results(report)), successes)
        self.assertEqual({entry["action"] for entry in self.all_results(report)}, {"update"})
        self.assertEqual(report["failed_operation"]["kind"], "policies")
        self.assertEqual(report["failed_operation"]["action"], "update")
        self.assertEqual(fake.requests[-1][0], "PATCH")

    def test_failure_after_preexisting_noops_counts_acknowledged_writes_separately(self):
        metric_count = len(m.run(fixtures._args(), fixtures._no_op_get)["metrics"])
        fake = RoundTripAPI()
        fake.fail_at = metric_count
        self.run_apply(fake)  # All but the last metric now exist and will be noops.
        fake.requests.clear()
        fake.fail_at = 3
        report = self.run_apply(fake)
        results = self.all_results(report)
        self.assertEqual(sum(e["action"] == "noop" for e in results), metric_count - 1)
        self.assertEqual(sum(e["action"] == "create" for e in results), 2)
        self.assertEqual(report["failed_operation"]["kind"], "uptime")
        self.assertEqual(len(report["policies"]), 0)
        self.assertEqual(len(fake.requests), 3)

    def test_cli_preserves_partial_report_in_stdout_and_file(self):
        for report_to_file in (False, True):
            with self.subTest(report_to_file=report_to_file), tempfile.TemporaryDirectory() as temporary:
                fake = RoundTripAPI()
                fake.fail_at = 3
                destination = str(Path(temporary) / "report.json") if report_to_file else None
                args = fixtures._args(apply=True, channel=CHANNEL, report=destination)
                output = io.StringIO()
                with patch.object(m, "parse_arguments", return_value=args), \
                     patch.object(m, "reader", return_value=(fake.get, fake.post, fake.patch, fake.put)), \
                     redirect_stdout(output):
                    exit_code = m.main([])
                report = json.loads(Path(destination).read_text() if destination else output.getvalue())
                self.assertEqual(exit_code, 2)
                self.assertEqual(len(report["metrics"]), 2)
                self.assertEqual(report["failed_operation"]["kind"], "metrics")
                self.assertNotIn("filter", json.dumps(report))
                self.assertNotIn("documentation", json.dumps(report))

    def test_lost_write_response_is_unconfirmed_and_retry_reinventories(self):
        for failure in ("transport", "missing_identity"):
            with self.subTest(failure=failure):
                fake = RoundTripAPI()

                def write_then_fail(url, body):
                    response = fake.post(url, body)
                    if len(fake.requests) == 2:
                        if failure == "transport":
                            raise m.ApplyError("transport_or_trust_unavailable")
                        return {}
                    return response

                args = fixtures._args(apply=True, channel=CHANNEL)
                report = m.run(args, fake.get, write_then_fail, fake.patch, fake.put)
                self.assertEqual(report["exit_code"], 2)
                self.assertEqual(len(report["metrics"]), 1)
                self.assertEqual(report["failed_operation"]["outcome"], "unconfirmed")
                self.assertEqual(len(fake.metrics), 2, "the unacknowledged write actually committed")
                retry = self.run_apply(fake)
                self.assertEqual([e["action"] for e in retry["metrics"][:2]], ["noop", "noop"])

    def test_duplicate_local_resource_identity_fails_before_writes(self):
        for kind, directory in (("metrics", fixtures.METRICS_DIR),
                                ("uptime", fixtures.UPTIME_DIR),
                                ("policies", fixtures.POLICIES_DIR)):
            with self.subTest(kind=kind), edited_files(directory, lambda body: None) as temporary:
                first = next(Path(temporary).glob("*.json"))
                (Path(temporary) / "duplicate.json").write_text(first.read_text())
                fake = RoundTripAPI()
                with self.assertRaisesRegex(m.ApplyError, "ambiguous_resource_identity"):
                    self.run_apply(fake, **{kind + "_dir": temporary})
                self.assertEqual(fake.requests, [])

    def test_duplicate_remote_resource_identity_fails_before_writes(self):
        for kind, key in (("uptime", "uptimeCheckConfigs"), ("policies", "alertPolicies")):
            with self.subTest(kind=kind):
                fake = RoundTripAPI()
                self.run_apply(fake)
                # Leave earlier phases needing creates to prove this is preflight.
                fake.metrics.clear()
                fake.requests.clear()

                def get(url, params):
                    response = fake.get(url, params)
                    if url.endswith(key):
                        duplicate = deepcopy(response[key][0])
                        duplicate["name"] += "-duplicate"
                        response[key].append(duplicate)
                    return response

                with self.assertRaisesRegex(m.ApplyError, "ambiguous_resource_identity"):
                    m.run(fixtures._args(apply=True, channel=CHANNEL), get, fake.post, fake.patch, fake.put)
                self.assertEqual(fake.requests, [])

    def test_ambiguous_or_missing_condition_identity_fails_before_any_writes(self):
        for problem in ("duplicate_display", "duplicate_id", "missing_id", "foreign_id"):
            with self.subTest(problem=problem):
                fake = RoundTripAPI()
                self.run_apply(fake)
                target = next(p for p in fake.policies.values() if len(p["conditions"]) > 1)
                first, second = target["conditions"][:2]
                if problem == "duplicate_display":
                    second["displayName"] = first["displayName"]
                elif problem == "duplicate_id":
                    second["name"] = first["name"]
                elif problem == "missing_id":
                    first.pop("name")
                else:
                    first["name"] = "projects/other/alertPolicies/1/conditions/1"
                fake.metrics.clear()
                fake.requests.clear()
                with self.assertRaises(m.ApplyError):
                    self.run_apply(fake)
                self.assertEqual(fake.requests, [])

    def test_duplicate_local_condition_display_name_fails_before_any_writes(self):
        def edit(body):
            body["conditions"].append(deepcopy(body["conditions"][0]))

        with edited_files(fixtures.POLICIES_DIR, edit) as directory:
            fake = RoundTripAPI()
            with self.assertRaisesRegex(m.ApplyError, "ambiguous_resource_identity"):
                self.run_apply(fake, policies_dir=directory)
            self.assertEqual(fake.requests, [])


if __name__ == "__main__":
    unittest.main()
