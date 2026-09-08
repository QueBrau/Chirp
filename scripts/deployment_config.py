"""Read-only intended Cloud Run configuration, release comparison and command rendering."""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from typing import Any

from deploy_verify import ready_and_observed

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "infra/deployment.json"
MAX_BYTES = 1024 * 1024
ENV_NAMES = ("ENV", "AUTH_MODE", "FIREBASE_PROJECT_ID", "DB_POOL_SIZE", "DB_MAX_OVERFLOW", "DB_POOL_TIMEOUT", "WEB_CONCURRENCY")
ANN = ("autoscaling.knative.dev/minScale", "autoscaling.knative.dev/maxScale", "run.googleapis.com/cloudsql-instances", "run.googleapis.com/vpc-access-connector", "run.googleapis.com/vpc-access-egress", "run.googleapis.com/cpu-throttling", "run.googleapis.com/startup-cpu-boost")
SERVICE_ANN = ("run.googleapis.com/scalingMode", "run.googleapis.com/manualInstanceCount", "run.googleapis.com/minScale", "run.googleapis.com/maxScale", "run.googleapis.com/ingress", "run.googleapis.com/urls")


class ConfigError(ValueError):
    """Only fixed diagnostic labels are exposed to the caller."""


class SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ConfigError("invalid_arguments")


def load(path: Path) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ConfigError("duplicate_json_key")
            result[key] = value
        return result
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_BYTES:
            raise ValueError
        value = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, ValueError, TypeError):
        raise ConfigError("invalid_json_input") from None


def positive(value: Any, minimum: int = 1) -> int:
    if type(value) is not int or not minimum <= value <= 100000:
        raise ConfigError("invalid_capacity")
    return value


def timestamp(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed
    except (AttributeError, ValueError):
        raise ConfigError("invalid_timestamp") from None


def fresh(value: Any, now: datetime, hours: int) -> bool:
    try:
        age = now - timestamp(value)
        return timedelta(0) <= age <= timedelta(hours=hours)
    except ConfigError:
        return False


def identifier(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,62}", value) is not None


def public_origin(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"https://[a-z0-9-]+(?:\.[a-z0-9-]+)*\.run\.app", value) is not None


def defaults(root: Path = ROOT) -> dict[str, int]:
    """Read actual checked-out settings, without importing app/secrets/dependencies."""
    tree = ast.parse((root / "backend/app/config.py").read_text())
    names = {"db_pool_size": "DB_POOL_SIZE", "db_max_overflow": "DB_MAX_OVERFLOW", "db_pool_timeout": "DB_POOL_TIMEOUT"}
    values = {}
    for cls in tree.body:
        if isinstance(cls, ast.ClassDef) and cls.name == "Settings":
            for row in cls.body:
                if isinstance(row, ast.AnnAssign) and isinstance(row.target, ast.Name) and row.target.id in names:
                    values[names[row.target.id]] = positive(ast.literal_eval(row.value), 0)
    if set(values) != set(names.values()) or values["DB_POOL_SIZE"] == 0:
        raise ConfigError("unbounded_or_unknown_runtime_defaults")
    values["WEB_CONCURRENCY"] = 1
    return values


def validate(config: dict) -> None:
    try:
        if config["version"] != 1 or set(config["services"]) != {"api", "ws"}:
            raise ConfigError("invalid_configuration")
        for value in (config["project"], config["region"], config["database"]["instance"]):
            if not identifier(value):
                raise ConfigError("invalid_resource_name")
        if not re.fullmatch(r"[a-z0-9-]+-docker.pkg.dev/[a-z0-9/-]+", config["image_repository"]):
            raise ConfigError("invalid_image_repository")
        for key, value in config["database"].items():
            if key not in ("instance", "tier"):
                positive(value, 0 if "reserved" in key else 1)
        for service in config["services"].values():
            if not identifier(service["name"]) or not public_origin(service["client_origin"]):
                raise ConfigError("invalid_service")
            for key in ("concurrency", "revision_min_instances", "revision_max_instances", "service_max_instances", "workers"):
                positive(service[key], 0 if key == "revision_min_instances" else 1)
            if service["revision_min_instances"] > service["revision_max_instances"]:
                raise ConfigError("invalid_scaling_range")
            if service["workers"] != 1:
                raise ConfigError("worker_model_requires_review")
            if not re.fullmatch(r"[1-9][0-9]*", service["cpu"]) or not re.fullmatch(r"[1-9][0-9]*(?:Mi|Gi)", service["memory"]):
                raise ConfigError("invalid_resources")
            for key in ("DB_POOL_SIZE", "DB_MAX_OVERFLOW"):
                positive(int(service["env"][key]), 0 if key.endswith("OVERFLOW") else 1)
        shared = config["shared"]
        if set(shared["env"]) - set(ENV_NAMES) or any(set(s["env"]) - set(ENV_NAMES) for s in config["services"].values()):
            raise ConfigError("unapproved_environment_key")
        for value in list(shared["env"].values()) + [x for s in config["services"].values() for x in s["env"].values()]:
            if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", value):
                raise ConfigError("invalid_environment_value")
        for key, value in (shared["secrets"] | {k: v for service in config["services"].values() for k, v in service.get("secrets", {}).items()}).items():
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or not re.fullmatch(r"[A-Za-z0-9_-]+:(?:latest|[1-9][0-9]*)", value):
                raise ConfigError("invalid_secret_reference")
        for key in ("service_account", "cloud_sql", "vpc_connector", "vpc_egress", "ingress"):
            if not re.fullmatch(r"[A-Za-z0-9_@.:-]+", shared[key]):
                raise ConfigError("invalid_shared_setting")
        if shared["scaling_mode"] != "automatic":
            raise ConfigError("automatic_scaling_model_required")
        positive(shared["timeout_seconds"])
        for key in ("cpu_throttling", "startup_cpu_boost"):
            if type(shared[key]) is not bool:
                raise ConfigError("invalid_shared_setting")
        for name, job in config["jobs"].items():
            if not identifier(name):
                raise ConfigError("invalid_job_name")
            for key in ("parallelism", "max_overlapping_executions", "workers"):
                positive(job[key])
            if job["workers"] != 1 or not re.fullmatch(r"app\.jobs\.[a-z_]+", job["module"]):
                raise ConfigError("job_worker_model_requires_review")
        rollout = config["rollout"]
        if rollout["max_overlapping_services"] != 1 or rollout["revisions_per_service"] != 2 or rollout["jobs_quiescent"] is not True:
            raise ConfigError("rollout_policy_requires_review")
        positive(rollout["max_window_minutes"])
        if config["client"]["ws_path"] != "/ws":
            raise ConfigError("invalid_client_path")
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError("invalid_configuration") from None


def image_ok(value: Any, config: dict) -> bool:
    return isinstance(value, str) and re.fullmatch(re.escape(config["image_repository"]) + r"@sha256:[0-9a-f]{64}", value) is not None


def validate_release(release: dict, config: dict) -> None:
    try:
        if not image_ok(release["image"], config) or not re.fullmatch(r"[A-Za-z0-9_]{1,64}", release["schema_head"]):
            raise ConfigError("invalid_release")
        for role, service in config["services"].items():
            name = release["revisions"][role]
            if not identifier(name) or not name.startswith(service["name"] + "-"):
                raise ConfigError("invalid_release_revision")
        for role, row in release.get("rollout", {}).get("previous", {}).items():
            if role not in config["services"] or not identifier(row["revision"]) or not row["revision"].startswith(config["services"][role]["name"] + "-") or not image_ok(row["image"], config):
                raise ConfigError("invalid_previous_release")
    except (KeyError, TypeError):
        raise ConfigError("invalid_release") from None


def pool_envelope(config: dict, runtime: dict[str, int], job_pools: dict[str, int] | None = None) -> dict:
    """Conservative configured pool capacity; not measured demand or a hard GCP cap."""
    base = {}
    for role, service in config["services"].items():
        per_process = sum(int(service["env"][key]) for key in ("DB_POOL_SIZE", "DB_MAX_OVERFLOW"))
        base[role] = service["revision_max_instances"] * service["workers"] * per_process
    jobs = sum(job["parallelism"] * job["max_overlapping_executions"] * job["workers"] * (job_pools or {}).get(name, runtime["DB_POOL_SIZE"] + runtime["DB_MAX_OVERFLOW"]) for name, job in config["jobs"].items())
    database = config["database"]
    overhead = sum(database[k] for k in ("superuser_reserved_connections", "reserved_connections", "operator_connections", "surge_allowance_connections"))
    steady = sum(base.values()) + jobs + overhead
    serial = sum(base.values()) + max(base.values()) + overhead
    simultaneous = 2 * sum(base.values()) + jobs + overhead
    return {"service_connections": base, "job_connections": jobs, "reserved_operator_and_contingency": overhead,
            "steady_with_jobs": steady, "serialized_rollout_jobs_quiescent": serial,
            "simultaneous_rollout_with_jobs": simultaneous, "assumed_max_connections": database["max_connections"],
            "steady_fits": steady <= database["max_connections"], "policy_rollout_fits": serial <= database["max_connections"],
            "simultaneous_rollout_fits": simultaneous <= database["max_connections"],
            "hard_capacity_guarantee": False}


def cloud(args: argparse.Namespace, command: list[str], fields: str) -> Any:
    # A projection is mandatory. Filter env rows inside gcloud before output and
    # sanitize again below; never serialize provider bodies or stderr.
    child = {k: v for k, v in os.environ.items() if k not in ("DEPLOY_VERIFY_BEARER", "DATABASE_URL", "PGPASSWORD")}
    child["CLOUDSDK_CORE_DISABLE_FILE_LOGGING"] = "1"
    child["CLOUDSDK_CORE_DISABLE_PROMPTS"] = "1"
    try:
        result = subprocess.run([args.gcloud, *command, "--project", args.config_data["project"], "--format", f"json({fields})"], env=child, capture_output=True, timeout=30, check=False)
        if result.returncode != 0 or len(result.stdout) > MAX_BYTES:
            raise ValueError
        return json.loads(result.stdout)
    except Exception:
        raise ConfigError("cloud_metadata_unavailable") from None


def annotations(prefix: str, keys: tuple[str, ...]) -> list[str]:
    return [prefix + '."' + key + '"' for key in keys]


def container_fields(prefix: str) -> list[str]:
    return [prefix + "." + field for field in ("image", "resources", "command", "args")]+[prefix + '.env.filter("name:(' + " ".join(ENV_NAMES) + ') OR valueFrom.secretKeyRef.name:*")']


def revision_fields() -> list[str]:
    return ["metadata.name", 'metadata.labels."serving.knative.dev/service"', "metadata.generation", "status.observedGeneration", "status.conditions[].type", "status.conditions[].status", "status.imageDigest", "spec.containerConcurrency", "spec.timeoutSeconds", "spec.serviceAccountName"] + annotations("metadata.annotations", ANN) + container_fields("spec.containers[]")


def service_fields() -> str:
    fields = ["metadata.name", "metadata.generation", "status.observedGeneration", "status.conditions[].type", "status.conditions[].status", "status.url", "status.traffic", "status.latestCreatedRevisionName", "status.latestReadyRevisionName", "spec.template.spec.containerConcurrency", "spec.template.spec.timeoutSeconds", "spec.template.spec.serviceAccountName"]
    return ",".join(fields + annotations("metadata.annotations", SERVICE_ANN) + annotations("spec.template.metadata.annotations", ANN) + container_fields("spec.template.spec.containers[]"))


def collect(args: argparse.Namespace) -> dict:
    config = args.config_data
    region = ["--region", config["region"]]
    snapshot = {"services": {}, "revisions": {}, "jobs": {}}
    for role, service in config["services"].items():
        body = cloud(args, ["run", "services", "describe", service["name"], *region], service_fields())
        snapshot["services"][role] = body
        status = body.get("status", {})
        names = {row.get("revisionName") for row in status.get("traffic", [])}
        names.update(status.get(k) for k in ("latestCreatedRevisionName", "latestReadyRevisionName"))
        names.discard(None)
        if not names or len(names) > 8 or any(not identifier(n) or not n.startswith(service["name"] + "-") for n in names):
            raise ConfigError("unaccounted_revision_inventory")
        for name in sorted(names):
            snapshot["revisions"][name] = cloud(args, ["run", "revisions", "describe", name, *region], ",".join(revision_fields()))
    fields = ["metadata.name", "spec.template.spec.parallelism", "spec.template.spec.taskCount"] + container_fields("spec.template.spec.template.spec.containers[]")
    rows = cloud(args, ["run", "jobs", "list", *region], ",".join(fields))
    if not isinstance(rows, list) or len(rows) > 100:
        raise ConfigError("invalid_job_inventory")
    for row in rows:
        name = row.get("metadata", {}).get("name")
        if not identifier(name):
            raise ConfigError("invalid_job_inventory")
        snapshot["jobs"][name] = row
    snapshot["database"] = cloud(args, ["sql", "instances", "describe", config["database"]["instance"]], "name,settings.tier,settings.databaseFlags")
    # Re-read service metadata after the other queries; a moving snapshot is not
    # promoted to steady-state evidence. No HTTP probes or credentials are used.
    snapshot["services_after"] = {role: cloud(args, ["run", "services", "describe", service["name"], *region], service_fields()) for role, service in config["services"].items()}
    return snapshot


def note(report: dict, scope: str, kind: str, field: str) -> None:
    row = {"scope": scope, "kind": kind, "field": field}
    if row not in report["findings"]:
        report["findings"].append(row)


def env_values(container: dict, runtime: dict) -> tuple[dict, list[str]]:
    values, inferred = {}, []
    for key in ENV_NAMES:
        rows = [r for r in container.get("env", []) if r.get("name") == key]
        if len(rows) == 1 and isinstance(rows[0].get("value"), str):
            values[key] = rows[0]["value"]
        elif not rows and key in runtime:
            values[key] = str(runtime[key]); inferred.append(key)
        else:
            values[key] = None
    return values, inferred


def check_spec(report: dict, scope: str, spec: dict, ann: dict, config: dict, role: str, runtime: dict) -> dict:
    service, shared = config["services"][role], config["shared"]
    expected = {"containerConcurrency": service["concurrency"], "timeoutSeconds": shared["timeout_seconds"], "serviceAccountName": shared["service_account"]}
    for key, value in expected.items():
        if spec.get(key) != value:
            note(report, scope, "drift", key)
    expected_ann = {"autoscaling.knative.dev/minScale": str(service["revision_min_instances"]), "autoscaling.knative.dev/maxScale": str(service["revision_max_instances"]), "run.googleapis.com/cloudsql-instances": shared["cloud_sql"], "run.googleapis.com/vpc-access-connector": shared["vpc_connector"], "run.googleapis.com/vpc-access-egress": shared["vpc_egress"], "run.googleapis.com/cpu-throttling": str(shared["cpu_throttling"]).lower(), "run.googleapis.com/startup-cpu-boost": str(shared["startup_cpu_boost"]).lower()}
    for key, value in expected_ann.items():
        actual = ann.get(key, "true" if key == "run.googleapis.com/cpu-throttling" else "0" if key.endswith("/minScale") else None)
        if actual != value:
            note(report, scope, "drift", key)
    containers = spec.get("containers", [])
    if not isinstance(containers, list) or len(containers) != 1:
        note(report, scope, "unknown", "container_inventory"); return {}
    container = containers[0]
    if container.get("command") or container.get("args"):
        note(report, scope, "unknown", "entrypoint_override")
    resources = container.get("resources", {}).get("limits", {})
    cpu = resources.get("cpu")
    if cpu != service["cpu"] and cpu != str(int(service["cpu"]) * 1000) + "m":
        note(report, scope, "drift", "cpu")
    if resources.get("memory") != service["memory"]:
        note(report, scope, "drift", "memory")
    values, inferred = env_values(container, runtime)
    expected_env = shared["env"] | service["env"] | {"WEB_CONCURRENCY": str(service["workers"])}
    for key, value in expected_env.items():
        if values.get(key) != value:
            note(report, scope, "drift", key)
    expected_refs = shared["secrets"] | service.get("secrets", {})
    owned_ref_names = set(shared["secrets"]) | {key for row in config["services"].values() for key in row.get("secrets", {})}
    if any(row.get("name") in owned_ref_names - set(expected_refs) for row in container.get("env", [])):
        note(report, scope, "drift", "unexpected_owned_secret_binding")
    for key, value in expected_refs.items():
        rows = [r for r in container.get("env", []) if r.get("name") == key]
        ref = rows[0].get("valueFrom", {}).get("secretKeyRef", {}) if len(rows) == 1 else {}
        if ref.get("name", "") + ":" + ref.get("key", "") != value:
            note(report, scope, "drift", "secret_reference:" + key)
    if any(key in inferred for key in ("DB_POOL_SIZE", "DB_MAX_OVERFLOW")):
        note(report, scope, "unknown", "pool_inferred_from_checkout_not_image")
    # Only recognized nonsecret values are retained. Malformed/malicious values
    # cannot become diagnostics or public artifacts, even under an allowed key.
    return {"inferred_from_checkout_not_image": inferred, "env": {key: value if value == expected_env.get(key) else "mismatch" for key, value in values.items()},
            "revision_max_instances": int(ann["autoscaling.knative.dev/maxScale"]) if re.fullmatch(r"[1-9][0-9]{0,4}", ann.get("autoscaling.knative.dev/maxScale", "")) else None}


def client_repository(config: dict, root: Path = ROOT) -> bool:
    """Check checked-in defaults/profile overrides; external EAS env is separate evidence."""
    try:
        source = (root / "app-mobile/src/api/client.ts").read_text()
        expected = {"DEFAULT_API_BASE_URL": config["services"]["api"]["client_origin"], "DEFAULT_WS_URL": config["services"]["ws"]["client_origin"].replace("https:", "wss:") + config["client"]["ws_path"]}
        for name, value in expected.items():
            if re.findall(r"const " + name + r' = "([^"\n]+)";', source) != [value]:
                return False
        profiles = load(root / "app-mobile/eas.json")["build"]
        def merged(name, seen):
            if name in seen or name not in profiles:
                raise ConfigError("invalid_profile_inheritance")
            profile = profiles[name]
            parent = merged(profile["extends"], seen | {name}) if "extends" in profile else {}
            return parent | profile.get("env", {})
        for name in profiles:
            env = merged(name, set())
            if env.get("EXPO_PUBLIC_API_URL", expected["DEFAULT_API_BASE_URL"]) != expected["DEFAULT_API_BASE_URL"] or env.get("EXPO_PUBLIC_WS_URL", expected["DEFAULT_WS_URL"]) != expected["DEFAULT_WS_URL"]:
                return False
        return True
    except (OSError, KeyError, ValueError, TypeError):
        return False


def compare(config: dict, snapshot: dict, release: dict | None, now: datetime, root: Path = ROOT) -> dict:
    runtime = defaults(root)
    report = {"observed_at": now.isoformat(), "verdict": "NOT_PROVEN", "authenticated_ready": False, "findings": [], "services": {}, "jobs": {}}
    active_window = False
    previous = {}
    if release:
        validate_release(release, config)
        rollout = release.get("rollout")
        if rollout:
            previous = rollout.get("previous", {})
            try:
                start, end = timestamp(rollout["started_at"]), timestamp(rollout["ends_at"])
                active_window = start <= now < end and timedelta(0) < end-start <= timedelta(minutes=config["rollout"]["max_window_minutes"])
            except (KeyError, ConfigError):
                pass
    else:
        note(report, "release", "unknown", "expected_release_missing")
    transitioned = set()
    overlapping = set()
    observed_capacity = 0
    observed_capacity_known = True
    serving_images = []
    for role, service in config["services"].items():
        body = snapshot["services"][role]
        if body != snapshot.get("services_after", {}).get(role):
            note(report, role, "unknown", "metadata_changed_during_observation")
        metadata, status = body.get("metadata", {}), body.get("status", {})
        if metadata.get("name") != service["name"] or not ready_and_observed(body):
            note(report, role, "unknown", "service_readiness_or_generation")
        ann = metadata.get("annotations", {})
        automatic = ann.get("run.googleapis.com/scalingMode", "automatic") == config["shared"]["scaling_mode"] and ann.get("run.googleapis.com/manualInstanceCount", "0") in ("0", "")
        if not automatic:
            note(report, role, "drift", "automatic_scaling_model")
        if ann.get("run.googleapis.com/maxScale") != str(service["service_max_instances"]) or ann.get("run.googleapis.com/minScale", "0") != "0":
            note(report, role, "drift", "service_scaling")
        if ann.get("run.googleapis.com/ingress") != config["shared"]["ingress"]:
            note(report, role, "drift", "ingress")
        try:
            aliases = json.loads(ann.get("run.googleapis.com/urls", "[]"))
            alias_ok = isinstance(aliases, list) and all(public_origin(u) for u in aliases) and service["client_origin"] in aliases and status.get("url") in aliases
        except (ValueError, TypeError):
            alias_ok = False
        if not alias_ok:
            note(report, role, "drift", "published_client_origin")
        template = body.get("spec", {}).get("template", {})
        settings = check_spec(report, role + ":template", template.get("spec", {}), template.get("metadata", {}).get("annotations", {}), config, role, runtime)
        template_containers = template.get("spec", {}).get("containers", [])
        template_image = template_containers[0].get("image") if len(template_containers) == 1 else None
        latest_name = status.get("latestCreatedRevisionName")
        latest_image = snapshot["revisions"].get(latest_name, {}).get("status", {}).get("imageDigest")
        if not image_ok(template_image, config) or template_image != latest_image:
            note(report, role, "drift", "template_image")
        traffic = status.get("traffic", [])
        if not isinstance(traffic, list) or not traffic or any(not isinstance(r, dict) or not identifier(r.get("revisionName")) or not r["revisionName"].startswith(service["name"] + "-") or type(r.get("percent", 0)) is not int or not 0 <= r.get("percent", 0) <= 100 for r in traffic) or sum(r.get("percent", 0) for r in traffic) != 100:
            note(report, role, "drift", "traffic_accounting")
            traffic = []
        names = {r.get("revisionName") for r in traffic}
        names.update(status.get(k) for k in ("latestCreatedRevisionName", "latestReadyRevisionName"))
        names.discard(None)
        safe_traffic = []
        for name in sorted(names):
            if not identifier(name) or not name.startswith(service["name"] + "-"):
                note(report, role, "unknown", "revision_identity"); continue
            revision = snapshot["revisions"].get(name, {})
            if not ready_and_observed(revision) or revision.get("metadata", {}).get("name") != name or revision.get("metadata", {}).get("labels", {}).get("serving.knative.dev/service") != service["name"]:
                note(report, role, "unknown", "revision_readiness_or_identity")
            revision_settings = check_spec(report, name, revision.get("spec", {}), revision.get("metadata", {}).get("annotations", {}), config, role, runtime)
            capacity = None
            try:
                if not automatic:
                    raise ConfigError("manual_capacity_unknown")
                container = revision["spec"]["containers"][0]
                actual_env, _ = env_values(container, runtime)
                capacity = positive(revision_settings["revision_max_instances"]) * positive(int(actual_env["WEB_CONCURRENCY"])) * (positive(int(actual_env["DB_POOL_SIZE"])) + positive(int(actual_env["DB_MAX_OVERFLOW"]), 0))
                observed_capacity += capacity
            except (KeyError, IndexError, TypeError, ValueError):
                observed_capacity_known = False
            actual_image = revision.get("status", {}).get("imageDigest")
            rows = [r for r in traffic if r.get("revisionName") == name]
            if rows:
                if image_ok(actual_image, config):
                    serving_images.append(actual_image)
                else:
                    note(report, role, "unknown", "serving_image")
            if release and (name != release["revisions"][role] or actual_image != release["image"]):
                old = previous.get(role, {})
                if active_window and name == old.get("revision") and actual_image == old.get("image") and not any(r.get("tag") for r in rows):
                    note(report, role, "transition", "previous_release_present"); transitioned.add(role)
                else:
                    note(report, role, "drift", "unexpected_revision_or_image")
            safe_traffic.append({"revision": name, "image": actual_image if image_ok(actual_image, config) else None, "percent": sum(r.get("percent", 0) for r in rows), "tagged": any(bool(r.get("tag")) for r in rows), "configured_pool_capacity": capacity})
        if release and previous.get(role, {}).get("revision") in names and release["revisions"][role] in names:
            overlapping.add(role)
        report["services"][role] = {"canonical_origin": status.get("url") if alias_ok else None, "client_origin_matches_published_alias": alias_ok, "settings": settings, "revisions": safe_traffic}
    if len(overlapping) > config["rollout"]["max_overlapping_services"]:
        note(report, "rollout", "drift", "simultaneous_service_rollout")
    if not release and len(set(serving_images)) > 1:
        note(report, "release", "drift", "image_parity")
    job_pools = {}
    if set(snapshot["jobs"]) != set(config["jobs"]):
        note(report, "jobs", "unknown", "unmodelled_or_missing_job")
    for name, policy in config["jobs"].items():
        spec = snapshot["jobs"].get(name, {}).get("spec", {}).get("template", {}).get("spec", {})
        containers = spec.get("template", {}).get("spec", {}).get("containers", [])
        if len(containers) != 1:
            note(report, name, "unknown", "job_container_inventory"); continue
        container = containers[0]
        values, inferred = env_values(container, runtime)
        arguments = container.get("args")
        entrypoint_matches = container.get("command") == ["python"] and isinstance(arguments, list) and arguments[:2] == ["-m", policy["module"]] and all(isinstance(v, str) and re.fullmatch(r"--[a-z-]+(?:=[a-z0-9]+)?|[0-9]+", v) for v in arguments[2:])
        if not entrypoint_matches or values.get("WEB_CONCURRENCY") != str(policy["workers"]):
            note(report, name, "unknown", "job_entrypoint_or_worker_model")
        if not image_ok(container.get("image"), config):
            note(report, name, "unknown", "job_image_identity")
        try:
            job_pools[name] = positive(int(values["DB_POOL_SIZE"])) + positive(int(values["DB_MAX_OVERFLOW"]), 0)
        except (ValueError, TypeError, ConfigError):
            note(report, name, "unknown", "job_pool"); continue
        tasks = spec.get("taskCount")
        parallel = spec.get("parallelism", tasks)
        if type(tasks) is not int or type(parallel) is not int or min(tasks, parallel) != policy["parallelism"]:
            note(report, name, "drift", "job_parallelism")
        if any(key in inferred for key in ("DB_POOL_SIZE", "DB_MAX_OVERFLOW")):
            note(report, name, "unknown", "pool_inferred_from_checkout_not_image")
        report["jobs"][name] = {"entrypoint_model_matches": entrypoint_matches, "pool_capacity_per_process": job_pools[name], "inferred_from_checkout_not_image": inferred, "parallelism": parallel if type(parallel) is int else None,
                                "overlapping_executions": "operator_policy_not_enforced", "image": containers[0].get("image") if image_ok(containers[0].get("image"), config) else None}
    report["pool_envelope"] = pool_envelope(config, runtime, job_pools)
    report["pool_envelope"]["listed_revisions_configured_capacity"] = observed_capacity if observed_capacity_known else None
    observed_total = observed_capacity + report["pool_envelope"]["job_connections"] + report["pool_envelope"]["reserved_operator_and_contingency"]
    report["pool_envelope"]["listed_revisions_plus_jobs_and_reserves"] = observed_total if observed_capacity_known else None
    if not observed_capacity_known:
        note(report, "database", "unknown", "observed_revision_pool_capacity")
    if not report["pool_envelope"]["steady_fits"] or not report["pool_envelope"]["policy_rollout_fits"]:
        note(report, "database", "drift", "intended_pool_envelope_exceeded")
    if snapshot.get("database", {}).get("settings", {}).get("tier") != config["database"]["tier"]:
        note(report, "database", "drift", "tier")
    for flag in snapshot.get("database", {}).get("settings", {}).get("databaseFlags", []):
        if flag.get("name") in ("max_connections", "superuser_reserved_connections", "reserved_connections") and flag.get("value") != str(config["database"][flag["name"]]):
            note(report, "database", "drift", "configured_connection_flag")
    observed_db = (release or {}).get("database_observation", {})
    if not fresh(observed_db.get("observed_at"), now, config["database"]["evidence_max_age_hours"]) or any(type(observed_db.get(k)) is not int or observed_db[k] != config["database"][k] for k in ("max_connections", "superuser_reserved_connections", "reserved_connections")):
        note(report, "database", "unknown", "current_sql_settings_evidence_missing_or_mismatched")
    report["database_evidence"] = "operator_supplied_SQL_observation" if not any(f["field"] == "current_sql_settings_evidence_missing_or_mismatched" for f in report["findings"]) else "assumed_capacity_only"
    if not client_repository(config, root):
        note(report, "client", "drift", "checked_in_endpoints")
    build = (release or {}).get("client_build", {})
    if not fresh(build.get("observed_at"), now, config["client"]["build_evidence_max_age_hours"]) or not isinstance(build.get("build_id"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}", build["build_id"]) or build.get("api_url") != config["services"]["api"]["client_origin"] or build.get("ws_url") != config["services"]["ws"]["client_origin"].replace("https:", "wss:") + config["client"]["ws_path"]:
        note(report, "client", "unknown", "compiled_build_evidence_missing_or_mismatched")
    report["client_evidence_scope"] = "repository_config_plus_operator_supplied_artifact_observation_not_device_test"
    if transitioned and (release or {}).get("rollout", {}).get("jobs_quiescent") is not True:
        note(report, "rollout", "unknown", "quiescent_jobs_not_confirmed")
    kinds = {row["kind"] for row in report["findings"]}
    report["verdict"] = "DRIFT" if "drift" in kinds else "NOT_PROVEN" if "unknown" in kinds else "TRANSITION" if "transition" in kinds else "CONFIG_MATCH"
    return report


def plan(config: dict, release: dict, gcloud: str) -> dict:
    validate_release(release, config)
    pool = pool_envelope(config, defaults())
    if not pool["steady_fits"] or not pool["policy_rollout_fits"]:
        raise ConfigError("unsafe_intended_pool_envelope")
    steps = []
    shared = config["shared"]
    for role, service in config["services"].items():
        name, revision = service["name"], release["revisions"][role]
        env = shared["env"] | service["env"] | {"WEB_CONCURRENCY": str(service["workers"])}
        argv = [gcloud, "run", "deploy", name, "--project", config["project"], "--region", config["region"], "--image", release["image"], "--revision-suffix", revision[len(name)+1:], "--no-traffic", "--scaling", "auto", "--cpu", service["cpu"], "--memory", service["memory"], "--concurrency", str(service["concurrency"]), "--min-instances", str(service["revision_min_instances"]), "--max-instances", str(service["revision_max_instances"]), "--max", str(service["service_max_instances"]), "--timeout", str(shared["timeout_seconds"]), "--service-account", shared["service_account"], "--add-cloudsql-instances", shared["cloud_sql"], "--vpc-connector", shared["vpc_connector"], "--vpc-egress", shared["vpc_egress"], "--ingress", shared["ingress"], "--cpu-throttling" if shared["cpu_throttling"] else "--no-cpu-throttling", "--cpu-boost" if shared["startup_cpu_boost"] else "--no-cpu-boost", "--update-env-vars", ",".join(k+"="+v for k, v in sorted(env.items())), "--update-secrets", ",".join(k+"="+v for k, v in sorted((shared["secrets"] | service.get("secrets", {})).items()))]
        promote = [gcloud, "run", "services", "update-traffic", name, "--project", config["project"], "--region", config["region"], "--to-revisions", revision+"=100"]
        steps.append({"service": role, "stage_command": shlex.join(argv), "promote_after_review_command": shlex.join(promote), "before_next_service": "Confirm old revision drained with zero instances; jobs remain quiescent. Traffic=0 alone is insufficient."})
    verify = ["scripts/deploy-verify", "--authenticated", "--project", config["project"], "--region", config["region"], "--api-service", config["services"]["api"]["name"], "--ws-service", config["services"]["ws"]["name"], "--expected-schema", release["schema_head"], "--api-revision", release["revisions"]["api"], "--ws-revision", release["revisions"]["ws"], "--api-image-digest", release["image"].split("@")[1], "--ws-image-digest", release["image"].split("@")[1], "--gcloud", gcloud]
    verification = shlex.join(verify) + ' --base-url "$API_CANONICAL_ORIGIN" --ws-base-url "$WS_CANONICAL_ORIGIN" --user-id "$QA_USER_ID" --campus-id "$QA_CAMPUS_ID"'
    return {"mode": "PRINT_ONLY_NOT_EXECUTED", "pool_envelope": pool, "steps": steps, "verification_command": verification, "verification_inputs": "Use canonical origins from a fresh inspection, known QA fixture IDs, and DEPLOY_VERIFY_BEARER environment only. Follow DEPLOY-VERIFICATION.md."}


def main(argv: list[str] | None = None) -> int:
    parser = SafeParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("mode", choices=("inspect", "check", "plan"))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--release", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--gcloud", default="gcloud")
    args = None
    try:
        args = parser.parse_args(argv)
        config = load(args.config); validate(config); args.config_data = config
        release = load(args.release) if args.release else None
        if args.mode != "inspect" and not release:
            raise ConfigError("reviewed_release_input_required")
        if release:
            validate_release(release, config)
        if args.mode == "plan":
            report = plan(config, release, args.gcloud)
        else:
            report = compare(config, collect(args), release, datetime.now(timezone.utc))
            report["config_sha256"] = hashlib.sha256(args.config.read_bytes()).hexdigest()
        output = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.report:
            args.report.write_text(output)
        print(output, end="")
        return 0 if report.get("verdict") == "CONFIG_MATCH" or args.mode == "plan" else 1
    except Exception as exc:
        # Values from argv/files/provider errors are never interpolated here.
        label = str(exc) if isinstance(exc, ConfigError) else "invalid_or_incomplete_evidence"
        print(json.dumps({"verdict": "NOT_PROVEN", "error": label, "authenticated_ready": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
