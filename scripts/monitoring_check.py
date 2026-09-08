"""Read a projected Monitoring inventory. No policy, channel, or resource writes.

Only aggregate configuration counts and a fixed native metric vocabulary leave
this process. No recipient labels, conditions, user labels, documents, tokens,
response bodies or resource identities are included in the report.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

MAX_BODY_BYTES = 1024 * 1024
MAX_PAGES = 10
PAGE_SIZE = 100
# Descriptor availability is not time-series availability or alert coverage.
METRICS = {
    "run.googleapis.com/request_count": ("DELTA", "INT64"),
    "run.googleapis.com/request_latencies": ("DELTA", "DISTRIBUTION"),
    "run.googleapis.com/container/instance_count": ("GAUGE", "INT64"),
    "run.googleapis.com/container/cpu/utilizations": ("DELTA", "DISTRIBUTION"),
    "run.googleapis.com/container/memory/utilizations": ("DELTA", "DISTRIBUTION"),
    "run.googleapis.com/job/completed_execution_count": ("DELTA", "INT64"),
    "cloudsql.googleapis.com/database/up": ("GAUGE", "INT64"),
    "cloudsql.googleapis.com/database/cpu/utilization": ("GAUGE", "DOUBLE"),
    "cloudsql.googleapis.com/database/disk/utilization": ("GAUGE", "DOUBLE"),
    "cloudsql.googleapis.com/database/disk/bytes_used": ("GAUGE", "INT64"),
    "cloudsql.googleapis.com/database/postgresql/num_backends": ("GAUGE", "INT64"),
    "redis.googleapis.com/clients/connected": ("GAUGE", "INT64"),
    "redis.googleapis.com/server/uptime": ("GAUGE", "INT64"),
    "redis.googleapis.com/stats/memory/usage_ratio": ("GAUGE", "DOUBLE"),
    "redis.googleapis.com/stats/evicted_keys": ("DELTA", "INT64"),
}


class CheckError(ValueError):
    """Fixed diagnostic codes only, never an upstream exception's text."""


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps({"status": "error", "reason": "invalid_arguments"}))
        raise SystemExit(2)


def parse_arguments(argv=None):
    parser = SafeParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--project", required=True)
    parser.add_argument("--gcloud", default="gcloud", help="Trusted SDK executable")
    parser.add_argument("--ca-file", help="Optional trusted CA bundle; TLS verification is always enabled")
    parser.add_argument("--timeout-seconds", type=float, default=15.0,
                        help="Per token command/network blocking-operation timeout, 1 to 30 seconds")
    args = parser.parse_args(argv)
    if (not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]|[0-9]{6,20}", args.project)
            or not math.isfinite(args.timeout_seconds) or not 1 <= args.timeout_seconds <= 30
            or not args.gcloud or "\x00" in args.gcloud
            or (args.ca_file is not None and "\x00" in args.ca_file)):
        parser.error("invalid input")
    return args


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CheckError("invalid_json")
        result[key] = value
    return result


def reject_constant(value):
    raise CheckError("invalid_json")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the Authorization header to a redirected origin.
        return None


def reader(args):
    """Build a GET-only verified-TLS reader; credentials never enter process args."""
    try:
        context = ssl.create_default_context(cafile=args.ca_file)
        environment = os.environ.copy()
        environment.update(CLOUDSDK_CORE_DISABLE_FILE_LOGGING="1", CLOUDSDK_CORE_DISABLE_PROMPTS="1")
        result = subprocess.run(
            [args.gcloud, "auth", "print-access-token", "--quiet"],
            capture_output=True, text=True, encoding="utf-8", timeout=args.timeout_seconds,
            env=environment, check=False,
        )
        token = result.stdout.strip()
        if result.returncode or not token or len(token) > 16384 or any(ord(c) < 33 for c in token):
            raise CheckError("authentication_unavailable")
    except (OSError, UnicodeError, subprocess.SubprocessError):
        raise CheckError("authentication_or_trust_unavailable") from None
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))

    def get(url, params):
        request = urllib.request.Request(
            url + "?" + urllib.parse.urlencode(params),
            headers={"Authorization": "Bearer " + token}, method="GET",
        )
        try:
            with opener.open(request, timeout=args.timeout_seconds) as response:
                raw = response.read(MAX_BODY_BYTES + 1)
                if len(raw) > MAX_BODY_BYTES:
                    raise CheckError("response_size_limit")
            value = json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)
            if not isinstance(value, dict):
                raise CheckError("invalid_schema")
            return value
        except urllib.error.HTTPError as error:
            # No response body is read and no provider message is printed.
            raise CheckError("http_" + str(error.code)) from None
        except (urllib.error.URLError, OSError):
            raise CheckError("transport_or_trust_unavailable") from None
        except (ValueError, RecursionError) as error:
            if isinstance(error, CheckError):
                raise
            raise CheckError("invalid_json") from None
    return get


def list_records(get, url, params, key):
    items, tokens = [], set()
    for _ in range(MAX_PAGES):
        page = get(url, params)
        if not isinstance(page, dict):
            raise CheckError("invalid_schema")
        # An empty protobuf list is omitted. Unexpected response objects are not
        # silently interpreted as an empty successful inventory.
        if set(page) - {key, "nextPageToken", "totalSize"}:
            raise CheckError("invalid_schema")
        value = page.get(key, [])
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise CheckError("invalid_schema")
        if len(value) > PAGE_SIZE:
            raise CheckError("page_size_limit")
        items.extend(value)
        token = page.get("nextPageToken", "")
        if not isinstance(token, str) or len(token) > 16384:
            raise CheckError("invalid_schema")
        if not token:
            return items
        if token in tokens:
            raise CheckError("pagination_cycle")
        tokens.add(token)
        params = {**params, "pageToken": token}
    raise CheckError("page_limit")


def summarize(name, items):
    if any(not isinstance(item.get("name"), str) or not item["name"] for item in items):
        raise CheckError("invalid_schema")
    report = {"status": "complete", "count": len(items)}
    if name in {"alerts", "channels"}:
        # The AlertPolicy read contract explicitly makes an omitted value
        # unknown. Never invent disabled/default state from partial metadata.
        if any(type(item.get("enabled")) is not bool for item in items):
            raise CheckError("invalid_schema")
        report["enabled_count"] = sum(item["enabled"] for item in items)
    if name == "alerts":
        for item in items:
            channels = item.get("notificationChannels", [])
            if not isinstance(channels, list) or any(not isinstance(c, str) for c in channels):
                raise CheckError("invalid_schema")
        report["with_channel_reference_count"] = sum(bool(i.get("notificationChannels")) for i in items)
    if name == "channels":
        states = {"VERIFICATION_STATUS_UNSPECIFIED", "UNVERIFIED", "VERIFIED"}
        for item in items:
            state = item.get("verificationStatus", "VERIFICATION_STATUS_UNSPECIFIED")
            if type(state) is not str or state not in states:
                raise CheckError("invalid_schema")
        report["verified_count"] = sum(i.get("verificationStatus") == "VERIFIED" for i in items)
    return report


def collect(project, get):
    base = "https://monitoring.googleapis.com/v3/projects/" + project + "/"
    report = {
        "schema_version": 1,
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "read_only": True, "operational_acceptance": False,
        "external_and_organization_inventory": "not_verified",
        "alert_delivery_and_responders": "not_verified",
        "time_series_and_signal_coverage": "not_verified",
        "limits": {"max_pages_per_collection": MAX_PAGES, "page_size": PAGE_SIZE,
                   "max_response_bytes": MAX_BODY_BYTES},
        "collections": {}, "native_metric_descriptors": {},
    }
    specs = [
        ("alerts", base + "alertPolicies", "alertPolicies",
         "alertPolicies(name,enabled,notificationChannels),nextPageToken"),
        ("uptime", base + "uptimeCheckConfigs", "uptimeCheckConfigs",
         "uptimeCheckConfigs(name),nextPageToken"),
        ("channels", base + "notificationChannels", "notificationChannels",
         "notificationChannels(name,enabled,verificationStatus),nextPageToken"),
    ]
    for name, url, key, fields in specs:
        try:
            items = list_records(get, url, {"fields": fields, "pageSize": str(PAGE_SIZE)}, key)
            report["collections"][name] = summarize(name, items)
        except CheckError as error:
            report["collections"][name] = {"status": "error", "reason": str(error)}
    for prefix in ("run.googleapis.com/", "cloudsql.googleapis.com/", "redis.googleapis.com/"):
        try:
            items = list_records(get, base + "metricDescriptors", {
                "fields": "metricDescriptors(type,metricKind,valueType),nextPageToken",
                "filter": 'metric.type = starts_with("' + prefix + '")',
                "pageSize": str(PAGE_SIZE),
            }, "metricDescriptors")
            seen = {}
            for item in items:
                kind = item.get("type")
                if not isinstance(kind, str) or kind in seen:
                    raise CheckError("invalid_schema")
                seen[kind] = (item.get("metricKind"), item.get("valueType"))
            for kind, expected in METRICS.items():
                if kind.startswith(prefix):
                    report["native_metric_descriptors"][kind] = (
                        "available" if seen.get(kind) == expected
                        else "not_listed" if kind not in seen else "contract_mismatch")
        except CheckError as error:
            for kind in METRICS:
                if kind.startswith(prefix):
                    report["native_metric_descriptors"][kind] = "query_error"
            report["collections"][prefix.split(".")[0] + "_descriptors"] = {
                "status": "error", "reason": str(error)}
    gaps = []
    for name in ("alerts", "uptime", "channels"):
        item = report["collections"][name]
        if item["status"] == "complete" and not item.get("enabled_count", item["count"]):
            gaps.append(name + "_none_enabled_or_configured")
    if any(value in {"not_listed", "contract_mismatch"} for value in report["native_metric_descriptors"].values()):
        gaps.append("native_descriptor_review_required")
    error = any(item["status"] == "error" for item in report["collections"].values())
    report["configuration_gaps"] = gaps
    code = 2 if error else 1 if gaps else 0
    report.update(status=("inventory_complete", "inventory_gaps", "inventory_error")[code], exit_code=code)
    return report


def main(argv=None):
    args = parse_arguments(argv)
    try:
        report = collect(args.project, reader(args))
    except CheckError as error:
        report = {"status": "inventory_error", "exit_code": 2, "read_only": True,
                  "operational_acceptance": False, "reason": str(error)}
    print(json.dumps(report, indent=2, allow_nan=False))
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
