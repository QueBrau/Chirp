"""Run the executable against two local HTTP peers and fake gcloud, never GCP.

These peers deliberately return apparently healthy routing while lying about
authentication, fixtures, migrations or serving metadata. The script must refuse
readiness, and must not copy bearer values into args, logs or report artifacts.
"""
from __future__ import annotations

import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "fake-c361-private-bearer-never-print"
USER = str(uuid.uuid4())
CAMPUS = str(uuid.uuid4())
DIGEST = "sha256:" + "a" * 64
HEAD = "expected-c361-head"
ME = {"user": {
    "id": USER, "firebase_uid": "fixture-uid", "email": "fixture@example.edu",
    "display_name": "QA Fixture", "avatar_url": None, "account_type": "greek",
    "campus_id": CAMPUS, "is_ghost": False, "is_platform_admin": False,
    "suspended_at": None, "created_at": "2026-09-07T00:00:00Z",
}, "memberships": []}
FEED = [{"id": str(uuid.uuid4()), "campus_id": CAMPUS, "body": "fixture chirp",
         "score": 0, "created_at": "2026-09-07T00:00:00Z", "my_vote": None}]


@pytest.fixture
def peers(tmp_path):
    states, servers, threads = [], [], []
    for service in ("chirp-api", "chirp-ws"):
        state = {"service": service, "revision": service + "-c361", "requests": [], "overrides": {}, "stop": threading.Event()}

        def handler_for(state):
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_GET(self):
                    auth = self.headers.get("Authorization")
                    state["requests"].append((self.path, auth))
                    status, body, headers = 404, {}, {}
                    if self.path == "/auth/campus-verification" or self.path.endswith("/chirps"):
                        status = 401
                    elif self.path == "/auth/me":
                        status, body = (200, copy.deepcopy(ME)) if auth == "Bearer " + TOKEN else (401, {})
                    elif self.path == "/_deployment":
                        status, body = 200, {"service": state["service"], "revision": state["revision"],
                                             "code_schema_heads": [HEAD], "database_schema_heads": [HEAD]}
                    elif self.path == f"/campuses/{CAMPUS}/chirps?limit=1":
                        status, body = (200, copy.deepcopy(FEED)) if auth == "Bearer " + TOKEN else (401, {})
                    override = state["overrides"].get(self.path)
                    if override:
                        status, body, headers = override(status, body, auth)
                    self.send_response(status)
                    for key, value in headers.items():
                        self.send_header(key, value)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    raw = json.dumps(body).encode()
                    if state.get("slow_stream") and self.path == "/auth/me" and auth == "Bearer " + TOKEN:
                        for byte in raw:
                            try:
                                self.wfile.write(bytes([byte]))
                                self.wfile.flush()
                            except (BrokenPipeError, ConnectionResetError):
                                break
                            if state["stop"].wait(0.1):
                                break
                    else:
                        self.wfile.write(raw)
            return Handler

        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(state))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        state["url"] = f"http://127.0.0.1:{server.server_port}"
        states.append(state)
        servers.append(server)
        threads.append(thread)
    metadata = {}
    for state in states:
        metadata[state["service"]] = {
            "metadata": {"name": state["service"], "generation": 3},
            "status": {"url": state["url"], "observedGeneration": 3,
                       "conditions": [{"type": "Ready", "status": "True"}],
                       "traffic": [{"revisionName": state["revision"], "percent": 100}]},
        }
        metadata[state["revision"]] = {
            "metadata": {"name": state["revision"], "generation": 1,
                         "labels": {"serving.knative.dev/service": state["service"]}},
            "status": {"imageDigest": "registry/release@" + DIGEST, "observedGeneration": 1,
                       "conditions": [{"type": "Ready", "status": "True"}]},
        }
    fake = tmp_path / "gcloud"
    fake.write_text('''#!/usr/bin/env python3
import json, os, sys
assert "DEPLOY_VERIFY_BEARER" not in os.environ
assert "fake-c361-private-bearer-never-print" not in " ".join(sys.argv)
assert sys.argv[1] == "run" and sys.argv[3] == "describe"
assert sys.argv[sys.argv.index("--project") + 1] == "fixture-project"
with open(os.environ["FAKE_CLOUD_CONFIG"]) as f:
    config = json.load(f)
with open(os.environ["FAKE_CLOUD_CALLS"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
print(json.dumps(config[sys.argv[4]]))
''')
    fake.chmod(0o755)
    config = tmp_path / "cloud.json"
    for state in states:
        state["cloud_config"] = config
    calls = tmp_path / "cloud-calls.jsonl"
    report = tmp_path / "report.json"

    def run(*, extra=(), remove=(), bearer=TOKEN, authenticated=True):
        config.write_text(json.dumps(metadata))
        command = [str(ROOT / "scripts/deploy-verify"), "--base-url", states[0]["url"], "--report", str(report)]
        if authenticated:
            command += ["--authenticated", "--ws-base-url", states[1]["url"], "--user-id", USER,
                        "--campus-id", CAMPUS, "--expected-schema", HEAD,
                        "--api-revision", states[0]["revision"], "--ws-revision", states[1]["revision"],
                        "--api-image-digest", DIGEST, "--ws-image-digest", DIGEST,
                        "--project", "fixture-project", "--gcloud", str(fake)]
        for option in remove:
            index = command.index(option)
            del command[index:index + 2]
        command += list(extra)
        env = {k: v for k, v in os.environ.items() if not k.startswith("DEPLOY_VERIFY_")}
        env.update(FAKE_CLOUD_CONFIG=str(config), FAKE_CLOUD_CALLS=str(calls))
        # c391: the wrapper resolves its interpreter from the checkout (CHIRP_PYTHON,
        # then backend/.venv, then PATH's python3) and refuses one with no CA
        # certificates. A worktree has no backend/.venv and PATH's python3 may be a
        # certless framework build, so pin the CLI under test to the interpreter
        # running this suite - the one interpreter known to exist here.
        env.setdefault("CHIRP_PYTHON", sys.executable)
        if bearer is not None:
            env["DEPLOY_VERIFY_BEARER"] = bearer
        result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=20)
        assert TOKEN not in result.stdout + result.stderr
        if report.exists():
            assert TOKEN not in report.read_text()
        return result, json.loads(result.stdout)

    yield states, metadata, run, report, calls
    for state in states:
        state["stop"].set()
    for server in servers:
        server.shutdown()
        server.server_close()
    for thread in threads:
        thread.join()


def test_ready_checks_both_services_and_rechecks_actual_traffic(peers):
    states, _, run, report, calls = peers
    result, body = run()
    assert result.returncode == 0, result.stdout
    assert body["verdict"] == "AUTHENTICATED_READY"
    assert len(body["checks"]) == 16
    assert len(body["services"]) == 2
    assert len(calls.read_text().splitlines()) == 8
    assert json.loads(report.read_text()) == body
    for state in states:
        assert ("/_deployment", "Bearer " + TOKEN) in state["requests"]
        assert (f"/campuses/{CAMPUS}/chirps?limit=1", "Bearer " + TOKEN) in state["requests"]


def test_routing_only_is_never_readiness(peers):
    states, _, run, _, calls = peers
    result, body = run(authenticated=False, bearer=None)
    assert result.returncode == 0 and body["verdict"] == "ROUTING_ONLY"
    assert not calls.exists()
    assert all(auth is None for _, auth in states[0]["requests"])
    assert not states[1]["requests"]


@pytest.mark.parametrize("bearer", ["expired-token", "invalid-token", "", "bad\nheader"])
def test_expired_invalid_empty_and_control_character_tokens_fail(peers, bearer):
    _, _, run, _, _ = peers
    result, body = run(bearer=bearer)
    assert result.returncode == 1 and body["verdict"] == "NOT_READY"


@pytest.mark.parametrize("service_index", [0, 1])
@pytest.mark.parametrize("field,value", [
    ("service", "wrong-service"), ("revision", "old-revision"),
    ("database_schema_heads", ["old-head"]), ("code_schema_heads", ["future-head"]),
    ("database_schema_heads", []), ("code_schema_heads", [HEAD, "split-head"]),
    ("database_schema_heads", None), ("code_schema_heads", None),
])
def test_missing_mismatched_and_multiple_heads_fail_on_either_service(peers, service_index, field, value):
    states, _, run, _, _ = peers

    def override(status, body, auth):
        if value is None:
            body.pop(field)
        else:
            body[field] = value
        return status, body, {}

    states[service_index]["overrides"]["/_deployment"] = override
    result, body = run()
    assert result.returncode == 1 and body["verdict"] == "NOT_READY"
    assert body["checks"][-1]["check"] == "deployment_schema_revision"


@pytest.mark.parametrize("case", ["missing_user_field", "missing_memberships", "wrong_user", "wrong_campus", "suspended", "wrong_feed_schema", "missing_vote", "invalid_vote", "author_leak", "campus_denied", "invalid_token_accepted", "wrong_routing", "response_echoes_secret", "redirect"])
def test_routing_and_200s_cannot_hide_auth_or_contract_failures(peers, case):
    states, _, run, _, _ = peers
    path = "/auth/me"

    def override(status, body, auth):
        if case == "invalid_token_accepted":
            return 200, copy.deepcopy(ME), {}
        if auth != "Bearer " + TOKEN:
            return status, body, {}
        if case == "missing_user_field":
            body["user"].pop("created_at")
        elif case == "missing_memberships":
            body.pop("memberships")
        elif case == "wrong_user":
            body["user"]["id"] = str(uuid.uuid4())
        elif case == "wrong_campus":
            body["user"]["campus_id"] = str(uuid.uuid4())
        elif case == "suspended":
            body["user"]["suspended_at"] = "2026-09-07T00:00:00Z"
        elif case == "response_echoes_secret":
            return 500, {"error": TOKEN}, {}
        elif case == "redirect":
            return 302, {}, {"Location": states[1]["url"] + "/stolen-token"}
        elif case == "wrong_feed_schema":
            body[0].pop("score")
        elif case == "missing_vote":
            body[0].pop("my_vote")
        elif case == "invalid_vote":
            body[0]["my_vote"] = True
        elif case == "author_leak":
            body[0]["author_id"] = USER
        elif case == "campus_denied":
            return 403, {}, {}
        return status, body, {}

    if case in ("wrong_feed_schema", "missing_vote", "invalid_vote", "author_leak", "campus_denied"):
        path = f"/campuses/{CAMPUS}/chirps?limit=1"
    if case == "wrong_routing":
        path = "/__deploy_verify_bogus_route_c186"
        override = lambda *args: (200, {}, {})
    states[0]["overrides"][path] = override
    result, body = run()
    assert result.returncode == 1 and body["verdict"] == "NOT_READY"
    assert not states[1]["requests"]  # Includes no redirected credential request.


@pytest.mark.parametrize("case", ["missing_traffic", "split_traffic", "tagged_old_revision", "only_90_percent", "wrong_origin", "wrong_digest", "missing_digest", "wrong_service_label", "missing_generation", "stale_generation", "not_ready", "missing_revision_metadata"])
def test_actual_cloud_metadata_must_account_for_serving_release(peers, case):
    states, metadata, run, _, _ = peers
    service, revision = metadata["chirp-api"], metadata["chirp-api-c361"]
    if case == "missing_traffic":
        service["status"].pop("traffic")
    elif case == "split_traffic":
        service["status"]["traffic"] = [{"revisionName": "chirp-api-c361", "percent": 99}, {"revisionName": "old", "percent": 1}]
    elif case == "tagged_old_revision":
        service["status"]["traffic"].append({"revisionName": "old", "tag": "stale"})
    elif case == "only_90_percent":
        service["status"]["traffic"][0]["percent"] = 90
    elif case == "wrong_origin":
        service["status"]["url"] = "https://another-service.example.com"
    elif case == "wrong_digest":
        revision["status"]["imageDigest"] = "sha256:" + "b" * 64
    elif case == "missing_digest":
        revision["status"].pop("imageDigest")
    elif case == "wrong_service_label":
        revision["metadata"]["labels"]["serving.knative.dev/service"] = "another-service"
    elif case == "missing_generation":
        service["status"].pop("observedGeneration")
    elif case == "stale_generation":
        service["status"]["observedGeneration"] = 2
    elif case == "not_ready":
        revision["status"]["conditions"][0]["status"] = "False"
    elif case == "missing_revision_metadata":
        metadata.pop("chirp-api-c361")
    result, body = run()
    assert result.returncode == 1 and body["verdict"] == "NOT_READY"
    assert not states[0]["requests"] and not states[1]["requests"]


@pytest.mark.parametrize("extra,remove", [
    (("--bearer", TOKEN), ()), ((), ("--project",)), ((), ("--api-image-digest",)),
    (("--campus-id", "00000000-0000-0000-0000-000000000000"), ()),
    (("--campus-id", "//another-host.example"), ()),
    (("--base-url", "https://name:secret@example.com"), ()),
    (("--base-url", "https://example.com?token=secret"), ()),
    (("--base-url", "http://example.com"), ()),
])
def test_missing_inputs_unsafe_urls_and_legacy_token_args_fail_without_echo(peers, extra, remove):
    states, _, run, _, _ = peers
    result, body = run(extra=extra, remove=remove)
    assert result.returncode == 1 and body["verdict"] == "NOT_READY"
    assert "secret" not in result.stdout + result.stderr
    assert not states[0]["requests"] and not states[1]["requests"]


def test_rollout_after_http_probes_is_caught_by_metadata_recheck(peers):
    states, metadata, run, _, _ = peers

    def rollout(status, body, auth):
        metadata["chirp-api"]["status"]["traffic"] = [{"revisionName": "rolled-out", "percent": 100}]
        states[0]["cloud_config"].write_text(json.dumps(metadata))
        return status, body, {}

    states[1]["overrides"][f"/campuses/{CAMPUS}/chirps?limit=1"] = rollout
    result, body = run()
    assert result.returncode == 1 and body["verdict"] == "NOT_READY"
    assert all(item["passed"] for item in body["checks"])
    assert len(body["checks"]) == 16


def test_empty_authorized_campus_is_valid_but_not_a_row_serialization_proof(peers):
    states, _, run, _, _ = peers
    for state in states:
        state["overrides"][f"/campuses/{CAMPUS}/chirps?limit=1"] = lambda *args: (200, [], {})
    result, body = run()
    assert result.returncode == 0 and body["verdict"] == "AUTHENTICATED_READY"


def test_slow_stream_has_a_total_deadline_and_cannot_hold_cli_open(peers):
    states, _, run, _, _ = peers
    states[0]["slow_stream"] = True
    started = time.monotonic()
    result, body = run()
    assert time.monotonic() - started < 18  # 10s whole-operation deadline + process overhead.
    assert result.returncode == 1 and body["verdict"] == "NOT_READY"
    assert body["checks"][-1] == {"service": "chirp-api", "check": "authenticated_fixture", "passed": False, "status": 0}
    assert not states[1]["requests"]
