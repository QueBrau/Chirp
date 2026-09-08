"""Projected collector contracts using fixtures only; no network or credentials."""
import importlib.util
import contextlib
import io
import json
from pathlib import Path
import ssl
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error

PATH = Path(__file__).resolve().parents[1] / "monitoring_check.py"
spec = importlib.util.spec_from_file_location("monitoring_check", PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
SECRET = "private-recipient-or-token"


def fixture_get(url, params):
    if url.endswith("metricDescriptors"):
        prefix = params["filter"].split('"')[1]
        return {"metricDescriptors": [
            {"type": kind, "metricKind": value[0], "valueType": value[1]}
            for kind, value in m.METRICS.items() if kind.startswith(prefix)]}
    return {}


class MonitoringCheckTests(unittest.TestCase):
    def test_empty_inventory_is_gap_and_not_delivered_or_covered(self):
        report = m.collect("sample-project", fixture_get)
        self.assertEqual(report["exit_code"], 1)
        self.assertEqual(len(report["configuration_gaps"]), 3)
        self.assertFalse(report["operational_acceptance"])
        self.assertEqual(report["alert_delivery_and_responders"], "not_verified")
        self.assertEqual(report["time_series_and_signal_coverage"], "not_verified")
        self.assertEqual(set(report["native_metric_descriptors"].values()), {"available"})

    def test_populated_metadata_cannot_claim_delivery_and_never_echoes_fields(self):
        def get(url, params):
            if url.endswith("alertPolicies"):
                self.assertNotIn("conditions", params["fields"])
                return {"alertPolicies": [{"name": SECRET, "enabled": True,
                    "notificationChannels": [SECRET], "conditions": [SECRET]}]}
            if url.endswith("notificationChannels"):
                self.assertNotIn("labels", params["fields"])
                return {"notificationChannels": [{"name": SECRET, "enabled": True,
                    "verificationStatus": "VERIFIED", "labels": {"email_address": SECRET}}]}
            if url.endswith("uptimeCheckConfigs"):
                return {"uptimeCheckConfigs": [{"name": SECRET, "httpCheck": {"authInfo": SECRET}}]}
            return fixture_get(url, params)
        report = m.collect("sample-project", get)
        self.assertEqual(report["exit_code"], 0)
        self.assertFalse(report["operational_acceptance"])
        self.assertNotIn(SECRET, json.dumps(report))

    def test_denied_is_not_empty_or_missing(self):
        def get(url, params):
            if url.endswith("alertPolicies"):
                raise m.CheckError("http_403")
            return fixture_get(url, params)
        report = m.collect("sample-project", get)
        self.assertEqual(report["exit_code"], 2)
        self.assertEqual(report["collections"]["alerts"], {"status": "error", "reason": "http_403"})
        self.assertNotIn("alerts_none_enabled_or_configured", report["configuration_gaps"])

    def test_pagination_visits_later_configuration(self):
        calls = []
        def get(url, params):
            if not url.endswith("alertPolicies"):
                return fixture_get(url, params)
            calls.append(params.copy())
            if "pageToken" not in params:
                return {"nextPageToken": SECRET}
            return {"alertPolicies": [{"name": SECRET, "enabled": True}]}
        report = m.collect("sample-project", get)
        self.assertEqual(report["collections"]["alerts"]["enabled_count"], 1)
        self.assertEqual(len(calls), 2)
        self.assertNotIn(SECRET, json.dumps(report))

    def test_pagination_cycle_and_cap_fail_without_partial_absence(self):
        for cycle in (False, True):
            calls = []
            def get(url, params):
                if not url.endswith("alertPolicies"):
                    return fixture_get(url, params)
                calls.append(1)
                return {"nextPageToken": SECRET if cycle else str(len(calls))}
            report = m.collect("sample-project", get)
            self.assertEqual(report["collections"]["alerts"]["status"], "error")
            self.assertNotIn("count", report["collections"]["alerts"])
            self.assertEqual(len(calls), 2 if cycle else m.MAX_PAGES)

    def test_invalid_schemas_are_not_empty_success(self):
        for body in ({"error": SECRET}, {"alertPolicies": None},
                     {"alertPolicies": [{"name": SECRET, "enabled": "false"}]},
                     {"alertPolicies": [{"name": SECRET, "notificationChannels": SECRET}]},
                     {"alertPolicies": [SECRET]}, {"nextPageToken": []}):
            with self.subTest(body=body):
                def get(url, params):
                    return body if url.endswith("alertPolicies") else fixture_get(url, params)
                report = m.collect("sample-project", get)
                self.assertEqual(report["exit_code"], 2)
                self.assertNotIn(SECRET, json.dumps(report))

    def test_descriptor_absence_and_contract_drift_remain_distinct(self):
        def get(url, params):
            result = fixture_get(url, params)
            rows = result.get("metricDescriptors", [])
            if rows:
                rows.pop()
                rows[0]["valueType"] = "STRING"
            return result
        report = m.collect("sample-project", get)
        self.assertIn("contract_mismatch", report["native_metric_descriptors"].values())
        self.assertIn("not_listed", report["native_metric_descriptors"].values())

    def test_native_metrics_query_failure_not_descriptor_absence(self):
        def get(url, params):
            if url.endswith("metricDescriptors"):
                raise m.CheckError("http_403")
            return fixture_get(url, params)
        report = m.collect("sample-project", get)
        self.assertEqual(report["exit_code"], 2)
        self.assertEqual(set(report["native_metric_descriptors"].values()), {"query_error"})

    def test_token_in_header_only_get_and_tls_verification(self):
        args = m.parse_arguments(["--project", "sample-project"])
        captured = {}
        class Opener:
            def open(self, request, timeout):
                captured.update(method=request.method, url=request.full_url,
                                auth=request.get_header("Authorization"), timeout=timeout)
                return io.BytesIO(b'{}')
        def build(*handlers):
            context = handlers[1]._context
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(context.check_hostname)
            self.assertIsInstance(handlers[0], m.NoRedirect)
            return Opener()
        with patch.object(m.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=SECRET)) as run:
            with patch.object(m.urllib.request, "build_opener", side_effect=build):
                get = m.reader(args)
                self.assertEqual(get("https://monitoring.googleapis.com/v3/projects/sample-project/alertPolicies", {}), {})
        self.assertEqual(captured["method"], "GET")
        self.assertEqual(captured["auth"], "Bearer " + SECRET)
        self.assertNotIn(SECRET, captured["url"])
        self.assertNotIn(SECRET, str(run.call_args))
        self.assertEqual(run.call_args.kwargs["env"]["CLOUDSDK_CORE_DISABLE_FILE_LOGGING"], "1")
        self.assertIsNone(m.NoRedirect().redirect_request(None, None, 302, SECRET, {}, "https://other.invalid"))

    def test_transport_errors_and_invalid_json_are_content_free(self):
        args = m.parse_arguments(["--project", "sample-project"])
        cases = [urllib.error.HTTPError("https://example", 403, SECRET, {}, io.BytesIO(SECRET.encode())),
                 urllib.error.URLError(SECRET), b'{"token":"'+SECRET.encode()+b'",',
                 b'{"x":1,"x":2}', b'{"x":NaN}', b'null', b'x' * (m.MAX_BODY_BYTES + 1)]
        for value in cases:
            class Opener:
                def open(self, *args, **kwargs):
                    if isinstance(value, Exception): raise value
                    return io.BytesIO(value)
            with patch.object(m.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=SECRET)):
                with patch.object(m.urllib.request, "build_opener", return_value=Opener()):
                    get = m.reader(args)
                    with self.assertRaises(m.CheckError) as caught:
                        get("https://monitoring.googleapis.com/v3/projects/sample-project/alertPolicies", {})
                    self.assertNotIn(SECRET, str(caught.exception))

    def test_cli_invalid_input_does_not_echo_or_authenticate(self):
        for args in (["--project", SECRET+"/invalid"], ["--project", "sample-project", "--token", SECRET],
                     ["--project", "sample-project", "--timeout-seconds", "nan"]):
            result = subprocess.run([sys.executable, str(PATH), *args], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn(SECRET, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["reason"], "invalid_arguments")

    def test_sdk_failures_are_redacted_and_do_not_start_network(self):
        args = m.parse_arguments(["--project", "sample-project"])
        for result in (SimpleNamespace(returncode=1, stdout=SECRET, stderr=SECRET),
                       SimpleNamespace(returncode=0, stdout=SECRET+"\n"+SECRET),
                       subprocess.TimeoutExpired(SECRET, 1, output=SECRET, stderr=SECRET),
                       OSError(SECRET)):
            with patch.object(m.urllib.request, "build_opener") as opener:
                with patch.object(m.subprocess, "run") as run:
                    if isinstance(result, Exception): run.side_effect = result
                    else: run.return_value = result
                    with self.assertRaises(m.CheckError) as caught:
                        m.reader(args)
                    self.assertNotIn(SECRET, str(caught.exception))
                    opener.assert_not_called()

    def test_untrusted_ca_failure_is_redacted_without_auth_attempt(self):
        args = m.parse_arguments(["--project", "sample-project", "--ca-file", "/nonexistent/"+SECRET])
        with patch.object(m.subprocess, "run") as run:
            with self.assertRaises(m.CheckError) as caught:
                m.reader(args)
            self.assertNotIn(SECRET, str(caught.exception))
            run.assert_not_called()

    def test_omitted_enabled_is_unknown_not_disabled(self):
        for collection, key in (("alertPolicies", "alerts"), ("notificationChannels", "channels")):
            def get(url, params):
                if url.endswith(collection):
                    return {collection: [{"name": SECRET}]}
                return fixture_get(url, params)
            report = m.collect("sample-project", get)
            self.assertEqual(report["exit_code"], 2)
            self.assertEqual(report["collections"][key]["status"], "error")
            self.assertNotIn(key+"_none_enabled_or_configured", report["configuration_gaps"])
            self.assertNotIn(SECRET, json.dumps(report))

    def test_nonstring_verification_is_structured_cli_error(self):
        for verification in ([SECRET], {"recipient": SECRET}):
            def get(url, params):
                if url.endswith("notificationChannels"):
                    return {"notificationChannels": [{"name": SECRET, "enabled": True,
                                                       "verificationStatus": verification}]}
                return fixture_get(url, params)
            output = io.StringIO()
            with patch.object(m, "reader", return_value=get), contextlib.redirect_stdout(output):
                code = m.main(["--project", "sample-project"])
            self.assertEqual(code, 2)
            report = json.loads(output.getvalue())
            self.assertEqual(report["collections"]["channels"], {"status": "error", "reason": "invalid_schema"})
            self.assertNotIn(SECRET, output.getvalue())


if __name__ == "__main__":
    unittest.main()
