"""Falsify sanitized metadata collection; no cloud credentials or live endpoints."""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    import network_audit as N
finally:
    sys.path.pop(0)

PRIVATE = "private-canary@example.invalid"


@pytest.fixture
def cfg():
    return N.load(ROOT / "infra/deployment.json")


@pytest.fixture
def policy():
    return N.load(ROOT / "infra/network-review.json")


def attached(cfg):
    return {"serviceAccountName": cfg["shared"]["service_account"]}, dict(zip(N.NETWORK_ANN, [cfg["shared"]["vpc_connector"], "private-ranges-only", cfg["shared"]["cloud_sql"]]))


def fake_provider(cfg):
    spec, ann = attached(cfg)
    iam = {"etag": "ignored", "bindings": [{"role": "roles/secretmanager.secretAccessor", "members": ["serviceAccount:" + cfg["shared"]["service_account"], "user:" + PRIVATE], "condition": {"expression": PRIVATE}}]}

    def request(args, command, fields):
        assert fields and "--project" not in command  # cloud() adds explicit config project.
        if "get-iam-policy" in command:
            return copy.deepcopy(iam)
        if command[:2] == ["redis", "instances"]:
            return {"state": "READY", "tier": "BASIC", "authEnabled": True, "transitEncryptionMode": "SERVER_AUTHENTICATION", "port": 6378, "authorizedNetwork": "projects/chirps-prod/global/networks/default", "connectMode": "DIRECT_PEERING", "authString": PRIVATE}
        if "connectors" in command:
            return {"state": "READY", "network": "default", "ipCidrRange": "10.8.0.0/28", "minInstances": 2, "maxInstances": 3}
        if "get-effective-firewalls" in command:
            return [{"type": "network-firewall", "name": "aet-hidden-connector-rule", "direction": "INGRESS", "priority": 100, "action": "ALLOW", "target_tags": [PRIVATE], "ip_ranges": ["10.8.0.0/28"], "description": PRIVATE}]
        if "firewall-rules" in command:
            return [{"name": "default-allow-ssh", "network": "projects/chirps-prod/global/networks/default", "direction": "INGRESS", "priority": 65534, "sourceRanges": ["0.0.0.0/0"], "allowed": [{"IPProtocol": "tcp", "ports": ["22"]}]}]
        if command[:3] == ["compute", "networks", "describe"]:
            return {"autoCreateSubnetworks": True, "networkFirewallPolicyEnforcementOrder": "AFTER_CLASSIC_FIREWALL", "peerings": [{"name": PRIVATE, "network": PRIVATE, "state": "ACTIVE"}]}
        if command[:2] == ["compute", "instances"]:
            return []
        if command[0] == "sql":
            return {"settings": {"ipConfiguration": {"ipv4Enabled": True, "sslMode": "ENCRYPTED_ONLY"}}}
        if command[0] == "storage":
            return {"public_access_prevention": "enforced", "uniform_bucket_level_access": True}
        if command[:2] == ["run", "services"]:
            return {"spec": {"template": {"spec": spec, "metadata": {"annotations": ann}}}, "status": {"traffic": [{"revisionName": command[3] + "-0001-test", "percent": 100}]}}
        if command[:2] == ["run", "revisions"]:
            return {"spec": spec, "metadata": {"annotations": ann}}
        if command[:2] == ["run", "jobs"]:
            return [{"metadata": {"name": name}, "spec": {"template": {"metadata": {"annotations": ann}, "spec": {"template": {"spec": spec}}}}} for name in cfg["jobs"]]
        raise AssertionError(command)
    return request


def collect(monkeypatch, cfg, policy, request=None):
    monkeypatch.setattr(N, "cloud", request or fake_provider(cfg))
    result = N.collect(argparse.Namespace(config_data=cfg, policy_data=policy, gcloud="unused"))
    assert PRIVATE not in json.dumps(result)
    return result


def test_full_inventory_never_claims_effective_access(monkeypatch, cfg, policy):
    report = collect(monkeypatch, cfg, policy)
    assert report["metadata_complete_for_requested_scopes"]
    assert report["verdict"] == "NOT_PROVEN"
    assert not report["reachability_verified"] and not report["effective_iam_verified"]
    assert report["observations"]["vms"]["project_vm_count"] == 0
    assert report["observations"]["effective_firewalls"][0]["label"] == "connector_name_pattern"
    assert not report["observations"]["effective_firewalls"][0]["protocol_details_collected"]
    assert any(r["issue"] == "broad_unscoped_ingress_rule_not_reachability_proof" for r in report["findings"])


def test_conditional_and_inherited_policy_not_granted(cfg):
    result = N.iam_summary({"bindings": [{"role": "roles/secretmanager.secretAccessor", "members": ["serviceAccount:" + cfg["shared"]["service_account"], PRIVATE], "condition": {"expression": PRIVATE}}]}, cfg)
    row = result["direct_bindings"][0]
    assert row["shared_runtime_member"] and row["condition_present"] and row["member_count"] == 2
    assert result["effective_access_verified"] is False
    assert PRIVATE not in json.dumps(result)


def test_unknown_roles_principals_and_conditions_are_not_serialized(cfg):
    result = N.iam_summary({"bindings": [{"role": PRIVATE, "members": [PRIVATE], "condition": {"expression": PRIVATE}}]}, cfg)
    assert result["unknown_role_bindings"] == 1
    assert PRIVATE not in json.dumps(result)


@pytest.mark.parametrize("auth", [None, False, "true", 1])
def test_omitted_or_wrong_type_auth_is_not_enabled(cfg, policy, auth):
    body = {} if auth is None else {"authEnabled": auth}
    observed = N.summarize("redis", body, cfg, policy)
    report = {"observations": {"redis": observed}, "findings": []}
    N.assess(report, cfg, policy)
    assert any(r["issue"] == "auth_not_observed_enabled" for r in report["findings"])


@pytest.mark.parametrize("mode", [None, "DISABLED", PRIVATE])
def test_tls_off_unknown_or_missing_remains_followup(cfg, policy, mode):
    row = N.summarize("redis", {"transitEncryptionMode": mode}, cfg, policy)
    report = {"observations": {"redis": row}, "findings": []}
    N.assess(report, cfg, policy)
    assert any(r["issue"] == "tls_replacement_and_staging_required" for r in report["findings"])
    assert PRIVATE not in json.dumps(report)


@pytest.mark.parametrize("scope,payload,issue", [
    ("sql", {"ssl_mode": "ALLOW_UNENCRYPTED_AND_ENCRYPTED"}, "encrypted_only_control_not_confirmed"),
    ("sql", {"ssl_mode": "ENCRYPTED_ONLY", "authorized_network_entries_reported": 1}, "authorized_network_entries_require_review"),
    ("bucket", {"public_access_prevention": "inherited", "uniform_bucket_level_access": True}, "private_bucket_controls_not_confirmed"),
    ("bucket", {"public_access_prevention": "enforced", "uniform_bucket_level_access": False}, "private_bucket_controls_not_confirmed"),
])
def test_existing_privacy_controls_cannot_silently_regress(cfg, policy, scope, payload, issue):
    report = {"observations": {scope: payload}, "findings": []}
    N.assess(report, cfg, policy)
    assert any(r["issue"] == issue for r in report["findings"])


def test_service_template_does_not_hide_wrong_serving_attachment(monkeypatch, cfg, policy):
    base = fake_provider(cfg)
    def wrong(args, command, fields):
        result = base(args, command, fields)
        if command[:2] == ["run", "revisions"]:
            result["metadata"]["annotations"] = {N.NETWORK_ANN[0]: "unrelated"}
        return result
    report = collect(monkeypatch, cfg, policy, wrong)
    assert any(r["scope"].startswith("revision/") and r["issue"] == "network_attachment_differs_from_source" for r in report["findings"])


@pytest.mark.parametrize("traffic", [[{"percent": 100}], [{"revisionName": PRIVATE, "percent": 100}], [{"revisionName": "chirp-api-one", "percent": 99}], []])
def test_unresolved_or_malformed_traffic_is_incomplete(monkeypatch, cfg, policy, traffic):
    base = fake_provider(cfg)
    def wrong(args, command, fields):
        result = base(args, command, fields)
        if command[:2] == ["run", "services"] and "get-iam-policy" not in command:
            result["status"]["traffic"] = traffic
        return result
    report = collect(monkeypatch, cfg, policy, wrong)
    assert not report["metadata_complete_for_requested_scopes"]
    assert "service/api" in report["unavailable"]


def test_tagged_zero_traffic_revision_is_also_inspected(monkeypatch, cfg, policy):
    base = fake_provider(cfg)
    seen = []
    def request(args, command, fields):
        result = base(args, command, fields)
        if command[:2] == ["run", "services"] and "get-iam-policy" not in command:
            result["status"]["traffic"].append({"revisionName": command[3] + "-tagged", "percent": 0, "tag": PRIVATE})
        if command[:2] == ["run", "revisions"]:
            seen.append(command[3])
        return result
    report = collect(monkeypatch, cfg, policy, request)
    assert "chirp-api-tagged" in seen and "chirp-ws-tagged" in seen
    assert report["metadata_complete_for_requested_scopes"]


@pytest.mark.parametrize("value", [[None], {"firewalls": []}, PRIVATE])
def test_wrong_effective_firewall_projection_is_not_complete(monkeypatch, cfg, policy, value):
    base = fake_provider(cfg)
    def request(args, command, fields):
        return value if "get-effective-firewalls" in command else base(args, command, fields)
    result = collect(monkeypatch, cfg, policy, request)
    assert "effective_firewalls" in result["unavailable"]


def test_denied_metadata_is_redacted_and_other_scopes_continue(monkeypatch, cfg, policy):
    base = fake_provider(cfg)
    def denied(args, command, fields):
        if "get-iam-policy" in command:
            raise N.ConfigError(PRIVATE)
        return base(args, command, fields)
    result = collect(monkeypatch, cfg, policy, denied)
    assert "redis" in result["observations"] and "iam/project" in result["unavailable"]
    assert not result["metadata_complete_for_requested_scopes"]


def test_cross_project_default_is_not_same_network(cfg):
    assert not N.network_matches("projects/other-project/global/networks/default", cfg, "default")


def test_missing_identity_cannot_be_called_distinct(cfg):
    result = N.attachment({}, {}, cfg)
    assert not result["runtime_identity_observed"] and result["shared_runtime_identity"] is None
    result = N.attachment({"serviceAccountName": PRIVATE}, {}, cfg)
    assert result["runtime_identity_observed"] and result["shared_runtime_identity"] is False
    assert PRIVATE not in json.dumps(result)


@pytest.mark.parametrize("binding", [{"role": "roles/editor"}, {"members": []}])
def test_malformed_iam_binding_not_empty_grant(cfg, binding):
    with pytest.raises(N.ConfigError):
        N.iam_summary({"bindings": [binding]}, cfg)


def test_broad_runtime_and_public_secret_grants_reported(cfg, policy):
    iam = N.iam_summary({"bindings": [{"role": "roles/editor", "members": ["serviceAccount:" + cfg["shared"]["service_account"]]}, {"role": "roles/secretmanager.secretAccessor", "members": ["allUsers"]}]}, cfg)
    result = {"observations": {"iam/project": iam}, "findings": []}
    N.assess(result, cfg, policy)
    assert {r["issue"] for r in result["findings"]} >= {"broad_runtime_grant_requires_review", "public_principal_grant_requires_review"}


def test_public_invoker_expected_but_public_bucket_flagged(cfg, policy):
    iam = N.iam_summary({"bindings": [{"role": "roles/run.invoker", "members": ["allUsers"]}]}, cfg)
    result = {"observations": {"iam/service/api": iam, "iam/bucket": iam}, "findings": []}
    N.assess(result, cfg, policy)
    assert not any(r["scope"] == "iam/service/api" for r in result["findings"])
    assert any(r["scope"] == "iam/bucket" for r in result["findings"])


def test_real_cli_redacts_provider_failure_and_has_explicit_read_only_commands(tmp_path):
    fake = tmp_path / "gcloud"
    calls = tmp_path / "calls.jsonl"
    fake.write_text("#!/usr/bin/env python3\nimport json,sys\nwith open(" + repr(str(calls)) + ", 'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\nprint(" + repr(PRIVATE) + ")\nprint(" + repr(PRIVATE) + ", file=sys.stderr)\nsys.exit(1)\n")
    fake.chmod(0o700)
    report = tmp_path / "safe.json"
    run = subprocess.run([sys.executable, str(ROOT / "scripts/network_audit.py"), "--gcloud", str(fake), "--report", str(report)], capture_output=True, text=True, timeout=15)
    assert run.returncode == 2 and PRIVATE not in run.stdout + run.stderr + report.read_text()
    for line in calls.read_text().splitlines():
        argv = json.loads(line)
        assert argv[argv.index("--project")+1] == "chirps-prod"
        assert argv[argv.index("--format")+1].startswith("json(")
        assert not set(argv) & {"update", "create", "delete", "access", "get-auth-string", "set-iam-policy", "add-iam-policy-binding"}


def test_review_policy_rejects_changed_privacy_intent(policy):
    N.review_policy(policy)
    policy["preserve_controls"]["sql_ssl_mode"] = "ALLOW_UNENCRYPTED_AND_ENCRYPTED"
    with pytest.raises(N.ConfigError):
        N.review_policy(policy)


def test_target_list_is_rejected_without_attribute_error(policy):
    policy["targets"] = ["network", "redis", "media_bucket"]
    with pytest.raises(N.ConfigError):
        N.review_policy(policy)


def test_sql_followup_artifact_is_numeric_and_has_no_private_path():
    row = json.loads((ROOT / "infra/evidence/c362-sql-2026-09-08.json").read_text())
    assert (row["max_connections"], row["superuser_reserved_connections"], row["reserved_connections"]) == (100, 3, 0)
    assert row["provenance"]["proxy_target_verified"] is True
    assert not any(token in json.dumps(row).lower() for token in ("password", "postgresql://", "infra-private", "@", "/users/"))
