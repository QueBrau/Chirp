"""Bounded c363 synthetic Firebase QA provisioning.

The default command is plan-only.  Execution requires an explicit plan digest,
``--execute``, the existing ``chirps-prod`` Firebase project, a private manifest
path, and an operator-supplied bootstrap endpoint/database proxy.  No credential
or token is ever printed.  This module is deliberately separate from the local
``seed_loadtest`` script; it cannot reuse that script's local-only data.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
from typing import Any, Callable, Iterator, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, urlopen
import uuid

MAX_USERS = 200
PROJECT_ID = "chirps-prod"
BOOTSTRAP_ORIGIN = "https://chirp-api-593616178468.us-central1.run.app"
TOKEN_MIN_REMAINING_SECONDS = 15 * 60
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,47}[a-z0-9])?$")
UID_RE = re.compile(r"^c363-[0-9a-f]{16}-[0-9]{3}$")


class ProvisioningError(RuntimeError):
    """Sanitized, operator-actionable failure; never contains provider payloads."""


class QuarantineRequired(ProvisioningError):
    """A provider response may be ambiguous; never retry or adopt by lookup."""


class FirebaseClient(Protocol):
    def create_user(self, **kwargs: Any) -> Any: ...


class TokenClient(Protocol):
    def __call__(self, *, email: str, password: str) -> Mapping[str, Any]: ...


class BootstrapClient(Protocol):
    def __call__(self, *, token: str, email: str, display_name: str) -> Mapping[str, Any]: ...


class OwnershipVerifier(Protocol):
    def __call__(self, *, plan: "Plan", users: list[Mapping[str, Any]]) -> Mapping[str, str]: ...


class _FirebaseAdminAdapter:
    def __init__(self, auth_module: Any) -> None:
        self._auth = auth_module

    def create_user(self, **kwargs: Any) -> Any:
        kwargs.pop("project_id", None)
        return self._auth.create_user(**kwargs)


def firebase_admin_client(*, project_id: str = PROJECT_ID,
                          credentials_path: str | None = None) -> FirebaseClient:
    """Create an explicit Firebase Admin client pinned to the deployed project.

    ADC is used only when the operator deliberately calls this function without a
    certificate path.  The CLI never calls it implicitly, so plan mode cannot
    discover credentials or contact Firebase.
    """
    if project_id != PROJECT_ID:
        raise ProvisioningError("firebase_project_mismatch")
    if os.environ.get("FIREBASE_AUTH_EMULATOR_HOST"):
        raise ProvisioningError("firebase_emulator_forbidden")
    try:
        import firebase_admin
        from firebase_admin import auth
        from firebase_admin import credentials
    except ImportError:
        raise ProvisioningError("firebase_admin_unavailable") from None
    try:
        app = firebase_admin.get_app()
    except ValueError:
        credential = credentials.Certificate(credentials_path) if credentials_path else None
        options = {"projectId": PROJECT_ID, "httpTimeout": 15}
        app = firebase_admin.initialize_app(credential, options=options)
    configured = getattr(app, "project_id", None)
    if configured != PROJECT_ID:
        raise ProvisioningError("firebase_project_mismatch")
    return _FirebaseAdminAdapter(auth)


@dataclass(frozen=True)
class Plan:
    schema_version: int
    run_id: str
    project_id: str
    campus_slug: str
    chapter_slug: str
    user_count: int
    source_digest: str
    image_digest: str
    config_digest: str
    created_at: str
    plan_digest: str


@dataclass(frozen=True)
class UserPlan:
    uid: str
    email: str
    display_name: str


def _sha256(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ProvisioningError("digest_invalid")
    return value


def _slug(value: str, field: str) -> str:
    if not isinstance(value, str) or not SLUG_RE.fullmatch(value) or value.startswith("load-"):
        raise ProvisioningError(f"{field}_invalid")
    return value


def _uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise ProvisioningError("run_id_invalid") from None


def _plan_body(**kwargs: Any) -> dict[str, Any]:
    return {"schema_version": 1, **kwargs}


def make_plan(*, run_id: str, user_count: int, campus_slug: str, chapter_slug: str,
              source_digest: str, image_digest: str, config_digest: str,
              now: datetime | None = None) -> Plan:
    run_id = _uuid(run_id)
    if type(user_count) is not int or not 1 <= user_count <= MAX_USERS:
        raise ProvisioningError("user_count_cap_exceeded")
    campus_slug = _slug(campus_slug, "campus_slug")
    chapter_slug = _slug(chapter_slug, "chapter_slug")
    source_digest, image_digest, config_digest = (_sha256(x) for x in (source_digest, image_digest, config_digest))
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0).isoformat()
    raw = _plan_body(run_id=run_id, project_id=PROJECT_ID, campus_slug=campus_slug,
                     chapter_slug=chapter_slug, user_count=user_count,
                     source_digest=source_digest, image_digest=image_digest,
                     config_digest=config_digest, created_at=timestamp)
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return Plan(**raw, plan_digest=digest)


def users_for_plan(plan: Plan) -> tuple[UserPlan, ...]:
    run_hex = uuid.UUID(plan.run_id).hex[:16]
    users = tuple(UserPlan(uid=f"c363-{run_hex}-{i:03d}",
                           email=f"c363-{run_hex}-{i:03d}@loadtest.invalid",
                           display_name=f"C363 synthetic {i:03d}")
                  for i in range(1, plan.user_count + 1))
    if len({u.uid for u in users}) != len(users) or any(not UID_RE.fullmatch(u.uid) for u in users):
        raise ProvisioningError("generated_identity_collision")
    return users


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _atomic_private_json(path: Path, value: Mapping[str, Any], *, refuse_existing: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_EXCL if refuse_existing else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(_json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


@contextmanager
def _run_lock(path: Path) -> Iterator[None]:
    lock = path.with_name(path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ProvisioningError("provisioning_already_running") from None
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def plan_document(plan: Plan) -> dict[str, Any]:
    return {"kind": "c363_synthetic_firebase_plan", **asdict(plan),
            "users": [asdict(u) for u in users_for_plan(plan)],
            "execute_requires": ["--execute", "--confirm-plan-digest", plan.plan_digest,
                                 "chirps-prod", "--manifest", "--database-url",
                                 "--identity-tool-api-key-file"]}


def load_plan(path: Path) -> Plan:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        fields = {key: raw[key] for key in ("schema_version", "run_id", "project_id", "campus_slug",
                                            "chapter_slug", "user_count", "source_digest", "image_digest",
                                            "config_digest", "created_at")}
        if fields["schema_version"] != 1 or raw.get("project_id") != PROJECT_ID:
            raise KeyError("plan")
        expected = hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if raw.get("plan_digest") != expected:
            raise ProvisioningError("plan_digest_invalid")
        return Plan(**fields, plan_digest=expected)
    except ProvisioningError:
        raise
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ProvisioningError("plan_unreadable") from None


def postgres_fixture_ownership(*, database_url: str, plan: Plan,
                               allow_test_database: bool = False) -> OwnershipVerifier:
    """Return a local/proxy-only transactional synthetic enrollment verifier."""
    parsed = urlsplit(database_url)
    if parsed.scheme != "postgresql+asyncpg" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ProvisioningError("ownership_database_must_be_local_proxy")
    if (parsed.port or 5432) != 5433 and not allow_test_database:
        raise ProvisioningError("ownership_database_requires_test_opt_in")

    def verify(*, plan: Plan, users: list[Mapping[str, Any]]) -> Mapping[str, str]:
        try:
            from sqlalchemy import select
            from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
            from app.models import Campus, Chapter, Membership, User
        except ImportError:
            raise ProvisioningError("ownership_database_unavailable") from None

        async def enroll() -> Mapping[str, str]:
            engine = create_async_engine(database_url, pool_pre_ping=True, pool_timeout=10)
            try:
                factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
                async with factory() as session:
                    async with session.begin():
                        existing = (await session.execute(
                            select(Campus).where(Campus.slug == plan.campus_slug).with_for_update()
                        )).scalar_one_or_none()
                        if existing is not None:
                            raise ProvisioningError("synthetic_campus_collision")
                        campus = Campus(name=f"C363 synthetic {plan.run_id[:8]}",
                                        slug=plan.campus_slug, email_domains=None)
                        session.add(campus)
                        await session.flush()
                        chapter = Chapter(campus_id=campus.id, org_name="C363 synthetic QA",
                                           chapter_name=plan.chapter_slug)
                        session.add(chapter)
                        await session.flush()
                        now = datetime.now(timezone.utc)
                        for item in users:
                            row = (await session.execute(
                                select(User).where(User.firebase_uid == item.get("uid")).with_for_update()
                            )).scalar_one_or_none()
                            if row is None or row.campus_id is not None:
                                raise ProvisioningError("synthetic_user_binding_invalid")
                            row.campus_id = campus.id
                            row.campus_verified_at = now
                            session.add(Membership(user_id=row.id, chapter_id=chapter.id,
                                                   role="member", status="active"))
                        await session.flush()
                        bound = (await session.execute(
                            select(User.firebase_uid, Membership.chapter_id)
                            .join(Membership, Membership.user_id == User.id)
                            .where(User.campus_id == campus.id,
                                   Membership.chapter_id == chapter.id,
                                   Membership.status == "active")
                        )).all()
                        expected = {str(item["uid"]) for item in users}
                        actual = {str(uid) for uid, _ in bound}
                        if actual != expected or len(bound) != len(expected):
                            raise ProvisioningError("synthetic_enrollment_readback_failed")
                        return {"campus_id": str(campus.id), "chapter_id": str(chapter.id)}
            finally:
                await engine.dispose()

        import asyncio
        try:
            return asyncio.run(enroll())
        except ProvisioningError:
            raise
        except Exception:
            raise ProvisioningError("synthetic_enrollment_failed") from None

    return verify


def _no_redirect_opener():
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise ProvisioningError("provider_redirect_refused")
    return build_opener(NoRedirect)


def _read_json(response, *, max_bytes: int = 256 * 1024) -> Mapping[str, Any]:
    raw = response.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ProvisioningError("provider_response_too_large")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ProvisioningError("provider_response_invalid_json") from None
    if not isinstance(value, dict):
        raise ProvisioningError("provider_response_invalid_shape")
    return value


def identity_toolkit_token_client(*, api_key: str, project_id: str = PROJECT_ID,
                                  timeout_seconds: float = 15.0) -> TokenClient:
    if not api_key or not re.fullmatch(r"[A-Za-z0-9_-]{20,256}", api_key):
        raise ProvisioningError("identity_tool_api_key_invalid")
    if project_id != PROJECT_ID:
        raise ProvisioningError("firebase_project_mismatch")

    def issue(*, email: str, password: str) -> Mapping[str, Any]:
        body = json.dumps({"email": email, "password": password, "returnSecureToken": True}).encode()
        request = Request("https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?" + urlencode({"key": api_key}), data=body, method="POST", headers={"Content-Type": "application/json"})
        try:
            with _no_redirect_opener().open(request, timeout=timeout_seconds) as response:
                return _read_json(response)
        except HTTPError:
            raise ProvisioningError("identity_tool_request_failed") from None
        except (URLError, TimeoutError):
            raise ProvisioningError("identity_tool_request_unavailable") from None
    return issue


def firebase_token_verifier(*, project_id: str = PROJECT_ID) -> Callable[[str], Mapping[str, Any]]:
    """Return the Admin SDK verifier pinned to the same existing project."""
    if project_id != PROJECT_ID:
        raise ProvisioningError("firebase_project_mismatch")
    try:
        import firebase_admin
        from firebase_admin import auth
    except ImportError:
        raise ProvisioningError("firebase_admin_unavailable") from None
    try:
        app = firebase_admin.get_app()
    except ValueError:
        raise ProvisioningError("firebase_admin_not_initialized") from None
    if getattr(app, "project_id", None) != PROJECT_ID:
        raise ProvisioningError("firebase_project_mismatch")

    def verify(token: str) -> Mapping[str, Any]:
        try:
            return auth.verify_id_token(token, app=app, check_revoked=True)
        except Exception:
            raise ProvisioningError("token_verification_failed") from None
    return verify


def _verify_token(result: Mapping[str, Any], *, expected_uid: str, now: datetime,
                  verifier: Callable[[str], Mapping[str, Any]]) -> tuple[str, str]:
    token = result.get("idToken")
    local_id = result.get("localId")
    if not isinstance(token, str) or not token or local_id != expected_uid:
        raise QuarantineRequired("token_identity_mismatch")
    try:
        decoded = verifier(token)
        exp = int(decoded["exp"])
        uid = decoded["uid"]
        aud = decoded["aud"]
    except (KeyError, TypeError, ValueError, OverflowError):
        raise QuarantineRequired("token_verification_failed") from None
    if uid != expected_uid or aud != PROJECT_ID:
        raise QuarantineRequired("token_project_or_identity_mismatch")
    if exp < int(now.timestamp()) + TOKEN_MIN_REMAINING_SECONDS:
        raise ProvisioningError("token_expiry_window_too_short")
    return token, datetime.fromtimestamp(exp, timezone.utc).isoformat()


def provision_users(*, plan: Plan, firebase: FirebaseClient, token_client: TokenClient,
                    token_verifier: Callable[[str], Mapping[str, Any]],
                    bootstrap: BootstrapClient, verify_ownership: OwnershipVerifier,
                    manifest_path: Path | None = None,
                    now: datetime | None = None) -> dict[str, Any]:
    if plan.project_id != PROJECT_ID:
        raise ProvisioningError("firebase_project_mismatch")
    if manifest_path is None:
        raise ProvisioningError("manifest_required")
    users = users_for_plan(plan)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not callable(verify_ownership):
        raise ProvisioningError("ownership_verifier_required")
    created: list[dict[str, Any]] = []
    manifest_base = {"schema_version": 1, "kind": "c363_synthetic_firebase_manifest",
                     "project_id": PROJECT_ID, "plan_digest": plan.plan_digest,
                     "run_id": plan.run_id, "campus_slug": plan.campus_slug,
                     "chapter_slug": plan.chapter_slug, "created_at": current.isoformat(),
                     "fixture_provenance": "synthetic_only"}
    row_state = [{"uid": user.uid, "status": "pending"} for user in users]
    if manifest_path is not None:
        _atomic_private_json(manifest_path, {**manifest_base, "status": "RUNNING", "users": row_state})
    try:
        for user in users:
            state = next(row for row in row_state if row["uid"] == user.uid)
            password = secrets.token_urlsafe(32)
            try:
                record = firebase.create_user(uid=user.uid, email=user.email, password=password,
                                              display_name=user.display_name, project_id=PROJECT_ID)
            except Exception:
                # A network/SDK exception may follow a successful provider write. Never lookup
                # and adopt or retry: quarantine the run for operator reconciliation.
                raise QuarantineRequired("firebase_create_unknown") from None
            if getattr(record, "uid", None) != user.uid:
                raise QuarantineRequired("firebase_create_identity_mismatch")
            state["status"] = "firebase_created"
            if manifest_path is not None:
                _atomic_private_json(manifest_path, {**manifest_base, "status": "RUNNING", "users": row_state}, refuse_existing=False)
            result = token_client(email=user.email, password=password)
            token, expires_at = _verify_token(result, expected_uid=user.uid, now=current, verifier=token_verifier)
            state.update({"status": "token_issued", "expires_at": expires_at})
            if manifest_path is not None:
                _atomic_private_json(manifest_path, {**manifest_base, "status": "RUNNING", "users": row_state}, refuse_existing=False)
            try:
                response = bootstrap(token=token, email=user.email, display_name=user.display_name)
            except Exception:
                raise QuarantineRequired("bootstrap_unknown") from None
            if response.get("firebase_uid") != user.uid or not response.get("id"):
                raise QuarantineRequired("bootstrap_identity_mismatch")
            created.append({"uid": user.uid, "id_token": token, "expires_at": expires_at,
                           "user_id": str(response["id"])})
            state.update({"status": "bootstrapped", "user_id": str(response["id"])})
            if manifest_path is not None:
                _atomic_private_json(manifest_path, {**manifest_base, "status": "RUNNING", "users": row_state,
                                                     "credentials": created}, refuse_existing=False)
        ownership = verify_ownership(plan=plan, users=created)
        if not isinstance(ownership, Mapping) or not re.fullmatch(r"[0-9a-f-]{36}", str(ownership.get("campus_id", ""))) or not re.fullmatch(r"[0-9a-f-]{36}", str(ownership.get("chapter_id", ""))):
            raise ProvisioningError("ownership_ids_invalid")
        manifest_base.update({"campus_id": str(ownership["campus_id"]), "chapter_id": str(ownership["chapter_id"])})
    except ProvisioningError as exc:
        if manifest_path is not None:
            _atomic_private_json(manifest_path, {**manifest_base, "status": "QUARANTINED",
                                                 "reason": str(exc), "users": row_state,
                                                 "credentials": created}, refuse_existing=False)
        raise
    except Exception:
        if manifest_path is not None:
            _atomic_private_json(manifest_path, {**manifest_base, "status": "QUARANTINED",
                                                 "reason": "ownership_or_provisioning_failed", "users": row_state,
                                                 "credentials": created}, refuse_existing=False)
        raise ProvisioningError("ownership_or_provisioning_failed") from None
    result = {**manifest_base, "status": "COMPLETE", "users": created}
    if manifest_path is not None:
        _atomic_private_json(manifest_path, result, refuse_existing=False)
    return result


def bootstrap_http_client(*, base_url: str, timeout_seconds: float = 15.0) -> BootstrapClient:
    parsed = urlsplit(base_url)
    if base_url.rstrip("/") != BOOTSTRAP_ORIGIN or parsed.query or parsed.fragment:
        raise ProvisioningError("bootstrap_url_invalid")
    endpoint = BOOTSTRAP_ORIGIN + "/auth/bootstrap"

    def bootstrap(*, token: str, email: str, display_name: str) -> Mapping[str, Any]:
        body = json.dumps({"email": email, "display_name": display_name, "account_type": "non_greek"}).encode()
        request = Request(endpoint, data=body, method="POST", headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        try:
            with _no_redirect_opener().open(request, timeout=timeout_seconds) as response:
                if response.status != 201:
                    raise ProvisioningError("bootstrap_rejected")
                return _read_json(response)
        except HTTPError:
            raise ProvisioningError("bootstrap_rejected") from None
        except (URLError, TimeoutError):
            raise QuarantineRequired("bootstrap_unknown") from None
    return bootstrap


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--run-id", default=str(uuid.uuid4()))
    parser.add_argument("--users", type=int, default=200)
    parser.add_argument("--campus-slug", required=True)
    parser.add_argument("--chapter-slug", required=True)
    parser.add_argument("--source-digest", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--config-digest", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--manifest")
    parser.add_argument("--database-url")
    parser.add_argument("--firebase-credentials")
    parser.add_argument("--identity-tool-api-key-file")
    parser.add_argument("--bootstrap-url", default=BOOTSTRAP_ORIGIN)
    parser.add_argument("--allow-test-database", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-plan-digest")
    args = parser.parse_args(argv)
    try:
        plan_path = Path(args.plan)
        with _run_lock(plan_path):
            if not args.execute:
                plan = make_plan(run_id=args.run_id, user_count=args.users, campus_slug=args.campus_slug,
                                 chapter_slug=args.chapter_slug, source_digest=args.source_digest,
                                 image_digest=args.image_digest, config_digest=args.config_digest)
                _atomic_private_json(plan_path, plan_document(plan))
                print(json.dumps({"status": "PLAN_ONLY", "plan_digest": plan.plan_digest,
                                  "users": plan.user_count, "project": PROJECT_ID}))
                return 0
            plan = load_plan(plan_path)
            if args.confirm_plan_digest != plan.plan_digest:
                raise ProvisioningError("plan_digest_confirmation_required")
            if not args.manifest or not args.database_url or not args.identity_tool_api_key_file:
                raise ProvisioningError("execution_inputs_required")
            key_path = Path(args.identity_tool_api_key_file)
            try:
                mode = key_path.stat().st_mode & 0o777
                api_key = key_path.read_text(encoding="utf-8").strip()
            except OSError:
                raise ProvisioningError("identity_tool_api_key_unreadable") from None
            if mode & 0o077 or not api_key:
                raise ProvisioningError("identity_tool_api_key_permissions")
            firebase = firebase_admin_client(credentials_path=args.firebase_credentials)
            token_client = identity_toolkit_token_client(api_key=api_key)
            verifier = firebase_token_verifier()
            bootstrap = bootstrap_http_client(base_url=args.bootstrap_url)
            ownership = postgres_fixture_ownership(database_url=args.database_url, plan=plan,
                                                   allow_test_database=args.allow_test_database)
            result = provision_users(plan=plan, firebase=firebase, token_client=token_client,
                                     token_verifier=verifier, bootstrap=bootstrap,
                                     verify_ownership=ownership, manifest_path=Path(args.manifest))
            print(json.dumps({"status": result["status"], "plan_digest": plan.plan_digest,
                              "users": len(result["users"]), "project": PROJECT_ID}))
            return 0
    except ProvisioningError as exc:
        print(json.dumps({"status": "REFUSED", "reason": str(exc)}))
        return 2
    except Exception:
        print(json.dumps({"status": "NOT_PROVEN", "reason": "provisioning_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
