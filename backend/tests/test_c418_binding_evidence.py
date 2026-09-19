"""Literal membership cannot stand in for an operator's effective-binding review."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

from tests.test_c362_deployment_config import C, NOW, config, release, snap, run_compare, finding

_path = Path(__file__).resolve().parents[2] / "scripts/tests/test_c418_compiled_endpoints.py"
_spec = importlib.util.spec_from_file_location("c418_binding_inventory_fixture", _path)
_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixtures)

LEGACY_FIELDS = {"build_id", "observed_at", "api_url", "ws_url"}
PROVENANCE_FIELDS = (
    "evidence_scope", "binding_method", "artifact_sha256", "bundle_sha256",
    "launch_bundle_path", "binding_evidence_sha256",
)


def test_complete_operator_observation_retains_provenance_without_claiming_inspection(config, snap, release):
    result = run_compare(config, snap, release)
    assert result["verdict"] == "CONFIG_MATCH"
    evidence = result["client_binding_evidence"]
    assert evidence["accepted_operator_observation"] is True
    assert evidence["artifact_inspected_by_checker"] is False
    assert evidence["artifact_sha256"] == release["client_build"]["artifact_sha256"]
    assert evidence["binding_evidence_sha256"] == release["client_build"]["binding_evidence_sha256"]
    assert result["authenticated_ready"] is False


@pytest.mark.parametrize("missing", ["all_legacy", *PROVENANCE_FIELDS])
def test_legacy_or_incomplete_binding_observation_is_not_accepted(config, snap, release, missing):
    if missing == "all_legacy":
        release["client_build"] = {k: v for k, v in release["client_build"].items() if k in LEGACY_FIELDS}
    else:
        release["client_build"].pop(missing)
    result = run_compare(config, snap, release)
    assert result["verdict"] == "NOT_PROVEN"
    assert finding(result, "compiled_build_evidence_missing_or_mismatched")
    assert result["client_binding_evidence"]["accepted_operator_observation"] is False


@pytest.mark.parametrize("field,value", [
    ("evidence_scope", "compiled_artifact_string_table_observation_not_a_device_test"),
    ("binding_method", "string_table_membership"),
    ("artifact_sha256", "not-a-hash"), ("bundle_sha256", "a" * 63),
    ("binding_evidence_sha256", None),
    ("launch_bundle_path", "Payload/../main.jsbundle"),
    ("launch_bundle_path", "Payload/chirp.app/unused.jsbundle"),
    ("launch_bundle_path", None),
    ("api_url", "https://alternative.invalid"), ("ws_url", "wss://alternative.invalid/ws"),
])
def test_wrong_scope_method_hash_member_or_endpoint_stays_unproven(config, snap, release, field, value):
    release["client_build"][field] = value
    result = run_compare(config, snap, release)
    assert result["verdict"] == "NOT_PROVEN"
    assert finding(result, "compiled_build_evidence_missing_or_mismatched")


@pytest.mark.parametrize("malformed", [None, [], "legacy-record"])
def test_malformed_client_observation_is_a_finding(config, snap, release, malformed):
    release["client_build"] = malformed
    assert finding(run_compare(config, snap, release), "compiled_build_evidence_missing_or_mismatched")


def test_actual_extractor_inventory_and_old_four_field_projection_cannot_pass_comparison(tmp_path, config, snap, release):
    artifact = tmp_path / "chirp.ipa"
    api = config["services"]["api"]["client_origin"]
    ws = config["services"]["ws"]["client_origin"].replace("https:", "wss:") + "/ws"
    artifact.write_bytes(_fixtures.ipa(_fixtures.hermes_bundle([api, ws, "https://alternative.invalid"])))
    inventory = _fixtures.ce.observe(artifact, "ambiguous-build", config, NOW)
    assert inventory["verdict"] == "NOT_PROVEN" and "client_build" not in inventory
    assert inventory["literal_status"] == "EXPECTED_LITERALS_PRESENT"
    for candidate in (
        inventory["literal_observation"], inventory,
        {"build_id": "ambiguous-build", "observed_at": NOW.isoformat(), "api_url": api, "ws_url": ws},
    ):
        attempt = copy.deepcopy(release)
        attempt["client_build"] = candidate
        result = run_compare(config, snap, attempt)
        assert result["verdict"] == "NOT_PROVEN", json.dumps(result)
        assert finding(result, "compiled_build_evidence_missing_or_mismatched")
