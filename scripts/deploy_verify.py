"""Read-only deployment verification. Credentials enter only through the environment.

HTTP bodies and subprocess diagnostics are untrusted and never enter the report.
Run through scripts/deploy-verify; see DEPLOY-VERIFICATION.md for the contract.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid

NIL = "00000000-0000-0000-0000-000000000000"
MAX_BODY = 1024 * 1024
HTTP_DEADLINE_SECONDS = 10


class VerificationError(Exception):
    """Only fixed, non-secret messages may be raised here."""


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's usual error echoes unknown arguments, including old --bearer.
        raise VerificationError("Invalid arguments; use --help. Bearer credentials belong only in DEPLOY_VERIFY_BEARER.")


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def origin(value: str) -> str:
    try:
        url = urllib.parse.urlsplit(value)
        port = url.port  # force validation
        if (not url.hostname or url.username is not None or url.password is not None
                or url.path not in ("", "/") or url.query or url.fragment
                or any(ord(c) < 33 or ord(c) > 126 for c in value)
                or (port is not None and port <= 0)
                or (url.scheme != "https" and not (
                    url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1", "::1")))):
            raise ValueError
        return value.rstrip("/")
    except ValueError:
        raise VerificationError("Service URL must be an HTTPS origin (HTTP allowed only for loopback tests).") from None


def _request_once(base: str, path: str, bearer: str | None) -> tuple[int, object]:
    headers = {"Accept": "application/json"}
    if bearer is not None:
        headers["Authorization"] = "Bearer " + bearer
    # Paths are fixed literals or contain validated UUIDs, never caller URLs.
    req = urllib.request.Request(base + path, headers=headers)
    try:
        with urllib.request.build_opener(NoRedirects()).open(req, timeout=10) as response:
            raw = response.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                return response.status, None
            try:
                return response.status, json.loads(raw)
            except (ValueError, UnicodeError):
                return response.status, None
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        return status, None
    except Exception:
        # Transport/header errors can embed credentials. Never print the exception.
        return 0, None


def request(base: str, path: str, bearer: str | None = None) -> tuple[int, object]:
    # urllib's socket timeout is only an inactivity timeout: a trickle response
    # could otherwise hold response.read open forever. Bound the entire operation,
    # including DNS/open/body parsing. A failed probe ends this CLI immediately;
    # the daemon cannot keep the process alive if its socket remains blocked.
    result = queue.SimpleQueue()
    worker = threading.Thread(target=lambda: result.put(_request_once(base, path, bearer)), daemon=True)
    worker.start()
    try:
        return result.get(timeout=HTTP_DEADLINE_SECONDS)
    except queue.Empty:
        return 0, None


def is_uuid(value: object) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value and value != NIL
    except ValueError:
        return False


def is_time(value: object) -> bool:
    try:
        return isinstance(value, str) and dt.datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


def valid_me(body: object, user_id: str, campus_id: str) -> bool:
    if not isinstance(body, dict) or not isinstance(body.get("user"), dict) or not isinstance(body.get("memberships"), list):
        return False
    user = body["user"]
    if not ({"id", "firebase_uid", "email", "display_name", "avatar_url", "account_type", "campus_id", "is_ghost", "is_platform_admin", "suspended_at", "created_at"} <= user.keys()
            and user["id"] == user_id and user["campus_id"] == campus_id
            and all(isinstance(user[k], str) and bool(user[k]) for k in ("firebase_uid", "email", "display_name"))
            and user["account_type"] in ("greek", "non_greek", "alumni")
            and type(user["is_ghost"]) is bool and type(user["is_platform_admin"]) is bool
            and user["suspended_at"] is None and is_time(user["created_at"])
            and (user["avatar_url"] is None or isinstance(user["avatar_url"], str))):
        return False
    for row in body["memberships"]:
        if not (isinstance(row, dict) and {"id", "user_id", "chapter_id", "role", "status", "joined_at"} <= row.keys()
                and is_uuid(row["id"]) and is_uuid(row["chapter_id"]) and row["user_id"] == user_id
                and row["status"] == "active" and row["role"] in ("president", "vice_president", "treasurer", "secretary", "historian", "member", "pledge", "alumni")
                and is_time(row["joined_at"])):
            return False
    return True


def valid_feed(body: object, campus_id: str) -> bool:
    if not isinstance(body, list) or len(body) > 1:
        return False
    return all(
        isinstance(row, dict) and {"id", "campus_id", "body", "score", "created_at", "my_vote"} <= row.keys()
        and is_uuid(row["id"]) and row["campus_id"] == campus_id
        and isinstance(row["body"], str) and type(row["score"]) is int and is_time(row["created_at"])
        and (row["my_vote"] is None or (type(row["my_vote"]) is int and row["my_vote"] in (-1, 1)))
        and not {"author_id", "author", "user_id"}.intersection(row)
        for row in body
    )


def cloud_json(args, resource: str, name: str, fields: str) -> dict:
    child_env = os.environ.copy()
    child_env.pop("DEPLOY_VERIFY_BEARER", None)
    child_env["CLOUDSDK_CORE_DISABLE_FILE_LOGGING"] = "1"
    try:
        completed = subprocess.run(
            [args.gcloud, "run", resource, "describe", name, "--project", args.project,
             "--region", args.region, "--format", "json(" + fields + ")"],
            capture_output=True, timeout=30, env=child_env, check=False,
        )
        if completed.returncode != 0 or len(completed.stdout) > MAX_BODY:
            raise ValueError
        result = json.loads(completed.stdout)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except Exception:
        raise VerificationError("Cloud Run metadata unavailable or malformed.") from None


def ready_and_observed(body: dict) -> bool:
    metadata, status = body.get("metadata"), body.get("status")
    if not isinstance(metadata, dict) or not isinstance(status, dict):
        return False
    generation, observed = metadata.get("generation"), status.get("observedGeneration")
    # gcloud can serialize int64 fields as strings. Require the same positive value.
    if not (str(generation).isdigit() and int(generation) > 0 and str(observed) == str(generation)):
        return False
    conditions = status.get("conditions")
    if not isinstance(conditions, list):
        return False
    ready = [c for c in conditions if isinstance(c, dict) and c.get("type") == "Ready"]
    return len(ready) == 1 and ready[0].get("status") == "True"


def cloud_metadata(args, service: str, revision: str, digest: str, base: str) -> dict:
    body = cloud_json(args, "services", service, "metadata.name,metadata.generation,status.url,status.traffic,status.conditions,status.observedGeneration")
    if not ready_and_observed(body):
        raise VerificationError("Cloud Run service is not ready at its current generation.")
    if body["metadata"].get("name") != service or body["status"].get("url") != base:
        raise VerificationError("Probe origin does not match the expected Cloud Run service.")
    traffic = body["status"].get("traffic")
    if not isinstance(traffic, list) or not traffic:
        raise VerificationError("Cloud Run serving traffic is missing.")
    accounted = []
    for row in traffic:
        if (not isinstance(row, dict) or row.get("revisionName") != revision
                or type(row.get("percent", 0)) is not int or not 0 <= row.get("percent", 0) <= 100):
            raise VerificationError("Cloud Run traffic includes an unaccounted revision.")
        accounted.append({"revision": revision, "percent": row.get("percent", 0)})
    if sum(row["percent"] for row in accounted) != 100:
        raise VerificationError("Cloud Run traffic does not account for 100 percent.")
    rev = cloud_json(args, "revisions", revision, "metadata.name,metadata.labels,metadata.generation,status.imageDigest,status.conditions,status.observedGeneration")
    if not ready_and_observed(rev):
        raise VerificationError("Cloud Run revision is not ready at its current generation.")
    if (rev["metadata"].get("name") != revision
            or not isinstance(rev["metadata"].get("labels"), dict)
            or rev["metadata"]["labels"].get("serving.knative.dev/service") != service):
        raise VerificationError("Cloud Run revision does not belong to the expected service.")
    actual_digest = rev["status"].get("imageDigest")
    if not isinstance(actual_digest, str) or actual_digest.split("@")[-1] != digest:
        raise VerificationError("Cloud Run resolved image digest differs from the expected release.")
    return {"service": service, "revision": revision, "image_digest": digest, "traffic": accounted}


def check(report: dict, service: str, label: str, passed: bool, status: int | None = None):
    item = {"service": service, "check": label, "passed": passed}
    if status is not None:
        item["status"] = status
    report["checks"].append(item)
    if not passed:
        raise VerificationError("A deployment probe failed; see the named check and HTTP status.")


def routing(report: dict, label: str, base: str):
    for name, path, expected in (
        ("auth_gate", "/auth/campus-verification", (401,)),
        ("missing_route", "/__deploy_verify_bogus_route_c186", (404,)),
        ("retired_yaks_route", f"/campuses/{NIL}/yaks", (404,)),
        ("chirps_route", f"/campuses/{NIL}/chirps", (200, 401)),
    ):
        status, _ = request(base, path)
        check(report, label, name, status in expected, status)


def parser():
    p = SafeParser(description="Read-only deployment checks. ROUTING_ONLY is not readiness. Credentials: DEPLOY_VERIFY_BEARER environment only.", allow_abbrev=False)
    p.add_argument("--authenticated", action="store_true")
    for name, default in (
        ("base-url", os.environ.get("DEPLOY_VERIFY_BASE_URL", "http://localhost:8000")),
        ("ws-base-url", os.environ.get("DEPLOY_VERIFY_WS_BASE_URL")),
        ("user-id", os.environ.get("DEPLOY_VERIFY_USER_ID")),
        ("campus-id", os.environ.get("DEPLOY_VERIFY_CAMPUS_ID")),
        ("expected-schema", os.environ.get("DEPLOY_VERIFY_EXPECTED_SCHEMA")),
        ("api-revision", None), ("ws-revision", None),
        ("api-image-digest", None), ("ws-image-digest", None),
        ("api-service", "chirp-api"), ("ws-service", "chirp-ws"),
        ("project", os.environ.get("DEPLOY_VERIFY_PROJECT")),
        ("region", "us-central1"), ("gcloud", "gcloud"), ("report", None),
    ):
        p.add_argument("--" + name, default=default)
    return p


def verify(args, report: dict):
    base = origin(args.base_url)
    report["targets"] = [{"service": "api", "origin": base}]
    bearer = os.environ.get("DEPLOY_VERIFY_BEARER", "")
    if not args.authenticated and not bearer:
        routing(report, "api", base)
        report["verdict"] = "ROUTING_ONLY"
        return
    if not bearer or len(bearer) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in bearer):
        raise VerificationError("Authenticated verification requires a valid DEPLOY_VERIFY_BEARER environment value.")
    if not is_uuid(args.user_id) or not is_uuid(args.campus_id):
        raise VerificationError("Authenticated verification requires known nonzero user and campus UUIDs.")
    for value in (args.project, args.region, args.expected_schema, args.api_service, args.ws_service, args.api_revision, args.ws_revision):
        if not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise VerificationError("Explicit project, region, schema head, service names and revisions are required.")
    for digest in (args.api_image_digest, args.ws_image_digest):
        if not digest or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise VerificationError("Both expected release image digests must be sha256 values.")
    if not args.ws_base_url:
        raise VerificationError("The WebSocket service origin is required.")
    ws_base = origin(args.ws_base_url)
    if base == ws_base or args.api_service == args.ws_service:
        raise VerificationError("API and WebSocket services must be verified independently.")
    targets = ((args.api_service, args.api_revision, args.api_image_digest, base),
               (args.ws_service, args.ws_revision, args.ws_image_digest, ws_base))
    report["targets"] = [{"service": service, "origin": url} for service, _, _, url in targets]
    for service, revision, digest, url in targets:
        # Validate the origin against control-plane metadata before sending a token.
        report["services"].append(cloud_metadata(args, service, revision, digest, url))
        routing(report, service, url)
        status, _ = request(url, "/auth/me", "invalid-c361-deployment-probe")
        check(report, service, "invalid_bearer_rejected", status == 401, status)
        status, me = request(url, "/auth/me", bearer)
        check(report, service, "authenticated_fixture", status == 200 and valid_me(me, args.user_id, args.campus_id), status)
        status, deployment = request(url, "/_deployment", bearer)
        check(report, service, "deployment_schema_revision", status == 200 and isinstance(deployment, dict)
              and deployment.get("service") == service and deployment.get("revision") == revision
              and deployment.get("code_schema_heads") == [args.expected_schema]
              and deployment.get("database_schema_heads") == [args.expected_schema], status)
        status, feed = request(url, f"/campuses/{args.campus_id}/chirps?limit=1", bearer)
        check(report, service, "authenticated_campus_chirps", status == 200 and valid_feed(feed, args.campus_id), status)
    # Reject an observed rollout during the probe window, including new split traffic.
    for target, before in zip(targets, report["services"]):
        if cloud_metadata(args, *target) != before:
            raise VerificationError("Cloud Run deployment changed during verification; rerun against a stable release.")
    report["schema_head"] = args.expected_schema
    report["verdict"] = "AUTHENTICATED_READY"


def main(argv=None) -> int:
    report = {"verdict": "NOT_READY", "checks": [], "services": []}
    args = None
    try:
        args = parser().parse_args(argv)
        verify(args, report)
    except VerificationError as exc:
        report["failure"] = str(exc)  # fixed messages only; never raw exceptions
    except Exception:
        report["failure"] = "Verification could not complete; no readiness claim was made."
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args and args.report:
        try:
            Path(args.report).write_text(rendered + "\n")
        except OSError:
            print("Could not write the verification report.", file=sys.stderr)
            return 1
    return 0 if report["verdict"] in ("ROUTING_ONLY", "AUTHENTICATED_READY") else 1


if __name__ == "__main__":
    raise SystemExit(main())
