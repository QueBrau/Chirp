"""Execute configuration comparison/command generation; never contact a provider."""
from __future__ import annotations
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    SPEC = importlib.util.spec_from_file_location("c362", ROOT / "scripts/deployment_config.py")
    C = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(C)
finally:
    sys.path.pop(0)
SECRET = "never-copy-this-private-provider-value"
NOW = datetime.now(timezone.utc)


@pytest.fixture
def config():
    return C.load(ROOT / "infra/deployment.json")


@pytest.fixture
def release(config):
    return {"image": config["image_repository"] + "@sha256:" + "a" * 64,
            "schema_head": "test_schema_head", "revisions": {role: row["name"] + "-release" for role, row in config["services"].items()},
            "database_observation": {"observed_at": NOW.isoformat(), "max_connections": 100, "superuser_reserved_connections": 3, "reserved_connections": 0},
            "client_build": {"build_id": "reviewed-build", "observed_at": NOW.isoformat(), "api_url": config["services"]["api"]["client_origin"], "ws_url": config["services"]["ws"]["client_origin"].replace("https:", "wss:") + "/ws",
                             "evidence_scope": "operator_reviewed_effective_compiled_endpoints",
                             "binding_method": "launch_bundle_assignment_review",
                             "artifact_sha256": "d" * 64, "bundle_sha256": "e" * 64,
                             "launch_bundle_path": "Payload/chirp.app/main.jsbundle",
                             "binding_evidence_sha256": "f" * 64}}


def fixture(config, release):
    shared = config["shared"]
    result = {"services": {}, "revisions": {}, "jobs": {}, "database": {"settings": {"tier": config["database"]["tier"]}}}
    for role, service in config["services"].items():
        name, revision = service["name"], release["revisions"][role]
        env = shared["env"] | service["env"] | {"WEB_CONCURRENCY": "1"}
        container = {"image": release["image"], "resources": {"limits": {"cpu": service["cpu"], "memory": service["memory"]}}, "env": [{"name": k, "value": v} for k, v in env.items()] + [{"name": k, "valueFrom": {"secretKeyRef": {"name": v.split(":")[0], "key": v.split(":")[1]}}} for k, v in (shared["secrets"] | service.get("secrets", {})).items()]}
        ann = {"autoscaling.knative.dev/minScale": str(service["revision_min_instances"]), "autoscaling.knative.dev/maxScale": str(service["revision_max_instances"]), "run.googleapis.com/cloudsql-instances": shared["cloud_sql"], "run.googleapis.com/vpc-access-connector": shared["vpc_connector"], "run.googleapis.com/vpc-access-egress": shared["vpc_egress"], "run.googleapis.com/startup-cpu-boost": "true"}
        spec = {"containers": [container], "containerConcurrency": service["concurrency"], "timeoutSeconds": shared["timeout_seconds"], "serviceAccountName": shared["service_account"]}
        ready = {"observedGeneration": 1, "conditions": [{"type": "Ready", "status": "True"}]}
        canonical = "https://" + name + "-example-uc.a.run.app"
        result["services"][role] = {"metadata": {"name": name, "generation": 1, "annotations": {"run.googleapis.com/maxScale": "20", "run.googleapis.com/ingress": "all", "run.googleapis.com/urls": json.dumps([canonical, service["client_origin"]])}}, "spec": {"template": {"metadata": {"annotations": copy.deepcopy(ann)}, "spec": copy.deepcopy(spec)}}, "status": ready | {"url": canonical, "traffic": [{"revisionName": revision, "percent": 100}], "latestCreatedRevisionName": revision, "latestReadyRevisionName": revision}}
        result["revisions"][revision] = {"metadata": {"name": revision, "generation": 1, "labels": {"serving.knative.dev/service": name}, "annotations": ann}, "spec": spec, "status": ready | {"imageDigest": release["image"]}}
    for name in config["jobs"]:
        result["jobs"][name] = {"metadata": {"name": name}, "spec": {"template": {"spec": {"taskCount": 1, "parallelism": 1, "template": {"spec": {"containers": [{"image": config["image_repository"] + "@sha256:" + "b" * 64, "command": ["python"], "args": ["-m", config["jobs"][name]["module"]], "env": [{"name": "DB_POOL_SIZE", "value": "3"}, {"name": "DB_MAX_OVERFLOW", "value": "2"}]}]}}}}}}
    result["services_after"] = copy.deepcopy(result["services"])
    return result


@pytest.fixture
def snap(config, release):
    return fixture(config, release)


def run_compare(config, snap, release):
    snap["services_after"] = copy.deepcopy(snap["services"])
    report = C.compare(config, snap, release, NOW)
    assert SECRET not in json.dumps(report)
    return report


def finding(report, field):
    return any(row["field"] == field for row in report["findings"])


def test_actual_policy_serialized_vs_unconstrained(config):
    C.validate(config)
    envelope = C.pool_envelope(config, C.defaults())
    assert envelope["steady_with_jobs"] == 68
    assert envelope["serialized_rollout_jobs_quiescent"] == 98
    assert envelope["simultaneous_rollout_with_jobs"] == 112
    assert envelope["policy_rollout_fits"] and not envelope["simultaneous_rollout_fits"]
    assert not envelope["hard_capacity_guarantee"]


@pytest.mark.parametrize("field,value", [("revision_max_instances", 9), ("workers", 2)])
def test_expanding_policy_breaks_pool_guard(config, field, value):
    config["services"]["api"][field] = value
    assert not C.pool_envelope(config, C.defaults())["policy_rollout_fits"]


def test_job_execution_and_parallelism_multiply(config):
    config["jobs"]["chirp-purge"].update(parallelism=3, max_overlapping_executions=2, workers=2)
    assert C.pool_envelope(config, C.defaults())["job_connections"] == 65
    assert not C.pool_envelope(config, C.defaults())["steady_fits"]


def test_complete_config_match_is_not_authenticated_readiness(config, snap, release):
    report = run_compare(config, snap, release)
    assert report["findings"] == [] and report["verdict"] == "CONFIG_MATCH"
    assert report["authenticated_ready"] is False
    assert report["pool_envelope"]["listed_revisions_plus_jobs_and_reserves"] == 68


@pytest.mark.parametrize("field", ["expected_release", "database_observation", "client_build"])
def test_missing_evidence_is_not_green(config, snap, release, field):
    if field == "expected_release": release = None
    else: release.pop(field)
    assert run_compare(config, snap, release)["verdict"] == "NOT_PROVEN"


@pytest.mark.parametrize("field", ["database_observation", "client_build"])
@pytest.mark.parametrize("age", [timedelta(days=2), timedelta(minutes=-1)])
def test_stale_or_future_evidence(config, snap, release, field, age):
    release[field]["observed_at"] = (NOW-age).isoformat()
    assert run_compare(config, snap, release)["verdict"] == "NOT_PROVEN"


def test_numeric_url_needs_published_alias(config, snap, release):
    body = snap["services"]["api"]
    body["metadata"]["annotations"]["run.googleapis.com/urls"] = json.dumps([body["status"]["url"]])
    report = run_compare(config, snap, release)
    assert finding(report, "published_client_origin") and report["verdict"] == "DRIFT"


def test_unrelated_canonical_origin_rejected(config, snap, release):
    snap["services"]["api"]["status"]["url"] = "https://unrelated.example"
    assert finding(run_compare(config, snap, release), "published_client_origin")


@pytest.mark.parametrize("field,value", [("containerConcurrency", 800), ("timeoutSeconds", 300), ("serviceAccountName", SECRET)])
def test_template_drift_not_hidden_by_serving_image(config, snap, release, field, value):
    snap["services"]["api"]["spec"]["template"]["spec"][field] = value
    report = run_compare(config, snap, release)
    assert finding(report, field) and report["verdict"] == "DRIFT"


def test_serving_revision_config_is_independent(config, snap, release):
    snap["revisions"][release["revisions"]["api"]]["metadata"]["annotations"]["autoscaling.knative.dev/maxScale"] = "25"
    report = run_compare(config, snap, release)
    assert report["verdict"] == "DRIFT"
    assert report["pool_envelope"]["listed_revisions_configured_capacity"] == 129


def test_template_image_drift(config, snap, release):
    snap["services"]["api"]["spec"]["template"]["spec"]["containers"][0]["image"] = config["image_repository"] + "@sha256:" + "c"*64
    assert run_compare(config, snap, release)["verdict"] == "DRIFT"


def test_cpu_millicores_equivalent(config, snap, release):
    snap["revisions"][release["revisions"]["ws"]]["spec"]["containers"][0]["resources"]["limits"]["cpu"] = "1000m"
    assert run_compare(config, snap, release)["verdict"] == "CONFIG_MATCH"


def test_missing_pool_env_inferred_not_proven(config, snap, release):
    row = snap["revisions"][release["revisions"]["api"]]["spec"]["containers"][0]
    row["env"] = [v for v in row["env"] if v["name"] not in ("DB_POOL_SIZE", "DB_MAX_OVERFLOW")]
    report = run_compare(config, snap, release)
    assert finding(report, "pool_inferred_from_checkout_not_image") and report["verdict"] == "NOT_PROVEN"


JOB = "chirp-media-reconcile"


def job_container(snap, name=JOB):
    return snap["jobs"][name]["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]


def add_job_pool_observation(config, snap, release, *, size=5, overflow=10):
    container = job_container(snap)
    row = {"version": 1, "observed_at": NOW.isoformat(), "project": config["project"],
           "region": config["region"], "image": container["image"],
           "command": copy.deepcopy(container["command"]), "args": copy.deepcopy(container["args"]),
           "pool_env": {key: next((v["value"] for v in container["env"] if v["name"] == key), None)
                        for key in ("DB_POOL_SIZE", "DB_MAX_OVERFLOW")},
           "pool": {"size": size, "max_overflow": overflow}, "evidence_sha256": "c" * 64,
           "evidence_scope": "operator_inspected_immutable_image_pool"}
    release["job_pool_observations"] = {JOB: row}
    return row


def test_older_job_effective_engine_pool_changes_observed_envelope_only(config, snap, release):
    # An older engine can hardcode 5+10 despite the checkout's current 3+2.
    job_container(snap)["env"] = []
    job_container(snap, "chirp-purge")["env"] = [
        {"name": "DB_POOL_SIZE", "value": "1"}, {"name": "DB_MAX_OVERFLOW", "value": "0"}]
    before = run_compare(config, snap, release)
    planned = C.plan(config, release, "gcloud")
    assert before["verdict"] == "NOT_PROVEN"
    assert before["pool_envelope"]["steady_with_jobs"] == 64
    add_job_pool_observation(config, snap, release)
    report = run_compare(config, snap, release)
    assert report["verdict"] == "CONFIG_MATCH"
    assert report["jobs"][JOB]["pool_capacity_per_process"] == 15
    assert report["pool_envelope"]["steady_with_jobs"] == 74
    assert report["pool_envelope"]["serialized_rollout_jobs_quiescent"] == 98
    assert report["pool_envelope"]["simultaneous_rollout_with_jobs"] == 118
    assert report["pool_envelope"]["listed_revisions_plus_jobs_and_reserves"] == 74
    assert C.plan(config, release, "gcloud") == planned
    job = report["jobs"][JOB]
    assert job["inferred_from_checkout_not_image"] == ["DB_POOL_TIMEOUT", "WEB_CONCURRENCY"]
    assert set(job["pool_input_sources"].values()) == {"operator_supplied_image_pool_observation"}
    assert job["pool_observation"]["evidence_sha256"] == "c" * 64
    assert job["pool_observation"]["image_inspected_by_checker"] is False
    assert report["authenticated_ready"] is False


def test_job_observation_fills_only_missing_pool_input(config, snap, release):
    job_container(snap)["env"] = [{"name": "DB_POOL_SIZE", "value": "3"}]
    add_job_pool_observation(config, snap, release, size=3)
    report = run_compare(config, snap, release)
    job = report["jobs"][JOB]
    assert report["verdict"] == "CONFIG_MATCH"
    assert job["pool_capacity_per_process"] == 13
    assert job["pool_input_sources"] == {"DB_POOL_SIZE": "live_literal_environment",
                                         "DB_MAX_OVERFLOW": "operator_supplied_image_pool_observation"}
    assert job["pool_observation"]["filled_environment_inputs"] == ["DB_MAX_OVERFLOW"]
    assert job_container(snap)["env"] == [{"name": "DB_POOL_SIZE", "value": "3"}]


@pytest.mark.parametrize("change", ["image", "command", "args", "environment", "explicit_conflict", "secret", "duplicate", "literal_and_secret", "stale", "future"])
def test_job_observation_binding_failures_never_replace_live_inputs(config, snap, release, change):
    row = add_job_pool_observation(config, snap, release, size=3, overflow=2)
    container = job_container(snap)
    if change == "image": row["image"] = config["image_repository"] + "@sha256:" + "d" * 64
    if change == "command": row["command"] = ["python3"]
    if change == "args": row["args"] += ["--limit=1"]
    if change == "environment": row["pool_env"]["DB_POOL_SIZE"] = None
    if change == "explicit_conflict": row["pool"]["size"] = 1
    if change == "secret": container["env"][0] = {"name": "DB_POOL_SIZE", "valueFrom": {"secretKeyRef": {"name": SECRET, "key": "latest"}}}
    if change == "duplicate": container["env"].append(copy.deepcopy(container["env"][0]))
    if change == "literal_and_secret": container["env"][0]["valueFrom"] = {"secretKeyRef": {"name": SECRET, "key": "latest"}}
    if change == "stale": row["observed_at"] = (NOW-timedelta(hours=24, microseconds=1)).isoformat()
    if change == "future": row["observed_at"] = (NOW+timedelta(microseconds=1)).isoformat()
    unchanged = copy.deepcopy(container)
    report = run_compare(config, snap, release)
    assert report["verdict"] == "NOT_PROVEN"
    assert finding(report, "job_pool_observation_stale_or_mismatched")
    assert job_container(snap) == unchanged
    assert "pool_observation" not in report["jobs"].get(JOB, {})
    if change not in ("secret", "duplicate"):
        assert report["jobs"][JOB]["pool_capacity_per_process"] == 5


@pytest.mark.parametrize("change", ["extra_job", "wrong_project", "wrong_region", "unknown_key", "missing_key", "version_bool", "version_future", "size_bool", "size_string", "size_zero", "overflow_negative", "overflow_bool", "oversized", "env_string", "env_missing_key", "pool_extra_key", "no_timezone", "non_utc", "bad_date", "bad_sha", "bad_scope", "command_string", "args_empty", "null_observations"])
def test_malformed_job_pool_evidence_has_fixed_diagnostic(config, snap, release, change):
    job_container(snap)["env"] = []
    row = add_job_pool_observation(config, snap, release)
    if change == "extra_job": release["job_pool_observations"][SECRET] = copy.deepcopy(row)
    if change == "wrong_project": row["project"] = SECRET
    if change == "wrong_region": row["region"] = SECRET
    if change == "unknown_key": row[SECRET] = SECRET
    if change == "missing_key": row.pop("pool_env")
    if change == "version_bool": row["version"] = True
    if change == "version_future": row["version"] = 2
    if change == "size_bool": row["pool"]["size"] = True
    if change == "size_string": row["pool"]["size"] = "5"
    if change == "size_zero": row["pool"]["size"] = 0
    if change == "overflow_negative": row["pool"]["max_overflow"] = -1
    if change == "overflow_bool": row["pool"]["max_overflow"] = False
    if change == "oversized": row["pool"]["size"] = 100001
    if change == "env_string": row["pool_env"]["DB_POOL_SIZE"] = SECRET
    if change == "env_missing_key": row["pool_env"].pop("DB_POOL_SIZE")
    if change == "pool_extra_key": row["pool"]["timeout"] = 10
    if change == "no_timezone": row["observed_at"] = "2026-09-14T00:00:00"
    if change == "non_utc": row["observed_at"] = "2026-09-14T01:00:00+01:00"
    if change == "bad_date": row["observed_at"] = "2026-99-14T00:00:00Z"
    if change == "bad_sha": row["evidence_sha256"] = SECRET
    if change == "bad_scope": row["evidence_scope"] = SECRET
    if change == "command_string": row["command"] = SECRET
    if change == "args_empty": row["args"] = []
    if change == "null_observations": release["job_pool_observations"] = None
    with pytest.raises(C.ConfigError) as exc:
        run_compare(config, snap, release)
    assert str(exc.value) == "invalid_job_pool_observation"
    assert SECRET not in str(exc.value)


@pytest.mark.parametrize("other_gap", ["worker", "parallelism", "entrypoint", "service_pool", "client", "sql", "capacity"])
def test_job_pool_observation_preserves_other_gates(config, snap, release, other_gap):
    container = job_container(snap)
    container["env"] = []
    if other_gap == "worker": container["env"].append({"name": "WEB_CONCURRENCY", "value": "2"})
    if other_gap == "parallelism": snap["jobs"][JOB]["spec"]["template"]["spec"].update(taskCount=2, parallelism=2)
    if other_gap == "entrypoint": container["command"] = ["wrapper"]
    if other_gap == "service_pool":
        revision = snap["revisions"][release["revisions"]["api"]]
        revision["spec"]["containers"][0]["env"] = [v for v in revision["spec"]["containers"][0]["env"] if v["name"] != "DB_POOL_SIZE"]
    if other_gap == "client": release.pop("client_build")
    if other_gap == "sql": release.pop("database_observation")
    add_job_pool_observation(config, snap, release, size=100 if other_gap == "capacity" else 5)
    report = run_compare(config, snap, release)
    expected = {"worker": "job_entrypoint_or_worker_model", "parallelism": "job_parallelism",
                "entrypoint": "job_entrypoint_or_worker_model", "service_pool": "pool_inferred_from_checkout_not_image",
                "client": "compiled_build_evidence_missing_or_mismatched", "sql": "current_sql_settings_evidence_missing_or_mismatched",
                "capacity": "intended_pool_envelope_exceeded"}
    assert report["verdict"] != "CONFIG_MATCH"
    assert finding(report, expected[other_gap])


@pytest.mark.parametrize("key,value", [("DB_POOL_SIZE", "0"), ("DB_MAX_OVERFLOW", "-1"), ("WEB_CONCURRENCY", "8"), ("DB_POOL_SIZE", SECRET)])
def test_unbounded_or_changed_capacity_rejected(config, snap, release, key, value):
    env = snap["revisions"][release["revisions"]["api"]]["spec"]["containers"][0]["env"]
    next(row for row in env if row["name"] == key)["value"] = value
    assert run_compare(config, snap, release)["verdict"] == "DRIFT"


def start_rollout(config, snap, release, role="api", old_only=False):
    old = copy.deepcopy(snap["revisions"][release["revisions"][role]])
    name = config["services"][role]["name"] + "-old"
    old["metadata"]["name"] = name
    old["status"]["imageDigest"] = config["image_repository"] + "@sha256:" + "c" * 64
    snap["revisions"][name] = old
    rollout = release.setdefault("rollout", {"started_at": (NOW-timedelta(minutes=5)).isoformat(), "ends_at": (NOW+timedelta(minutes=30)).isoformat(), "jobs_quiescent": True, "previous": {}})
    rollout["previous"][role] = {"revision": name, "image": old["status"]["imageDigest"]}
    status = snap["services"][role]["status"]
    status["traffic"] = [{"revisionName": name, "percent": 100}]
    if old_only:
        status["latestCreatedRevisionName"] = name; status["latestReadyRevisionName"] = name
        snap["services"][role]["spec"]["template"]["spec"]["containers"][0]["image"] = old["status"]["imageDigest"]
    return name


def test_bounded_serial_transition_with_other_service_still_old(config, snap, release):
    start_rollout(config, snap, release)
    start_rollout(config, snap, release, "ws", old_only=True)
    assert run_compare(config, snap, release)["verdict"] == "TRANSITION"


@pytest.mark.parametrize("change", ["expired", "future", "too_long", "missing_window", "unknown_image"])
def test_window_cannot_hide_permanent_or_unexplained_drift(config, snap, release, change):
    start_rollout(config, snap, release)
    row = release["rollout"]
    if change == "expired": row["ends_at"] = NOW.isoformat()
    if change == "future": row["started_at"] = (NOW+timedelta(minutes=1)).isoformat()
    if change == "too_long": row["ends_at"] = (NOW+timedelta(days=1)).isoformat()
    if change == "missing_window": release.pop("rollout")
    if change == "unknown_image": row["previous"]["api"]["image"] = config["image_repository"] + "@sha256:" + "d"*64
    assert run_compare(config, snap, release)["verdict"] == "DRIFT"


def test_two_service_overlap_rejected(config, snap, release):
    start_rollout(config, snap, release); start_rollout(config, snap, release, "ws")
    report = run_compare(config, snap, release)
    assert finding(report, "simultaneous_service_rollout") and report["verdict"] == "DRIFT"


def test_tagged_old_revision_not_excused(config, snap, release):
    start_rollout(config, snap, release)
    rows = snap["services"]["api"]["status"]["traffic"]
    rows[0].update(percent=0, tag="keep-alive")
    rows.append({"revisionName": release["revisions"]["api"], "percent": 100})
    assert run_compare(config, snap, release)["verdict"] == "DRIFT"


def test_transition_requires_jobs_attestation(config, snap, release):
    start_rollout(config, snap, release)
    release["rollout"].pop("jobs_quiescent")
    assert finding(run_compare(config, snap, release), "quiescent_jobs_not_confirmed")


@pytest.mark.parametrize("change", ["generation", "not_ready", "traffic_total", "image"])
def test_bad_readiness_or_release_metadata(config, snap, release, change):
    body = snap["services"]["api"]
    if change == "generation": body["metadata"]["generation"] = 2
    if change == "not_ready": body["status"]["conditions"][0]["status"] = "False"
    if change == "traffic_total": body["status"]["traffic"][0]["percent"] = 99
    if change == "image": snap["revisions"][release["revisions"]["api"]]["status"]["imageDigest"] = config["image_repository"] + "@sha256:" + "e"*64
    assert run_compare(config, snap, release)["verdict"] != "CONFIG_MATCH"


def test_metadata_changed_during_collection(config, snap, release):
    snap["services_after"]["api"]["metadata"]["generation"] = 2
    assert finding(C.compare(config, snap, release, NOW), "metadata_changed_during_observation")


def test_new_job_and_db_flag_not_ignored(config, snap, release):
    snap["jobs"]["other-worker"] = {}
    snap["database"]["settings"]["databaseFlags"] = [{"name": "max_connections", "value": "25"}]
    report = run_compare(config, snap, release)
    assert finding(report, "unmodelled_or_missing_job") and finding(report, "configured_connection_flag")


@pytest.mark.parametrize("api_role,ws_role", [("all", "all"), ("api", "ws"), ("all", "ws"), ("api", "all")])
def test_print_only_plan_derives_both_services_and_verification(config, release, api_role, ws_role):
    config["services"]["api"]["env"]["SERVICE_ROLE"] = api_role
    config["services"]["ws"]["env"]["SERVICE_ROLE"] = ws_role
    plan = C.plan(config, release, "gcloud")
    verification = shlex.split(plan["verification_command"])
    for role, row in zip(("api", "ws"), plan["steps"], strict=True):
        cmd = shlex.split(row["stage_command"])
        assert cmd[cmd.index("--image")+1] == release["image"]
        assert cmd[cmd.index("--max-instances")+1] == str(config["services"][role]["revision_max_instances"])
        assert cmd[cmd.index("--concurrency")+1] == str(config["services"][role]["concurrency"])
        assert "--no-traffic" in cmd and "--timeout" in cmd
        assert "--set-env-vars" not in cmd and "--set-secrets" not in cmd and "--allow-unauthenticated" not in cmd
        assert release["revisions"][role] + "=100" in shlex.split(row["promote_after_review_command"])
        assert config["shared"]["service_account"] in cmd
        expected_role = config["services"][role]["env"]["SERVICE_ROLE"]
        assert "SERVICE_ROLE=" + expected_role in cmd[cmd.index("--update-env-vars")+1].split(",")
        assert verification[verification.index("--" + role + "-expected-role")+1] == expected_role
    assert release["schema_head"] in plan["verification_command"] and "--bearer" not in plan["verification_command"]
    assert plan["mode"] == "PRINT_ONLY_NOT_EXECUTED"


@pytest.mark.parametrize("role,value", [("api", "ws"), ("ws", "api"), ("ws", "worker"), ("api", None)])
def test_plan_requires_explicit_compatible_reviewed_service_roles(config, release, role, value):
    if value is None:
        config["services"][role]["env"].pop("SERVICE_ROLE")
    else:
        config["services"][role]["env"]["SERVICE_ROLE"] = value
    with pytest.raises(C.ConfigError, match="invalid_service_role"):
        C.plan(config, release, "gcloud")


@pytest.mark.parametrize("env", [None, [], "SERVICE_ROLE=all"])
def test_plan_rejects_malformed_service_environment(config, release, env):
    config["services"]["api"]["env"] = env
    with pytest.raises(C.ConfigError, match="invalid_service_role"):
        C.plan(config, release, "gcloud")


def test_plan_rejects_mutable_image_and_unsafe_scale(config, release):
    release["image"] = config["image_repository"] + ":latest"
    with pytest.raises(C.ConfigError): C.plan(config, release, "gcloud")
    release["image"] = config["image_repository"] + "@sha256:" + "a"*64
    config["services"]["api"]["revision_max_instances"] = 20
    with pytest.raises(C.ConfigError): C.plan(config, release, "gcloud")


FAKE = '''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
p=Path(os.environ["C362_FIXTURE"]);s=json.loads(p.read_text());a=sys.argv[1:]
with p.with_suffix(".calls").open("a") as f:f.write(json.dumps({"args":a,"bearer_present":"DEPLOY_VERIFY_BEARER" in os.environ})+"\\n")
if os.environ.get("C362_FAIL"):
 print(os.environ["C362_FAIL"],file=sys.stderr);sys.exit(1)
if a[:3]==["run","services","describe"]:body=s["services"]["api" if a[3]=="chirp-api" else "ws"]
elif a[:3]==["run","revisions","describe"]:body=s["revisions"][a[3]]
elif a[:3]==["run","jobs","list"]:body=list(s["jobs"].values())
elif a[:3]==["sql","instances","describe"]:body=s["database"]
else:sys.exit(99)
print(json.dumps(body))
'''


def invoke(tmp_path, snap, release, *extra, fail=False, mode="check"):
    fake = tmp_path / "gcloud"; fake.write_text(FAKE); fake.chmod(0o755)
    fixture_path = tmp_path / "fixture.json"; fixture_path.write_text(json.dumps(snap))
    release_path = tmp_path / "release.json"; release_path.write_text(json.dumps(release))
    env = os.environ.copy() | {"C362_FIXTURE": str(fixture_path), "DEPLOY_VERIFY_BEARER": SECRET}
    if fail: env["C362_FAIL"] = SECRET
    result = subprocess.run([sys.executable, str(ROOT / "scripts/deployment_config.py"), mode, "--gcloud", str(fake), "--release", str(release_path), *extra], env=env, capture_output=True, text=True, timeout=15)
    assert SECRET not in result.stdout + result.stderr
    return result, json.loads(result.stdout), fixture_path.with_suffix(".calls")


def test_actual_cli_explicit_project_reads_and_private_metadata_redaction(config, snap, release, tmp_path):
    for body in snap["services"].values():
        body["spec"]["template"]["spec"]["containers"][0]["env"].append({"name": "PRIVATE_TOKEN", "value": SECRET})
        body["metadata"]["annotations"]["private-annotation"] = SECRET
    result, report, path = invoke(tmp_path, snap, release)
    assert result.returncode == 0 and report["verdict"] == "CONFIG_MATCH"
    calls = [json.loads(row) for row in path.read_text().splitlines()]
    assert len(calls) == 8
    for call in calls:
        assert call["args"][call["args"].index("--project")+1] == config["project"]
        assert call["args"][call["args"].index("--format")+1].startswith("json(")
        assert not call["bearer_present"]
        assert not {"deploy", "update", "replace", "execute"}.intersection(call["args"])


def test_actual_cli_provider_error_redacted(config, snap, release, tmp_path):
    result, report, _ = invoke(tmp_path, snap, release, fail=True)
    assert result.returncode == 2 and report["error"] == "cloud_metadata_unavailable"


def test_actual_plan_cli_no_provider_calls(config, snap, release, tmp_path):
    result, report, calls = invoke(tmp_path, snap, release, mode="plan")
    assert result.returncode == 0 and not calls.exists()
    assert report["mode"] == "PRINT_ONLY_NOT_EXECUTED"


def test_unknown_argument_redacted(config, snap, release, tmp_path):
    result, report, calls = invoke(tmp_path, snap, release, "--bearer", SECRET)
    assert result.returncode == 2 and report["error"] == "invalid_arguments" and not calls.exists()


def test_wrong_secret_ref_redacted_and_api_bindings_not_added_to_ws(config, snap, release):
    container = snap["revisions"][release["revisions"]["api"]]["spec"]["containers"][0]
    next(row for row in container["env"] if row["name"] == "DATABASE_URL")["valueFrom"]["secretKeyRef"]["name"] = SECRET
    ws = snap["revisions"][release["revisions"]["ws"]]["spec"]["containers"][0]
    ws["env"].append({"name": "STRIPE_SECRET_KEY", "value": SECRET})
    report = run_compare(config, snap, release)
    assert finding(report, "secret_reference:DATABASE_URL")
    assert finding(report, "unexpected_owned_secret_binding")
    plan = C.plan(config, release, "gcloud")
    assert "STRIPE_SECRET_KEY" in plan["steps"][0]["stage_command"]
    assert "STRIPE_SECRET_KEY" not in plan["steps"][1]["stage_command"]


def test_unexpected_entrypoint_or_container_prevents_capacity_proof(config, snap, release):
    snap["revisions"][release["revisions"]["api"]]["spec"]["containers"][0]["args"] = ["--workers", "16", SECRET]
    report = run_compare(config, snap, release)
    assert finding(report, "entrypoint_override") and report["verdict"] == "NOT_PROVEN"


def test_cloud_deadline_is_finite_and_timeout_text_never_leaks(config, monkeypatch):
    from argparse import Namespace
    def timeout(command, **kwargs):
        assert kwargs["timeout"] == 30
        raise subprocess.TimeoutExpired(command, 30, output=SECRET, stderr=SECRET)
    monkeypatch.setattr(C.subprocess, "run", timeout)
    with pytest.raises(C.ConfigError, match="^cloud_metadata_unavailable$"):
        C.cloud(Namespace(gcloud="gcloud", config_data=config), ["run", "jobs", "list"], "metadata.name")


def test_repository_build_override_or_inheritance_cycle_detected(config, tmp_path):
    mobile = tmp_path / "app-mobile"
    (mobile / "src/api").mkdir(parents=True)
    (mobile / "src/api/client.ts").write_text((ROOT / "app-mobile/src/api/client.ts").read_text())
    (mobile / "eas.json").write_text(json.dumps({"build": {"production": {"env": {"EXPO_PUBLIC_WS_URL": "wss://wrong.example/ws"}}}}))
    assert not C.client_repository(config, tmp_path)
    (mobile / "eas.json").write_text(json.dumps({"build": {"production": {"extends": "production"}}}))
    assert not C.client_repository(config, tmp_path)


def test_production_endpoints_are_read_from_source_not_copied_test_constants(config):
    assert C.client_repository(config)
    config["services"]["ws"]["client_origin"] = "https://another-service-uc.a.run.app"
    assert not C.client_repository(config)


@pytest.mark.parametrize("traffic", [[{"percent": 100}], [{"revisionName": "", "percent": 100}], [{"latestRevision": True, "percent": 100}]])
def test_each_traffic_row_must_resolve_its_own_revision(config, snap, release, traffic):
    snap["services"]["api"]["status"]["traffic"] = traffic
    report = run_compare(config, snap, release)
    assert finding(report, "traffic_accounting") and report["verdict"] == "DRIFT"


@pytest.mark.parametrize("change", ["shell_workers", "missing_command", "another_module", "workers", "missing_image"])
def test_job_worker_and_image_evidence_required(config, snap, release, change):
    row = snap["jobs"]["chirp-purge"]["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]
    if change == "shell_workers": row.update(command=["sh"], args=["-c", "python -m app.jobs.purge & python -m app.jobs.purge; wait"])
    if change == "missing_command": row.pop("command")
    if change == "another_module": row["args"] = ["-m", "app.main"]
    if change == "workers": row["env"].append({"name": "WEB_CONCURRENCY", "value": "8"})
    if change == "missing_image": row.pop("image")
    assert run_compare(config, snap, release)["verdict"] == "NOT_PROVEN"


@pytest.mark.parametrize("build_id", ["", "  ", "\n", 123, None])
def test_compiled_artifact_requires_nonblank_build_identity(config, snap, release, build_id):
    release["client_build"]["build_id"] = build_id
    report = run_compare(config, snap, release)
    assert finding(report, "compiled_build_evidence_missing_or_mismatched")


@pytest.mark.parametrize("overrides", [("api",), ("ws",), ("api", "ws")])
def test_service_identity_overrides_bind_plan_template_and_serving_revision(config, snap, release, overrides):
    expected = {role: config["shared"]["service_account"] for role in ("api", "ws")}
    for role in overrides:
        account = f"reviewed-{role}-runtime@chirps-prod.iam.gserviceaccount.com"
        config["services"][role]["service_account"] = account
        expected[role] = account
        # Change observed evidence independently of the production resolver.
        snap["services"][role]["spec"]["template"]["spec"]["serviceAccountName"] = account
        snap["revisions"][release["revisions"][role]]["spec"]["serviceAccountName"] = account
    C.validate(config)
    report = run_compare(config, snap, release)
    assert report["verdict"] == "CONFIG_MATCH" and report["findings"] == []
    plan = C.plan(config, release, "gcloud")
    assert plan["mode"] == "PRINT_ONLY_NOT_EXECUTED"
    for step in plan["steps"]:
        command = shlex.split(step["stage_command"])
        assert command[command.index("--service-account") + 1] == expected[step["service"]]
        assert command[command.index("--image") + 1] == release["image"]
        assert "--source" not in command and "--no-traffic" in command


@pytest.mark.parametrize("role", ["api", "ws"])
@pytest.mark.parametrize("observed_scope", ["template", "revision"])
def test_override_does_not_excuse_shared_identity_on_either_observed_scope(config, snap, release, role, observed_scope):
    account = f"reviewed-{role}-runtime@chirps-prod.iam.gserviceaccount.com"
    config["services"][role]["service_account"] = account
    template = snap["services"][role]["spec"]["template"]["spec"]
    revision = snap["revisions"][release["revisions"][role]]["spec"]
    template["serviceAccountName"] = revision["serviceAccountName"] = account
    (template if observed_scope == "template" else revision)["serviceAccountName"] = config["shared"]["service_account"]
    report = run_compare(config, snap, release)
    scope = role + ":template" if observed_scope == "template" else release["revisions"][role]
    assert report["verdict"] == "DRIFT"
    assert {"scope": scope, "kind": "drift", "field": "serviceAccountName"} in report["findings"]


@pytest.mark.parametrize("account", [None, "", " ", 123, True, [], {}, "account\n--flag", "account,other", "$(private)"])
def test_explicit_invalid_service_account_never_falls_back_or_renders(config, release, account):
    config["services"]["ws"]["service_account"] = account
    with pytest.raises(C.ConfigError, match="^invalid_service_account$"):
        C.plan(config, release, "gcloud")


def test_current_identity_defaults_and_all_roles_preserved(config, snap, release):
    assert all("service_account" not in row for row in config["services"].values())
    assert all(row["env"]["SERVICE_ROLE"] == "all" for row in config["services"].values())
    before = C.plan(config, release, "gcloud")
    assert run_compare(config, snap, release)["verdict"] == "CONFIG_MATCH"
    for service in config["services"].values():
        service["service_account"] = config["shared"]["service_account"]
    assert C.plan(config, release, "gcloud") == before
    assert run_compare(config, snap, release)["verdict"] == "CONFIG_MATCH"


@pytest.mark.parametrize("ann", [{"run.googleapis.com/scalingMode": "manual", "run.googleapis.com/manualInstanceCount": "40"}, {"run.googleapis.com/manualInstanceCount": "40"}, {"run.googleapis.com/scalingMode": "unrecognized"}])
def test_manual_scaling_cannot_borrow_autoscaling_capacity(config, snap, release, ann):
    snap["services"]["api"]["metadata"]["annotations"].update(ann)
    report = run_compare(config, snap, release)
    assert finding(report, "automatic_scaling_model") and report["verdict"] == "DRIFT"
    assert report["pool_envelope"]["listed_revisions_configured_capacity"] is None
    command = shlex.split(C.plan(config, release, "gcloud")["steps"][0]["stage_command"])
    assert command[command.index("--scaling")+1] == "auto"
    assert '"run.googleapis.com/scalingMode"' in C.service_fields()
    assert '"run.googleapis.com/manualInstanceCount"' in C.service_fields()
