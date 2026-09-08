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
            "client_build": {"build_id": "reviewed-build", "observed_at": NOW.isoformat(), "api_url": config["services"]["api"]["client_origin"], "ws_url": config["services"]["ws"]["client_origin"].replace("https:", "wss:") + "/ws"}}


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


def test_print_only_plan_derives_both_services_and_verification(config, release):
    plan = C.plan(config, release, "gcloud")
    for role, row in zip(("api", "ws"), plan["steps"], strict=True):
        cmd = shlex.split(row["stage_command"])
        assert cmd[cmd.index("--image")+1] == release["image"]
        assert cmd[cmd.index("--max-instances")+1] == str(config["services"][role]["revision_max_instances"])
        assert cmd[cmd.index("--concurrency")+1] == str(config["services"][role]["concurrency"])
        assert "--no-traffic" in cmd and "--timeout" in cmd
        assert "--set-env-vars" not in cmd and "--set-secrets" not in cmd and "--allow-unauthenticated" not in cmd
        assert release["revisions"][role] + "=100" in shlex.split(row["promote_after_review_command"])
        assert config["shared"]["service_account"] in cmd
    assert release["schema_head"] in plan["verification_command"] and "--bearer" not in plan["verification_command"]
    assert plan["mode"] == "PRINT_ONLY_NOT_EXECUTED"


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
