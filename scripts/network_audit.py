"""Read-only, deliberately incomplete network/IAM evidence; never a reachability test.

Only allowlisted projections are requested. Provider bodies, principal identifiers,
conditions, addresses and error text stay in memory and are never serialized.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import ipaddress
import json
from pathlib import Path
import re
import sys

from deployment_config import ConfigError, SafeParser, cloud, identifier, load, validate

ROOT = Path(__file__).resolve().parents[1]
IAM_FIELDS = "bindings[].role,bindings[].members,bindings[].condition,version,etag"
NETWORK_ANN = ("run.googleapis.com/vpc-access-connector", "run.googleapis.com/vpc-access-egress", "run.googleapis.com/cloudsql-instances")
ROLES = {
    "roles/owner", "roles/editor", "roles/viewer", "roles/run.invoker",
    "roles/run.admin", "roles/run.developer", "roles/iam.serviceAccountUser",
    "roles/iam.serviceAccountTokenCreator", "roles/secretmanager.secretAccessor",
    "roles/secretmanager.admin", "roles/cloudsql.client", "roles/cloudsql.admin",
    "roles/storage.admin", "roles/storage.objectAdmin", "roles/storage.objectCreator",
    "roles/storage.objectViewer", "roles/storage.legacyBucketOwner",
    "roles/storage.legacyBucketReader", "roles/redis.admin", "roles/redis.viewer",
    "roles/compute.networkAdmin", "roles/compute.securityAdmin",
    "roles/vpcaccess.user", "roles/logging.logWriter", "roles/bigquery.dataEditor",
}


def review_policy(value: dict) -> None:
    try:
        mapping(value["targets"])
        valid = (value["version"] == 1 and value["deployment_source"] == "infra/deployment.json"
                 and set(value["targets"]) == {"network", "redis", "media_bucket"}
                 and all(identifier(v) for v in value["targets"].values())
                 and value["preserve_controls"] == {"sql_ssl_mode": "ENCRYPTED_ONLY", "bucket_public_access_prevention": "enforced", "bucket_uniform_access": True}
                 and value["proposed_redis_controls"] == {"auth_enabled": True, "transit_encryption_mode": "SERVER_AUTHENTICATION", "requires_staging_acceptance": True})
        if not valid:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ConfigError("invalid_network_review_policy") from None


def enum(value, choices):
    return value if isinstance(value, str) and value in choices else "UNKNOWN"


def boolean(value):
    return value if type(value) is bool else None


def integer(value, low=0, high=100000):
    return value if type(value) is int and low <= value <= high else None


def rows(value):
    if not isinstance(value, list) or len(value) > 10000 or any(not isinstance(r, dict) for r in value):
        raise ConfigError("invalid_metadata_shape")
    return value


def mapping(value):
    if not isinstance(value, dict):
        raise ConfigError("invalid_metadata_shape")
    return value


def sequence(value):
    if not isinstance(value, list) or len(value) > 10000 or any(not isinstance(r, str) for r in value):
        raise ConfigError("invalid_metadata_shape")
    return value


def range_summary(value):
    ranges = sequence(value)
    parsed = []
    for row in ranges:
        try:
            parsed.append(ipaddress.ip_network(row, strict=False))
        except ValueError:
            raise ConfigError("invalid_metadata_shape") from None
    return {"count": len(parsed), "includes_anywhere": any(n.prefixlen == 0 for n in parsed)}


def network_matches(value, config, target):
    # Never equate an unrelated project's default VPC with this project's VPC.
    return value in (target, f"projects/{config['project']}/global/networks/{target}",
                     f"https://www.googleapis.com/compute/v1/projects/{config['project']}/global/networks/{target}")


def iam_summary(body, config):
    body = mapping(body)
    # No binding list can mean an empty policy. We report that observation, not
    # absence of inherited grants or permission. A completely empty response is unknown.
    if not body or ("bindings" not in body and "etag" not in body):
        raise ConfigError("invalid_metadata_shape")
    result = {"direct_bindings": [], "unknown_role_bindings": 0, "effective_access_verified": False}
    principal = "serviceAccount:" + config["shared"]["service_account"]
    for row in rows(body.get("bindings", [])):
        if not isinstance(row.get("role"), str) or "members" not in row:
            raise ConfigError("invalid_metadata_shape")
        members = sequence(row["members"])
        role = row.get("role")
        known = role in ROLES if isinstance(role, str) else False
        if not known:
            result["unknown_role_bindings"] += 1
        result["direct_bindings"].append({
            "role": role if known else "OTHER_ROLE",
            "member_count": len(members),
            "shared_runtime_member": principal in members,
            "all_users_member": "allUsers" in members,
            "all_authenticated_users_member": "allAuthenticatedUsers" in members,
            "condition_present": "condition" in row,
        })
    result["direct_bindings"].sort(key=lambda r: (r["role"], r["shared_runtime_member"], r["member_count"]))
    return result


def attachment(spec, annotations, config):
    spec, annotations = mapping(spec), mapping(annotations)
    connector = annotations.get(NETWORK_ANN[0])
    expected = config["shared"]["vpc_connector"]
    expected_long = f"projects/{config['project']}/locations/{config['region']}/connectors/{expected}"
    identity_present = isinstance(spec.get("serviceAccountName"), str) and bool(spec["serviceAccountName"].strip())
    return {
        "runtime_identity_observed": identity_present,
        "shared_runtime_identity": spec["serviceAccountName"] == config["shared"]["service_account"] if identity_present else None,
        "connector_matches_source": connector in (expected, expected_long),
        "egress": enum(annotations.get(NETWORK_ANN[1]), ("private-ranges-only", "all-traffic")),
        "cloud_sql_matches_source": annotations.get(NETWORK_ANN[2]) == config["shared"]["cloud_sql"],
    }


def firewall_summary(body, config, target, effective=False):
    summary = []
    for row in rows(body):
        # Effective-firewall gcloud projections flatten the response and omit
        # protocols. Keep that limitation explicit; classic rules add port evidence.
        kind = enum(row.get("type"), ("org-firewall", "network-firewall", "network-firewall-policy", "network-regional-firewall-policy", "system-network-firewall-policy", "system-network-regional-firewall-policy")) if effective else "network-firewall"
        name = row.get("name", "")
        # A name prefix is a hint, not authoritative management ownership.
        label = name if name in ("default-allow-ssh", "default-allow-rdp", "default-allow-internal", "default-allow-icmp") else ("connector_name_pattern" if isinstance(name, str) and name.startswith("aet-") else "other")
        item = {"kind": kind, "label": label, "direction": enum(row.get("direction"), ("INGRESS", "EGRESS")),
                "priority": integer(row.get("priority"), 0, 2147483647),
                "disabled": boolean(row.get("disabled", False)),
                "source_or_destination_ranges": range_summary(row.get("ip_ranges", [])) if effective else {"source": range_summary(row.get("sourceRanges", [])), "destination": range_summary(row.get("destinationRanges", []))},
                "target_tag_count": len(sequence(row.get("target_tags" if effective else "targetTags", []))),
                "target_identity_count": len(sequence(row.get("target_svc_acct" if effective else "targetServiceAccounts", [])))}
        if effective:
            item.update(action=enum(row.get("action"), ("ALLOW", "DENY", "GOTO_NEXT", "APPLY_SECURITY_PROFILE_GROUP")), protocol_details_collected=False)
        else:
            item["target_network_matches"] = network_matches(row.get("network"), config, target)
            item["source_tag_count"] = len(sequence(row.get("sourceTags", [])))
            item["source_identity_count"] = len(sequence(row.get("sourceServiceAccounts", [])))
            item["rules"] = []
            for action, key in (("ALLOW", "allowed"), ("DENY", "denied")):
                for rule in rows(row.get(key, [])):
                    ports = sequence(rule.get("ports", []))
                    if any(not re.fullmatch(r"[0-9]{1,5}(?:-[0-9]{1,5})?", p) for p in ports):
                        raise ConfigError("invalid_metadata_shape")
                    item["rules"].append({"action": action, "protocol": enum(rule.get("IPProtocol"), ("tcp", "udp", "icmp", "esp", "ah", "sctp", "all", "6", "17", "1", "58")), "ports": ports})
        summary.append(item)
    return summary


def summarize(scope, body, config, policy):
    target = policy["targets"]["network"]
    if scope.startswith("iam/"):
        return iam_summary(body, config)
    if scope == "classic_firewalls":
        return firewall_summary(body, config, target)
    if scope == "effective_firewalls":
        return firewall_summary(body, config, target, True)
    if scope == "vms":
        items = rows(body)
        return {"project_vm_count": len(items), "on_review_network": sum(any(network_matches(n.get("network"), config, target) for n in rows(r.get("networkInterfaces", []))) for r in items),
                "with_external_address_config": sum(any(n.get("accessConfigs") for n in rows(r.get("networkInterfaces", []))) for r in items),
                "shared_runtime_identity_count": sum(any(s.get("email") == config["shared"]["service_account"] for s in rows(r.get("serviceAccounts", []))) for r in items),
                "managed_connector_vms_included": False}
    body = mapping(body)
    if scope == "redis":
        return {"state": enum(body.get("state"), ("READY", "CREATING", "UPDATING", "DELETING", "REPAIRING", "MAINTENANCE", "IMPORTING", "FAILING_OVER")),
                "tier": enum(body.get("tier"), ("BASIC", "STANDARD_HA")),
                "auth_enabled": boolean(body.get("authEnabled")),
                "transit_encryption_mode": enum(body.get("transitEncryptionMode"), ("DISABLED", "SERVER_AUTHENTICATION")),
                "port": integer(body.get("port"), 1, 65535),
                "connect_mode": enum(body.get("connectMode"), ("DIRECT_PEERING", "PRIVATE_SERVICE_ACCESS")),
                "review_network_matches": network_matches(body.get("authorizedNetwork"), config, target)}
    if scope == "connector":
        return {"state": enum(body.get("state"), ("READY", "CREATING", "UPDATING", "DELETING", "ERROR")),
                "review_network_matches": network_matches(body.get("network"), config, target),
                "source_range_present": range_summary([body["ipCidrRange"]])["count"] == 1 if body.get("ipCidrRange") else False,
                "min_instances": integer(body.get("minInstances")), "max_instances": integer(body.get("maxInstances"))}
    if scope == "network":
        peers = rows(body.get("peerings", []))
        return {"auto_subnets": boolean(body.get("autoCreateSubnetworks")),
                "firewall_policy_order": enum(body.get("networkFirewallPolicyEnforcementOrder"), ("BEFORE_CLASSIC_FIREWALL", "AFTER_CLASSIC_FIREWALL")),
                "peerings": [{"state": enum(r.get("state"), ("ACTIVE", "INACTIVE")), "imports_custom_routes": boolean(r.get("importCustomRoutes")), "exports_custom_routes": boolean(r.get("exportCustomRoutes"))} for r in peers],
                "producer_network_policy_collected": False}
    if scope == "sql":
        ip = mapping(mapping(body.get("settings", {})).get("ipConfiguration", {}))
        return {"public_ipv4_enabled": boolean(ip.get("ipv4Enabled")), "ssl_mode": enum(ip.get("sslMode"), ("ENCRYPTED_ONLY", "ALLOW_UNENCRYPTED_AND_ENCRYPTED", "TRUSTED_CLIENT_CERTIFICATE_REQUIRED")),
                "authorized_network_entries_reported": len(rows(ip.get("authorizedNetworks", [])))}
    if scope == "bucket":
        return {"public_access_prevention": enum(body.get("public_access_prevention"), ("enforced", "inherited")),
                "uniform_bucket_level_access": boolean(body.get("uniform_bucket_level_access"))}
    if scope.startswith("revision/"):
        return attachment(body.get("spec", {}), mapping(body.get("metadata", {})).get("annotations", {}), config)
    if scope.startswith("service/"):
        template = mapping(mapping(body.get("spec", {})).get("template", {}))
        result = attachment(template.get("spec", {}), mapping(template.get("metadata", {})).get("annotations", {}), config)
        traffic = rows(mapping(body.get("status", {})).get("traffic", []))
        service_name = config["services"][scope.split("/")[1]]["name"]
        if not traffic or len(traffic) > 8 or any(not identifier(r.get("revisionName")) or not r["revisionName"].startswith(service_name + "-") for r in traffic):
            raise ConfigError("unresolved_serving_identity")
        result["traffic"] = [{"revision": r["revisionName"], "percent": integer(r.get("percent", 0), 0, 100), "tagged": bool(r.get("tag"))} for r in traffic]
        if any(r["percent"] is None for r in result["traffic"]) or sum(r["percent"] for r in result["traffic"]) != 100:
            raise ConfigError("unresolved_serving_identity")
        return result
    if scope == "jobs":
        # Handled separately because jobs are a top-level list.
        raise ConfigError("invalid_metadata_shape")
    raise ConfigError("unsupported_scope")


def collect(args):
    config, policy = args.config_data, args.policy_data
    project, region = config["project"], config["region"]
    targets = policy["targets"]
    report = {"version": 1, "project": project, "started_at": datetime.now(timezone.utc).isoformat(),
              "verdict": "NOT_PROVEN", "reachability_verified": False, "effective_iam_verified": False,
              "observations": {}, "unavailable": [], "findings": [],
              "limitations": ["metadata_is_not_a_packet_or_permission_test", "producer_and_regional_firewall_scope_incomplete", "iam_inheritance_deny_pab_and_conditions_not_evaluated", "project_vm_list_excludes_managed_connector_vms", "non_atomic_metadata_snapshot", "no_secret_payload_or_live_redis_command_read"]}

    def observe(scope, command, fields):
        try:
            value = summarize(scope, cloud(args, command, fields), config, policy)
            report["observations"][scope] = value
            return value
        except (ConfigError, KeyError, TypeError, ValueError):
            report["unavailable"].append(scope)
            return None

    observe("redis", ["redis", "instances", "describe", targets["redis"], "--region", region], "state,tier,authEnabled,transitEncryptionMode,port,connectMode,authorizedNetwork")
    observe("connector", ["compute", "networks", "vpc-access", "connectors", "describe", config["shared"]["vpc_connector"], "--region", region], "state,network,ipCidrRange,minInstances,maxInstances")
    observe("network", ["compute", "networks", "describe", targets["network"]], "autoCreateSubnetworks,networkFirewallPolicyEnforcementOrder,peerings[].state,peerings[].importCustomRoutes,peerings[].exportCustomRoutes")
    observe("classic_firewalls", ["compute", "firewall-rules", "list", "--filter", f"network:projects/{project}/global/networks/{targets['network']}"], "network,name,direction,priority,disabled,sourceRanges,destinationRanges,sourceTags,targetTags,sourceServiceAccounts,targetServiceAccounts,allowed,denied")
    observe("effective_firewalls", ["compute", "networks", "get-effective-firewalls", targets["network"]], "type,name,direction,priority,disabled,action,ip_ranges,target_tags,target_svc_acct")
    observe("vms", ["compute", "instances", "list"], "networkInterfaces[].network,networkInterfaces[].accessConfigs[].type,serviceAccounts[].email")
    observe("sql", ["sql", "instances", "describe", config["database"]["instance"]], "settings.ipConfiguration.ipv4Enabled,settings.ipConfiguration.sslMode,settings.ipConfiguration.authorizedNetworks[].value")
    observe("bucket", ["storage", "buckets", "describe", "gs://" + targets["media_bucket"]], "public_access_prevention,uniform_bucket_level_access")
    observe("iam/project", ["projects", "get-iam-policy", project], IAM_FIELDS)
    observe("iam/bucket", ["storage", "buckets", "get-iam-policy", "gs://" + targets["media_bucket"]], IAM_FIELDS)
    secrets = set(config["shared"]["secrets"].values())
    for service in config["services"].values():
        secrets.update(service.get("secrets", {}).values())
    for reference in sorted(secrets):
        secret = reference.split(":")[0]
        observe("iam/secret/" + secret, ["secrets", "get-iam-policy", secret], IAM_FIELDS)
    for role, service in config["services"].items():
        name = service["name"]
        fields = ["spec.template.spec.serviceAccountName", "status.traffic[].revisionName", "status.traffic[].percent", "status.traffic[].tag"] + ['spec.template.metadata.annotations."' + k + '"' for k in NETWORK_ANN]
        value = observe("service/" + role, ["run", "services", "describe", name, "--region", region], ",".join(fields))
        observe("iam/service/" + role, ["run", "services", "get-iam-policy", name, "--region", region], IAM_FIELDS)
        if value:
            for rev in sorted({row["revision"] for row in value["traffic"]}):
                observe("revision/" + rev, ["run", "revisions", "describe", rev, "--region", region], ",".join(["spec.serviceAccountName"] + ['metadata.annotations."' + k + '"' for k in NETWORK_ANN]))
    # Jobs may have a narrower identity or no broker attachment; report accurately,
    # without declaring service defaults necessary for every job.
    try:
        fields = "metadata.name,spec.template.spec.template.spec.serviceAccountName," + ",".join('spec.template.metadata.annotations."' + k + '"' for k in NETWORK_ANN)
        jobs = rows(cloud(args, ["run", "jobs", "list", "--region", region], fields))
        result = {"known": {}, "other_job_count": 0}
        for job in jobs:
            name = mapping(job.get("metadata", {})).get("name")
            if name not in config["jobs"]:
                result["other_job_count"] += 1
                continue
            template = mapping(mapping(job.get("spec", {})).get("template", {}))
            spec = mapping(mapping(mapping(template.get("spec", {})).get("template", {})).get("spec", {}))
            result["known"][name] = attachment(spec, mapping(template.get("metadata", {})).get("annotations", {}), config)
        report["observations"]["jobs"] = result
    except (ConfigError, KeyError, TypeError, ValueError):
        report["unavailable"].append("jobs")
    assess(report, config, policy)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["metadata_complete_for_requested_scopes"] = not report["unavailable"]
    return report


def assess(report, config, policy):
    observed = report["observations"]

    def note(scope, issue):
        report["findings"].append({"scope": scope, "issue": issue})

    redis = observed.get("redis", {})
    if redis.get("auth_enabled") is not True:
        note("redis", "auth_not_observed_enabled")
    if redis.get("transit_encryption_mode") != "SERVER_AUTHENTICATION":
        note("redis", "tls_replacement_and_staging_required")
    if redis.get("transit_encryption_mode") == "SERVER_AUTHENTICATION" and redis.get("port") != 6378:
        note("redis", "tls_port_unexpected")
    for scope in ("redis", "connector"):
        row = observed.get(scope, {})
        if row.get("state") != "READY" or row.get("review_network_matches") is not True:
            note(scope, "attachment_or_readiness_unproven")
    if observed.get("sql", {}).get("ssl_mode") != policy["preserve_controls"]["sql_ssl_mode"]:
        note("sql", "encrypted_only_control_not_confirmed")
    if observed.get("sql", {}).get("authorized_network_entries_reported", 0) > 0:
        note("sql", "authorized_network_entries_require_review")
    bucket = observed.get("bucket", {})
    if bucket.get("public_access_prevention") != "enforced" or bucket.get("uniform_bucket_level_access") is not True:
        note("bucket", "private_bucket_controls_not_confirmed")
    for scope, row in observed.items():
        if scope.startswith(("service/", "revision/")):
            if not row["connector_matches_source"] or row["egress"] != config["shared"]["vpc_egress"]:
                note(scope, "network_attachment_differs_from_source")
            if not row["shared_runtime_identity"]:
                note(scope, "runtime_identity_differs_from_source")
            if not row["cloud_sql_matches_source"]:
                note(scope, "sql_attachment_differs_from_source")
        if scope.startswith("iam/"):
            for grant in row["direct_bindings"]:
                if grant["shared_runtime_member"] and grant["role"] in ("roles/owner", "roles/editor", "roles/secretmanager.admin", "roles/storage.admin", "roles/cloudsql.admin"):
                    note(scope, "broad_runtime_grant_requires_review")
                if (grant["all_users_member"] or grant["all_authenticated_users_member"]) and not (scope.startswith("iam/service/") and grant["role"] == "roles/run.invoker"):
                    note(scope, "public_principal_grant_requires_review")
    for row in observed.get("classic_firewalls", []):
        if row["disabled"] is False and row["direction"] == "INGRESS" and row["source_or_destination_ranges"]["source"]["includes_anywhere"] and not row["target_tag_count"] and not row["target_identity_count"]:
            if any(rule["action"] == "ALLOW" for rule in row["rules"]):
                note("classic_firewalls", "broad_unscoped_ingress_rule_not_reachability_proof")
                break


def main(argv=None):
    try:
        parser = SafeParser(description=__doc__)
        parser.add_argument("--config", type=Path, default=ROOT / "infra/deployment.json")
        parser.add_argument("--policy", type=Path, default=ROOT / "infra/network-review.json")
        parser.add_argument("--gcloud", default="gcloud")
        parser.add_argument("--report", type=Path, required=True)
        args = parser.parse_args(argv)
        args.config_data, args.policy_data = load(args.config), load(args.policy)
        validate(args.config_data)
        review_policy(args.policy_data)
        report = collect(args)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"verdict": report["verdict"], "unavailable_scopes": len(report["unavailable"]), "finding_count": len(report["findings"]), "reachability_verified": False, "effective_iam_verified": False}))
        # Even a complete inventory cannot close staged access acceptance.
        return 2
    except (ConfigError, OSError, ValueError, TypeError, KeyError):
        print('{"verdict":"NOT_PROVEN","error":"network_evidence_unavailable"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
