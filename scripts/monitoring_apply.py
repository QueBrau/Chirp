"""Plan and apply Cloud Monitoring resources: log-based metrics, uptime checks
and AlertPolicy from infra/monitoring/{metrics,uptime,policies}.

Dry-run by default (no network write calls). --apply requires --channel and
makes create (POST) / update (PATCH or, for LogMetric, PUT) calls; it never
deletes. Auth extends scripts/monitoring_check.py's gcloud-token,
verified-TLS, no-redirect opener with POST/PATCH/PUT against the same host.
The bearer token stays in a closure, never in argv, logs or the report.

Apply order is always metrics -> uptime -> policies, so a policy can
reference a log metric or uptime check this same run just created.
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
API_HOST_PLACEHOLDER = "{{API_HOST}}"
WS_HOST_PLACEHOLDER = "{{WS_HOST}}"
SERVER_ASSIGNED_FIELDS = ("name", "creationRecord", "mutationRecord")
UPDATE_MASK_FIELDS = (
    "displayName", "combiner", "conditions", "documentation",
    "alertStrategy", "notificationChannels", "enabled",
)
UPTIME_UPDATE_MASK_FIELDS = (
    "displayName", "period", "timeout", "contentMatchers", "checkerType",
    "selectedRegions", "monitoredResource", "httpCheck",
)
UPTIME_CHECK_PASSED_METRIC = "monitoring.googleapis.com/uptime_check/check_passed"
LOG_METRIC_TYPE_PREFIX = "logging.googleapis.com/user/"
HOSTNAME_RE = re.compile(
    r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
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

# LogMetric REST v2 fields this tool writes (projects.metrics). Identity is
# the `name` field itself -- unlike AlertPolicy there is no separate
# displayName. labelExtractors is a free-form map[str,str] (arbitrary label
# names as keys), so it is declared as the bare `dict` type: validate_rest_shape
# treats a non-dict/list schema value as a leaf isinstance check, which for
# `dict` accepts any mapping without walking into per-key structure.
LOG_METRIC_SCHEMA: dict = {
    "name": str,
    "description": str,
    "filter": str,
    "disabled": bool,
    "metricDescriptor": {
        "name": str,
        "type": str,
        "labels": [{"key": str, "valueType": str, "description": str}],
        "metricKind": str,
        "valueType": str,
        "unit": str,
        "description": str,
        "displayName": str,
    },
    "labelExtractors": dict,
}

# UptimeCheckConfig REST v3 fields this tool writes (projects.uptimeCheckConfigs).
# Identity is displayName, matched the same way as AlertPolicy.
UPTIME_CHECK_SCHEMA: dict = {
    "displayName": str,
    "period": str,
    "timeout": str,
    "contentMatchers": [{"content": str, "matcher": str}],
    "checkerType": str,
    "selectedRegions": [str],
    "monitoredResource": {
        "type": str,
        "labels": {"project_id": str, "host": str},
    },
    "httpCheck": {
        "requestMethod": str,
        "useSsl": bool,
        "path": str,
        "port": (int, float),
        "validateSsl": bool,
        "acceptedResponseStatusCodes": [{"statusClass": str, "statusValue": (int, float)}],
    },
}

METRIC_TYPE_RE = re.compile(r'metric\.type\s*=\s*"([^"]+)"')
# Both "resource.label.X" and "resource.labels.X" are accepted Monitoring
# filter spellings; this repo's existing policies use the plural form, but
# the regex accepts either so a future filter isn't silently unmatched.
HOST_FILTER_RE = re.compile(r'resource\.labels?\.host\s*=\s*"([^"]+)"')


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
    parser.add_argument("--metrics-dir", default="infra/monitoring/metrics")
    parser.add_argument("--uptime-dir", default="infra/monitoring/uptime")
    parser.add_argument("--api-host", default=None,
                         help="Hostname for the chirp-api uptime check; required only when infra/monitoring/uptime has files")
    parser.add_argument("--ws-host", default=None,
                         help="Hostname for the chirp-ws uptime check; required only when infra/monitoring/uptime has files")
    args = parser.parse_args(argv)
    if (not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]|[0-9]{6,20}", args.project)
            or not math.isfinite(args.timeout_seconds) or not 1 <= args.timeout_seconds <= 30
            or not args.gcloud or "\x00" in args.gcloud
            or (args.ca_file is not None and "\x00" in args.ca_file)):
        parser.error("invalid input")
    for host_value in (args.api_host, args.ws_host):
        if host_value is not None and not HOSTNAME_RE.fullmatch(host_value):
            parser.error("invalid host")
    # Explicit pre-network guard, same posture as --apply implying --channel:
    # a bare hostname is required for BOTH endpoints whenever any uptime file
    # exists, checked here before reader()/existing_*()/anything network-shaped
    # runs. Substitution is unconditional (dry-run planning also needs the
    # real body to diff against remote state), so this cannot be deferred
    # until --apply the way --channel is.
    uptime_files_exist = any(Path(args.uptime_dir).glob("*.json"))
    if uptime_files_exist and (args.api_host is None or args.ws_host is None):
        parser.error("--api-host and --ws-host are required when infra/monitoring/uptime has files")
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


def _hosts_referenced(policy: dict) -> list[str]:
    """Every resource.label(s).host literal in a policy's condition filters."""
    found = []
    for condition in policy.get("conditions", []) if isinstance(policy, dict) else []:
        for kind in ("conditionThreshold", "conditionAbsent"):
            spec = condition.get(kind) if isinstance(condition, dict) else None
            if isinstance(spec, dict) and isinstance(spec.get("filter"), str):
                found.extend(HOST_FILTER_RE.findall(spec["filter"]))
    return found


def required_shape_errors(policy: dict, available_metric_types: Iterable[str],
                           defined_log_metric_types: Iterable[str] = (),
                           defined_uptime_hosts: Iterable[str] = ()) -> list[str]:
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
    defined_log_metrics = set(defined_log_metric_types)
    defined_hosts = set(defined_uptime_hosts)
    # Manager default D (inventory scope), extended for slice 2: a native
    # metric.type literal still checks against the frozen c370 inventory; a
    # logging.googleapis.com/user/<name> literal instead checks against the
    # log metrics THIS repo's own infra/monitoring/metrics defines; the
    # uptime_check/check_passed metric has no static check id to check
    # (server-assigned at creation), so it is cross-checked below by host.
    for metric_type in metric_types_referenced(policy):
        if metric_type == UPTIME_CHECK_PASSED_METRIC:
            continue
        if metric_type.startswith(LOG_METRIC_TYPE_PREFIX):
            if metric_type not in defined_log_metrics:
                errors.append("metric_not_in_inventory:" + metric_type)
            continue
        if metric_type not in available:
            errors.append("metric_not_in_inventory:" + metric_type)
    if UPTIME_CHECK_PASSED_METRIC in metric_types_referenced(policy):
        hosts_in_filters = set(_hosts_referenced(policy))
        if not hosts_in_filters:
            errors.append("uptime_check_host_missing")
        else:
            for host in sorted(hosts_in_filters):
                if host not in defined_hosts:
                    errors.append("uptime_check_host_not_in_repo:" + host)
    # The strict REST-shape check runs on this same path so that an unknown
    # key skips the file at load time instead of reaching the live API.
    errors.extend("rest_shape:" + problem for problem in validate_rest_shape(policy))
    return errors


def required_shape_errors_metric(metric: dict) -> list[str]:
    """Non-empty return means the LogMetric is not fit to plan or apply."""
    errors = []
    if not isinstance(metric, dict):
        return ["not_an_object"]
    if not isinstance(metric.get("name"), str) or not metric["name"]:
        errors.append("missing_name")
    if not isinstance(metric.get("filter"), str) or not metric["filter"]:
        errors.append("missing_filter")
    description = metric.get("description")
    if not isinstance(description, str) or not description.strip():
        errors.append("missing_description")
    elif "MONITORING-RUNBOOK.md" not in description:
        errors.append("documentation_missing_runbook_reference")
    errors.extend("rest_shape:" + problem for problem in validate_rest_shape(metric, LOG_METRIC_SCHEMA))
    return errors


def required_shape_errors_uptime(config: dict) -> list[str]:
    """Non-empty return means the UptimeCheckConfig is not fit to plan or apply."""
    errors = []
    if not isinstance(config, dict):
        return ["not_an_object"]
    if not isinstance(config.get("displayName"), str) or not config["displayName"]:
        errors.append("missing_display_name")
    resource = config.get("monitoredResource")
    labels = resource.get("labels") if isinstance(resource, dict) else None
    host = labels.get("host") if isinstance(labels, dict) else None
    if not isinstance(host, str) or not host or "{{" in host:
        errors.append("missing_or_unsubstituted_host")
    errors.extend("rest_shape:" + problem for problem in validate_rest_shape(config, UPTIME_CHECK_SCHEMA))
    return errors


def validate_rest_shape(value, schema=None, path="$") -> list[str]:
    """Recursively reject any key not in the schema. Empty = valid."""
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
        # A bare `dict` leaf (LogMetric.labelExtractors) accepts any mapping
        # without walking into per-key structure -- label names are arbitrary.
        if not isinstance(value, schema) or (schema is not dict and isinstance(value, bool)):
            errors.append(path + ": wrong_type")
    return errors


def _substitute_text(text: str, project: str, channel: str | None = None,
                      api_host: str | None = None, ws_host: str | None = None) -> str:
    text = text.replace(PROJECT_PLACEHOLDER, project)
    if channel is not None:
        text = text.replace(CHANNEL_PLACEHOLDER, channel)
    if api_host is not None:
        text = text.replace(API_HOST_PLACEHOLDER, api_host)
    if ws_host is not None:
        text = text.replace(WS_HOST_PLACEHOLDER, ws_host)
    return text


def load_local_policies(policies_dir, project: str, channel: str | None,
                         available_metric_types: Iterable[str],
                         defined_log_metric_types: Iterable[str] = (),
                         defined_uptime_hosts: Iterable[str] = (),
                         api_host: str | None = None, ws_host: str | None = None,
                         ) -> tuple[list[dict], list[dict]]:
    """Read every *.json under policies_dir. Returns (policies, errors).

    A file that fails shape validation is skipped (recorded in errors) rather
    than aborting the rest of the directory.
    """
    policies, errors = [], []
    available = list(available_metric_types)
    log_metrics = list(defined_log_metric_types)
    hosts = list(defined_uptime_hosts)
    for path in sorted(Path(policies_dir).glob("*.json")):
        try:
            raw = path.read_text(encoding="utf-8")
            body = json.loads(_substitute_text(raw, project, channel, api_host, ws_host))
        except (OSError, ValueError):
            errors.append({"file": path.name, "reason": "invalid_json"})
            continue
        problems = required_shape_errors(body, available, log_metrics, hosts)
        if problems:
            errors.append({"file": path.name, "reason": ",".join(problems)})
            continue
        policies.append(body)
    return policies, errors


def load_local_metrics(metrics_dir, project: str) -> tuple[list[dict], list[dict]]:
    """Read every *.json under metrics_dir. Returns (metrics, errors)."""
    metrics, errors = [], []
    for path in sorted(Path(metrics_dir).glob("*.json")):
        try:
            raw = path.read_text(encoding="utf-8")
            body = json.loads(_substitute_text(raw, project))
        except (OSError, ValueError):
            errors.append({"file": path.name, "reason": "invalid_json"})
            continue
        problems = required_shape_errors_metric(body)
        if problems:
            errors.append({"file": path.name, "reason": ",".join(problems)})
            continue
        metrics.append(body)
    return metrics, errors


def load_local_uptime(uptime_dir, project: str, api_host: str | None,
                       ws_host: str | None) -> tuple[list[dict], list[dict]]:
    """Read every *.json under uptime_dir. Returns (configs, errors)."""
    configs, errors = [], []
    for path in sorted(Path(uptime_dir).glob("*.json")):
        try:
            raw = path.read_text(encoding="utf-8")
            body = json.loads(_substitute_text(raw, project, None, api_host, ws_host))
        except (OSError, ValueError):
            errors.append({"file": path.name, "reason": "invalid_json"})
            continue
        problems = required_shape_errors_uptime(body)
        if problems:
            errors.append({"file": path.name, "reason": ",".join(problems)})
            continue
        configs.append(body)
    return configs, errors


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


def _existing_by_display_name(get: Callable, base_url: str, list_key: str,
                               wanted_display_names: Iterable[str]) -> dict[str, dict]:
    """displayName -> full remote body, for remote resources matching a local
    displayName. Shared by AlertPolicy and UptimeCheckConfig, the two kinds
    whose identity IS displayName; LogMetric's identity is its `name` field
    directly (no displayName field exists), so it uses existing_metrics()
    instead, a genuinely different (GET-by-name, no listing) lookup shape."""
    wanted = set(wanted_display_names)
    summary = _list_all(get, base_url, {"fields": list_key + "(name,displayName),nextPageToken",
                                         "pageSize": str(PAGE_SIZE)}, list_key)
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


def existing_policies(get: Callable, project: str, wanted_display_names: Iterable[str]) -> dict[str, dict]:
    base = "https://monitoring.googleapis.com/v3/projects/" + project + "/alertPolicies"
    return _existing_by_display_name(get, base, "alertPolicies", wanted_display_names)


def existing_uptime(get: Callable, project: str, wanted_display_names: Iterable[str]) -> dict[str, dict]:
    base = "https://monitoring.googleapis.com/v3/projects/" + project + "/uptimeCheckConfigs"
    return _existing_by_display_name(get, base, "uptimeCheckConfigs", wanted_display_names)


def existing_metrics(get: Callable, project: str, wanted_names: Iterable[str]) -> dict[str, dict]:
    """name -> full remote body. LogMetric identity is its `name` field, so
    this is a direct GET per wanted name (404 = does not exist yet), never a
    list+displayName-match -- a LogMetric has no displayName at all."""
    result = {}
    for name in wanted_names:
        url = "https://logging.googleapis.com/v2/projects/" + project + "/metrics/" + urllib.parse.quote(name, safe="")
        try:
            body = get(url, {})
        except ApplyError as error:
            if str(error) == "http_404":
                continue
            raise
        if not isinstance(body, dict) or body.get("name") != name:
            raise ApplyError("invalid_schema")
        result[name] = body
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


def plan_uptime(local: list[dict], existing: dict[str, dict]) -> list[dict]:
    plan = []
    for config in local:
        display = config["displayName"]
        match = existing.get(display)
        if match is None:
            plan.append({"displayName": display, "action": "create", "existing_name": None})
            continue
        same = (_normalize_for_compare(match, ignore_channels=False)
                == _normalize_for_compare(config, ignore_channels=False))
        plan.append({
            "displayName": display,
            "action": "noop" if same else "update",
            "existing_name": match["name"],
        })
    return plan


def plan_metrics(local: list[dict], existing: dict[str, dict]) -> list[dict]:
    plan = []
    for metric in local:
        name = metric["name"]
        match = existing.get(name)
        if match is None:
            plan.append({"name": name, "action": "create", "existing_name": None})
            continue
        same = (_normalize_for_compare(match, ignore_channels=False)
                == _normalize_for_compare(metric, ignore_channels=False))
        plan.append({
            "name": name,
            "action": "noop" if same else "update",
            "existing_name": match.get("name"),
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


def apply_uptime(plan: list[dict], local_by_display: dict[str, dict], project: str,
                  post: Callable, patch: Callable) -> list[dict]:
    base = "https://monitoring.googleapis.com/v3/projects/" + project + "/uptimeCheckConfigs"
    results = []
    for entry in plan:
        display = entry["displayName"]
        body = local_by_display[display]
        if entry["action"] == "create":
            response = post(base, body)
            results.append({"displayName": display, "action": "create", "resource_name": response.get("name")})
        elif entry["action"] == "update":
            mask = ",".join(field for field in UPTIME_UPDATE_MASK_FIELDS if field in body)
            url = "https://monitoring.googleapis.com/v3/" + entry["existing_name"] + "?updateMask=" + urllib.parse.quote(mask, safe=",")
            response = patch(url, body)
            results.append({"displayName": display, "action": "update", "resource_name": response.get("name")})
        else:
            results.append({"displayName": display, "action": "noop", "resource_name": entry["existing_name"]})
    return results


def apply_metrics(plan: list[dict], local_by_name: dict[str, dict], project: str,
                   post: Callable, put: Callable) -> list[dict]:
    """LogMetric.update is PUT, full-replace, with no updateMask parameter --
    a structurally different write mechanic from AlertPolicy/UptimeCheckConfig's
    PATCH+explicit-mask, verified against the projects.metrics.update reference."""
    base = "https://logging.googleapis.com/v2/projects/" + project + "/metrics"
    results = []
    for entry in plan:
        name = entry["name"]
        body = local_by_name[name]
        if entry["action"] == "create":
            response = post(base, body)
            results.append({"name": name, "action": "create", "resource_name": response.get("name")})
        elif entry["action"] == "update":
            url = base + "/" + urllib.parse.quote(name, safe="")
            response = put(url, body)
            results.append({"name": name, "action": "update", "resource_name": response.get("name")})
        else:
            results.append({"name": name, "action": "noop", "resource_name": entry["existing_name"]})
    return results


def reader(args):
    """Build a verified-TLS GET/POST/PATCH/PUT set; credentials never enter process args."""
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

    def put(url, body):
        return _call(url, "PUT", body=body)

    return get, post, patch, put


def _refuse_write(*_args, **_kwargs):
    raise ApplyError("write_attempted_in_dry_run")


def run(args, get: Callable, post: Callable | None = None, patch: Callable | None = None,
        put: Callable | None = None) -> dict:
    channel_for_load = args.channel if args.apply else None
    api_host, ws_host = getattr(args, "api_host", None), getattr(args, "ws_host", None)

    local_metrics, metric_errors = load_local_metrics(args.metrics_dir, args.project)
    local_by_name = {metric["name"]: metric for metric in local_metrics}
    defined_log_metric_types = [LOG_METRIC_TYPE_PREFIX + name for name in local_by_name]

    local_uptime, uptime_errors = load_local_uptime(args.uptime_dir, args.project, api_host, ws_host)
    local_uptime_by_display = {config["displayName"]: config for config in local_uptime}
    defined_uptime_hosts = [config["monitoredResource"]["labels"]["host"] for config in local_uptime]

    local_policies, policy_errors = load_local_policies(
        args.policies_dir, args.project, channel_for_load, _inventory_metric_types(),
        defined_log_metric_types, defined_uptime_hosts, api_host, ws_host,
    )
    local_policies_by_display = {policy["displayName"]: policy for policy in local_policies}

    existing_metric_bodies = existing_metrics(get, args.project, local_by_name.keys())
    metric_plan = plan_metrics(local_metrics, existing_metric_bodies)

    existing_uptime_bodies = existing_uptime(get, args.project, local_uptime_by_display.keys())
    uptime_plan = plan_uptime(local_uptime, existing_uptime_bodies)

    existing_policy_bodies = existing_policies(get, args.project, local_policies_by_display.keys())
    policy_plan = plan_policies(local_policies, existing_policy_bodies, channel_for_load)

    applied_metrics = applied_uptime = applied_policies = None
    if args.apply:
        # Order fixed: metrics -> uptime -> policies, so a policy referencing
        # a log metric or uptime check this same run just created is applied
        # against a resource that now exists.
        applied_metrics = apply_metrics(metric_plan, local_by_name, args.project,
                                         post or _refuse_write, put or _refuse_write)
        applied_uptime = apply_uptime(uptime_plan, local_uptime_by_display, args.project,
                                       post or _refuse_write, patch or _refuse_write)
        applied_policies = apply_policies(policy_plan, local_policies_by_display, args.project,
                                           post or _refuse_write, patch or _refuse_write)

    report = {
        "schema_version": 1,
        "project": args.project,
        "read_only": not args.apply,
        "metrics": applied_metrics if applied_metrics is not None else metric_plan,
        "uptime": applied_uptime if applied_uptime is not None else uptime_plan,
        "policies": applied_policies if applied_policies is not None else policy_plan,
        "skipped_files": metric_errors + uptime_errors + policy_errors,
    }
    report["exit_code"] = 1 if report["skipped_files"] else 0
    return report


_INVENTORY_PATH = Path(__file__).resolve().parents[1] / "infra/monitoring/evidence/c370-inventory-2026-09-08.json"


def _inventory_metric_types() -> list[str]:
    data = json.loads(_INVENTORY_PATH.read_text(encoding="utf-8"))
    descriptors = data.get("native_metric_descriptors", {})
    return [kind for kind, status in descriptors.items() if status == "available"]


def main(argv=None):
    args = parse_arguments(argv)
    try:
        get, post, patch, put = reader(args)
        report = run(args, get, post, patch, put)
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
