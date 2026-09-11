"""Plan and apply Cloud Monitoring AlertPolicy resources from infra/monitoring/policies.

Dry-run by default (no network write calls). --apply requires --channel and
makes create (POST) / update (PATCH) calls; it never deletes. Auth extends
scripts/monitoring_check.py's gcloud-token, verified-TLS, no-redirect opener
with POST/PATCH against the same host. The bearer token stays in a closure,
never in argv, logs or the report.
"""
from __future__ import annotations

import argparse
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
from pathlib import Path
from typing import Callable, Iterable

MAX_BODY_BYTES = 1024 * 1024
MAX_PAGES = 10
PAGE_SIZE = 100
PROJECT_PLACEHOLDER = "{{PROJECT}}"
CHANNEL_PLACEHOLDER = "{{NOTIFICATION_CHANNEL}}"
SERVER_ASSIGNED_FIELDS = ("name", "creationRecord", "mutationRecord")
UPDATE_MASK_FIELDS = (
    "displayName", "combiner", "conditions", "documentation",
    "alertStrategy", "notificationChannels", "enabled",
)
# The subset of AlertPolicy REST v3 fields this tool writes. Any other key at
# any of these levels is a body the API would very likely reject; the strict
# validator below fails on it rather than silently sending it.
ALERT_POLICY_SCHEMA: dict = {
    "displayName": str,
    "combiner": str,
    "enabled": bool,
    "notificationChannels": [str],
    "documentation": {"content": str, "mimeType": str},
    "alertStrategy": {
        "notificationRateLimit": {"period": str},
        "autoClose": str,
    },
    "conditions": [{
        "displayName": str,
        "conditionThreshold": {
            "filter": str,
            "comparison": str,
            "thresholdValue": (int, float),
            "duration": str,
            "trigger": {"count": (int, float), "percent": (int, float)},
            "aggregations": [{
                "alignmentPeriod": str,
                "perSeriesAligner": str,
                "crossSeriesReducer": str,
                "groupByFields": [str],
            }],
            # Native REST v3 ratio support (numerator filter/aggregations
            # above, denominator here): no query-language condition type is
            # needed for a ratio rule.
            "denominatorFilter": str,
            "denominatorAggregations": [{
                "alignmentPeriod": str,
                "perSeriesAligner": str,
                "crossSeriesReducer": str,
                "groupByFields": [str],
            }],
        },
        "conditionAbsent": {
            "filter": str,
            "duration": str,
            "trigger": {"count": (int, float), "percent": (int, float)},
            "aggregations": [{
                "alignmentPeriod": str,
                "perSeriesAligner": str,
                "crossSeriesReducer": str,
                "groupByFields": [str],
            }],
        },
    }],
}

METRIC_TYPE_RE = re.compile(r'metric\.type\s*=\s*"([^"]+)"')


class ApplyError(ValueError):
    """Fixed diagnostic codes only, never an upstream exception's text."""


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps({"status": "error", "reason": "invalid_arguments", "exit_code": 2}))
        raise SystemExit(2)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the Authorization header to a redirected origin.
        return None


def parse_arguments(argv=None):
    parser = SafeParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--project", required=True)
    parser.add_argument("--gcloud", default="gcloud", help="Trusted SDK executable")
    parser.add_argument("--ca-file", help="Optional trusted CA bundle; TLS verification is always enabled")
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    parser.add_argument("--apply", action="store_true", help="Make write calls; default is dry-run")
    parser.add_argument("--channel", help="Notification channel resource name; required only with --apply")
    parser.add_argument("--report", help="Write the JSON report here instead of stdout")
    parser.add_argument("--policies-dir", default="infra/monitoring/policies")
    args = parser.parse_args(argv)
    if (not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]|[0-9]{6,20}", args.project)
            or not math.isfinite(args.timeout_seconds) or not 1 <= args.timeout_seconds <= 30
            or not args.gcloud or "\x00" in args.gcloud
            or (args.ca_file is not None and "\x00" in args.ca_file)):
        parser.error("invalid input")
    # Explicit pre-network guard: --apply implies --channel. This is checked
    # here, before reader()/existing_policies()/anything network-shaped runs,
    # not lazily while iterating policies or via argparse required= (which
    # cannot express "required only when another flag is set").
    if args.apply and not args.channel:
        parser.error("--apply requires --channel")
    return args


def metric_types_referenced(policy: dict) -> list[str]:
    """Every metric.type literal in a policy's condition filters, in order.

    Scans both the numerator filter and (for ratio conditions) the
    denominatorFilter -- a metric type hidden only in the denominator must
    still be checked against the inventory allowlist.
    """
    found = []
    for condition in policy.get("conditions", []) if isinstance(policy, dict) else []:
        for kind in ("conditionThreshold", "conditionAbsent"):
            spec = condition.get(kind) if isinstance(condition, dict) else None
            if not isinstance(spec, dict):
                continue
            for key in ("filter", "denominatorFilter"):
                filt = spec.get(key)
                if isinstance(filt, str):
                    found.extend(METRIC_TYPE_RE.findall(filt))
    return found


def required_shape_errors(policy: dict, available_metric_types: Iterable[str]) -> list[str]:
    """Non-empty return means the policy is not fit to plan or apply."""
    errors = []
    if not isinstance(policy, dict):
        return ["not_an_object"]
    if not isinstance(policy.get("displayName"), str) or not policy["displayName"]:
        errors.append("missing_display_name")
    if not isinstance(policy.get("combiner"), str) or not policy["combiner"]:
        errors.append("missing_combiner")
    conditions = policy.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        errors.append("missing_conditions")
    documentation = policy.get("documentation")
    content = documentation.get("content") if isinstance(documentation, dict) else None
    if not isinstance(content, str) or not content.strip():
        errors.append("missing_documentation")
    elif "MONITORING-RUNBOOK.md" not in content:
        errors.append("documentation_missing_runbook_reference")
    available = set(available_metric_types)
    for metric_type in metric_types_referenced(policy):
        if metric_type not in available:
            errors.append("metric_not_in_inventory:" + metric_type)
    return errors


def validate_rest_shape(value, schema=None, path="$") -> list[str]:
    """Recursively reject any key not in ALERT_POLICY_SCHEMA. Empty = valid."""
    if schema is None:
        schema = ALERT_POLICY_SCHEMA
    errors = []
    if isinstance(schema, dict):
        if not isinstance(value, dict):
            return [path + ": expected object"]
        for key, sub in value.items():
            if key not in schema:
                errors.append(path + "." + str(key) + ": unknown_key")
            else:
                errors.extend(validate_rest_shape(sub, schema[key], path + "." + str(key)))
    elif isinstance(schema, list):
        if not isinstance(value, list):
            return [path + ": expected array"]
        for index, item in enumerate(value):
            errors.extend(validate_rest_shape(item, schema[0], path + "[" + str(index) + "]"))
    elif schema is bool:
        if not isinstance(value, bool):
            errors.append(path + ": wrong_type")
    else:
        # Numeric leaves are declared (int, float); Python's bool is an int
        # subtype, so it is excluded explicitly rather than accepted by accident.
        if not isinstance(value, schema) or isinstance(value, bool):
            errors.append(path + ": wrong_type")
    return errors


def _substitute_text(text: str, project: str, channel: str | None) -> str:
    text = text.replace(PROJECT_PLACEHOLDER, project)
    if channel is not None:
        text = text.replace(CHANNEL_PLACEHOLDER, channel)
    return text


def load_local_policies(policies_dir, project: str, channel: str | None,
                         available_metric_types: Iterable[str]) -> tuple[list[dict], list[dict]]:
    """Read every *.json under policies_dir. Returns (policies, errors).

    A file that fails shape validation is skipped (recorded in errors) rather
    than aborting the rest of the directory.
    """
    policies, errors = [], []
    available = list(available_metric_types)
    for path in sorted(Path(policies_dir).glob("*.json")):
        try:
            raw = path.read_text(encoding="utf-8")
            body = json.loads(_substitute_text(raw, project, channel))
        except (OSError, ValueError):
            errors.append({"file": path.name, "reason": "invalid_json"})
            continue
        problems = required_shape_errors(body, available)
        if problems:
            errors.append({"file": path.name, "reason": ",".join(problems)})
            continue
        policies.append(body)
    return policies, errors


def _normalize_for_compare(body: dict, ignore_channels: bool) -> dict:
    result = {k: v for k, v in body.items() if k not in SERVER_ASSIGNED_FIELDS}
    if ignore_channels:
        result.pop("notificationChannels", None)
    return result


def _list_all(get: Callable, url: str, params: dict, key: str) -> list[dict]:
    items, tokens = [], set()
    for _ in range(MAX_PAGES):
        page = get(url, params)
        if not isinstance(page, dict):
            raise ApplyError("invalid_schema")
        value = page.get(key, [])
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise ApplyError("invalid_schema")
        if len(value) > PAGE_SIZE:
            raise ApplyError("page_size_limit")
        items.extend(value)
        token = page.get("nextPageToken", "")
        if not isinstance(token, str) or len(token) > 16384:
            raise ApplyError("invalid_schema")
        if not token:
            return items
        if token in tokens:
            raise ApplyError("pagination_cycle")
        tokens.add(token)
        params = {**params, "pageToken": token}
    raise ApplyError("page_limit")


def existing_policies(get: Callable, project: str, wanted_display_names: Iterable[str]) -> dict[str, dict]:
    """displayName -> full remote body, for remote policies matching a local displayName."""
    base = "https://monitoring.googleapis.com/v3/projects/" + project + "/alertPolicies"
    wanted = set(wanted_display_names)
    summary = _list_all(get, base, {"fields": "alertPolicies(name,displayName),nextPageToken",
                                     "pageSize": str(PAGE_SIZE)}, "alertPolicies")
    result = {}
    for item in summary:
        name, display = item.get("name"), item.get("displayName")
        if not isinstance(name, str) or not isinstance(display, str):
            raise ApplyError("invalid_schema")
        if display in wanted:
            full = get("https://monitoring.googleapis.com/v3/" + name, {})
            if not isinstance(full, dict) or full.get("name") != name:
                raise ApplyError("invalid_schema")
            result[display] = full
    return result


def plan_policies(local: list[dict], existing: dict[str, dict], channel: str | None = None) -> list[dict]:
    plan = []
    ignore_channels = channel is None
    for policy in local:
        display = policy["displayName"]
        match = existing.get(display)
        if match is None:
            plan.append({"displayName": display, "action": "create", "existing_name": None})
            continue
        same = (_normalize_for_compare(match, ignore_channels)
                == _normalize_for_compare(policy, ignore_channels))
        plan.append({
            "displayName": display,
            "action": "noop" if same else "update",
            "existing_name": match["name"],
        })
    return plan


def apply_policies(plan: list[dict], local_by_display: dict[str, dict], project: str,
                    post: Callable, patch: Callable) -> list[dict]:
    base = "https://monitoring.googleapis.com/v3/projects/" + project + "/alertPolicies"
    results = []
    for entry in plan:
        display = entry["displayName"]
        body = local_by_display[display]
        if entry["action"] == "create":
            response = post(base, body)
            results.append({"displayName": display, "action": "create", "resource_name": response.get("name")})
        elif entry["action"] == "update":
            mask = ",".join(field for field in UPDATE_MASK_FIELDS if field in body)
            url = "https://monitoring.googleapis.com/v3/" + entry["existing_name"] + "?updateMask=" + urllib.parse.quote(mask, safe=",")
            response = patch(url, body)
            results.append({"displayName": display, "action": "update", "resource_name": response.get("name")})
        else:
            results.append({"displayName": display, "action": "noop", "resource_name": entry["existing_name"]})
    return results


def reader(args):
    """Build a verified-TLS GET/POST/PATCH set; credentials never enter process args."""
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
            raise ApplyError("authentication_unavailable")
    except (OSError, UnicodeError, subprocess.SubprocessError):
        raise ApplyError("authentication_or_trust_unavailable") from None
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))

    def _call(url, method, body=None, params=None):
        query = "?" + urllib.parse.urlencode(params) if params else ""
        headers = {"Authorization": "Bearer " + token}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url + query, data=data, headers=headers, method=method)
        try:
            with opener.open(request, timeout=args.timeout_seconds) as response:
                raw = response.read(MAX_BODY_BYTES + 1)
                if len(raw) > MAX_BODY_BYTES:
                    raise ApplyError("response_size_limit")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ApplyError("invalid_schema")
            return value
        except urllib.error.HTTPError as error:
            raise ApplyError("http_" + str(error.code)) from None
        except (urllib.error.URLError, OSError):
            raise ApplyError("transport_or_trust_unavailable") from None
        except ValueError:
            raise ApplyError("invalid_json") from None

    def get(url, params):
        return _call(url, "GET", params=params)

    def post(url, body):
        return _call(url, "POST", body=body)

    def patch(url, body):
        return _call(url, "PATCH", body=body)

    return get, post, patch


def _refuse_write(*_args, **_kwargs):
    raise ApplyError("write_attempted_in_dry_run")


def run(args, get: Callable, post: Callable | None = None, patch: Callable | None = None) -> dict:
    channel_for_load = args.channel if args.apply else None
    local, errors = load_local_policies(args.policies_dir, args.project, channel_for_load,
                                         _inventory_metric_types())
    local_by_display = {policy["displayName"]: policy for policy in local}
    existing = existing_policies(get, args.project, local_by_display.keys())
    plan = plan_policies(local, existing, channel_for_load)
    applied = None
    if args.apply:
        applied = apply_policies(plan, local_by_display, args.project, post or _refuse_write, patch or _refuse_write)
    report = {
        "schema_version": 1,
        "project": args.project,
        "read_only": not args.apply,
        "policies": applied if applied is not None else plan,
        "skipped_files": errors,
    }
    report["exit_code"] = 1 if errors else 0
    return report


_INVENTORY_PATH = Path(__file__).resolve().parents[1] / "infra/monitoring/evidence/c370-inventory-2026-09-08.json"


def _inventory_metric_types() -> list[str]:
    data = json.loads(_INVENTORY_PATH.read_text(encoding="utf-8"))
    descriptors = data.get("native_metric_descriptors", {})
    return [kind for kind, status in descriptors.items() if status == "available"]


def main(argv=None):
    args = parse_arguments(argv)
    try:
        get, post, patch = reader(args)
        report = run(args, get, post, patch)
    except ApplyError as error:
        report = {"status": "error", "exit_code": 2, "read_only": not args.apply, "reason": str(error)}
    output = json.dumps(report, indent=2, allow_nan=False)
    if args.report:
        Path(args.report).write_text(output + "\n", encoding="utf-8")
    else:
        print(output)
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
