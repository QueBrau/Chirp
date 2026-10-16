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
import tempfile
from typing import Any, Callable, Iterator, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
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


class _FirebaseRestAdapter:
    """Single-attempt Identity Toolkit Admin REST creator."""

    def __init__(self, session: Any, project_id: str = PROJECT_ID, timeout_seconds: float = 15.0) -> None:
        self._session = session
        self._project_id = project_id
        self._timeout = timeout_seconds

    def create_user(self, **kwargs: Any) -> Any:
        if kwargs.pop("project_id", None) != self._project_id:
            raise ProvisioningError("firebase_project_mismatch")
        payload = {"localId": kwargs.get("uid"), "email": kwargs.get("email"),
                   "password": kwargs.get("password"), "displayName": kwargs.get("display_name")}
        if set(payload) != {"localId", "email", "password", "displayName"}:
            raise ProvisioningError("firebase_create_request_invalid")
        try:
            response = self._session.request(
                "POST", f"https://identitytoolkit.googleapis.com/v1/projects/{self._project_id}/accounts",
                json=payload, timeout=self._timeout, allow_redirects=False)
        except Exception:
            raise QuarantineRequired("firebase_create_unknown") from None
        if getattr(response, "is_redirect", False) or getattr(response, "status_code", 0) in {301, 302, 303, 307, 308}:
            raise QuarantineRequired("firebase_create_redirect")
        if getattr(response, "status_code", 0) != 200:
            raise ProvisioningError("firebase_create_rejected")
        try:
            body = response.json()
            returned = body.get("localId")
        except Exception:
            raise QuarantineRequired("firebase_create_response_invalid") from None
        if not isinstance(returned, str):
            raise QuarantineRequired("firebase_create_response_invalid")
        return type("FirebaseRecord", (), {"uid": returned})()


def _google_authorized_session(*, credentials_path: str | None) -> Any:
    try:
        import google.auth
        from google.auth.transport.requests import Request as GoogleRequest
        from requests.adapters import HTTPAdapter
        import requests
    except ImportError:
        raise ProvisioningError("google_auth_unavailable") from None
    try:
        if credentials_path:
            from google.oauth2 import service_account
            credentials = service_account.Credentials.from_service_account_file(
                credentials_path, scopes=["https://www.googleapis.com/auth/cloud-platform"])
        else:
            credentials, detected_project = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"])
            if detected_project not in {None, PROJECT_ID}:
                raise ProvisioningError("firebase_project_mismatch")
        credentials.refresh(GoogleRequest())
        # Use a plain session instead of AuthorizedSession: the latter installs
        # auth refresh/retry behavior that can repeat a mutating request.
        session = requests.Session()
        session.trust_env = False
        session.headers.update({"Authorization": f"Bearer {credentials.token}"})
        session.mount("https://", HTTPAdapter(max_retries=0))
        return session
    except ProvisioningError:
        raise
    except Exception:
        raise ProvisioningError("firebase_credentials_unavailable") from None


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
    return _FirebaseRestAdapter(_google_authorized_session(credentials_path=credentials_path))


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
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent_stat = path.parent.stat()
    if not path.parent.is_dir() or parent_stat.st_uid != os.getuid() or parent_stat.st_mode & 0o777 != 0o700:
        raise ProvisioningError("private_parent_permissions")
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise ProvisioningError("private_path_invalid")
    if refuse_existing and path.exists():
        raise FileExistsError(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(_json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        if refuse_existing:
            os.link(temporary_path, path)
            temporary_path.unlink()
        else:
            os.replace(temporary_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            temporary_path.unlink()
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
                                 "chirps-prod", "C363_DATABASE_URL", "--manifest", "--credentials",
                                 "--identity-tool-api-key-file"]}


def load_plan(path: Path) -> Plan:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        fields = {key: raw[key] for key in ("schema_version", "run_id", "project_id", "campus_slug",
                                            "chapter_slug", "user_count", "source_digest", "image_digest",
                                            "config_digest", "created_at")}
        if fields["schema_version"] != 1 or raw.get("project_id") != PROJECT_ID:
            raise KeyError("plan")
        timestamp = datetime.fromisoformat(fields["created_at"])
        canonical = make_plan(run_id=fields["run_id"], user_count=fields["user_count"],
                              campus_slug=fields["campus_slug"], chapter_slug=fields["chapter_slug"],
                              source_digest=fields["source_digest"], image_digest=fields["image_digest"],
                              config_digest=fields["config_digest"], now=timestamp)
        if canonical.created_at != fields["created_at"]:
            raise ProvisioningError("plan_timestamp_invalid")
        expected = canonical.plan_digest
        if raw.get("plan_digest") != expected:
            raise ProvisioningError("plan_digest_invalid")
        if raw != plan_document(canonical):
            raise ProvisioningError("plan_document_invalid")
        return canonical
    except ProvisioningError:
        raise
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ProvisioningError("plan_unreadable") from None


def postgres_fixture_ownership(*, database_url: str, plan: Plan,
                               allow_test_database: bool = False,
                               expected_database: str | None = None,
                               expected_role: str | None = None,
                               expected_schema_head: str = "0049") -> OwnershipVerifier:
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
                            if (row is None or row.campus_id is not None or row.is_ghost
                                    or row.is_platform_admin or row.suspended_at is not None):
                                raise ProvisioningError("synthetic_user_binding_invalid")
                            if item.get("user_id") is not None and str(row.id) != str(item["user_id"]):
                                raise ProvisioningError("synthetic_user_binding_invalid")
                            if item.get("email") is not None and row.email != item["email"]:
                                raise ProvisioningError("synthetic_user_binding_invalid")
                            if (await session.execute(
                                select(Membership.id).where(Membership.user_id == row.id).limit(1)
                            )).first() is not None:
                                raise ProvisioningError("synthetic_user_membership_collision")
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

    async def _preflight_async(expected_users: list[UserPlan]) -> None:
        from sqlalchemy import select, text
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from app.models import Campus, User
        engine = create_async_engine(database_url, pool_pre_ping=True, pool_timeout=10)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
            async with factory() as session:
                identity = (await session.execute(text(
                    "SELECT current_database(), current_user, "
                    "(SELECT version_num FROM alembic_version)"))).one()
                if expected_database and identity[0] != expected_database:
                    raise ProvisioningError("ownership_database_mismatch")
                if expected_role and identity[1] != expected_role:
                    raise ProvisioningError("ownership_role_mismatch")
                if identity[2] != expected_schema_head:
                    raise ProvisioningError("ownership_schema_mismatch")
                row = (await session.execute(select(Campus.id).where(Campus.slug == plan.campus_slug))).first()
                if row is not None:
                    raise ProvisioningError("synthetic_campus_collision")
                for user in expected_users:
                    collision = (await session.execute(
                        select(User.id).where((User.firebase_uid == user.uid) | (User.email == user.email))
                    )).first()
                    if collision is not None:
                        raise ProvisioningError("synthetic_user_collision")
        finally:
            await engine.dispose()

    def preflight(expected_users: list[UserPlan]) -> None:
        import asyncio
        try:
            asyncio.run(_preflight_async(expected_users))
        except ProvisioningError:
            raise
        except Exception:
            raise ProvisioningError("ownership_preflight_failed") from None

    setattr(verify, "preflight", preflight)
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


def identity_toolkit_refresh_client(*, api_key: str, project_id: str = PROJECT_ID,
                                    timeout_seconds: float = 15.0) -> Callable[[str], Mapping[str, Any]]:
    if not api_key or not re.fullmatch(r"[A-Za-z0-9_-]{20,256}", api_key):
        raise ProvisioningError("identity_tool_api_key_invalid")
    if project_id != PROJECT_ID:
        raise ProvisioningError("firebase_project_mismatch")

    def refresh(refresh_token: str) -> Mapping[str, Any]:
        if not isinstance(refresh_token, str) or not refresh_token:
            raise ProvisioningError("refresh_token_missing")
        body = urlencode({"grant_type": "refresh_token", "refresh_token": refresh_token}).encode()
        request = Request("https://securetoken.googleapis.com/v1/token?" + urlencode({"key": api_key}),
                          data=body, method="POST",
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with _no_redirect_opener().open(request, timeout=timeout_seconds) as response:
                return _read_json(response)
        except HTTPError:
            raise ProvisioningError("identity_tool_refresh_failed") from None
        except (URLError, TimeoutError):
            raise ProvisioningError("identity_tool_refresh_unavailable") from None
    return refresh


def refresh_credentials(*, credentials_path: Path, plan: Plan,
                         refresh_client: Callable[[str], Mapping[str, Any]],
                         token_verifier: Callable[[str], Mapping[str, Any]],
                         manifest_path: Path | None = None,
                         now: datetime | None = None) -> dict[str, Any]:
    """Refresh every confirmed manifest token without changing Firebase identities."""
    try:
        raw = json.loads(credentials_path.read_text(encoding="utf-8"))
        if raw.get("project_id") != PROJECT_ID or raw.get("plan_digest") != plan.plan_digest:
            raise ProvisioningError("credentials_plan_mismatch")
        entries = raw["credentials"]
        if not isinstance(entries, list) or not entries:
            raise ProvisioningError("credentials_invalid")
    except ProvisioningError:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise ProvisioningError("credentials_unreadable") from None
    if manifest_path is None:
        raise ProvisioningError("manifest_required")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (manifest.get("kind") != "c363_synthetic_firebase_manifest"
                or manifest.get("status") != "COMPLETE"
                or manifest.get("fixture_provenance") != "synthetic_only"
                or manifest.get("plan_digest") != plan.plan_digest
                or not isinstance(manifest.get("campus_id"), str)
                or not isinstance(manifest.get("chapter_id"), str)):
            raise ProvisioningError("manifest_plan_mismatch")
        users = manifest.get("users")
        if not isinstance(users, list) or len(users) != len(entries):
            raise ProvisioningError("manifest_users_mismatch")
        manifest_users = {entry.get("uid"): entry for entry in users}
        if set(manifest_users) != {entry.get("uid") for entry in entries}:
            raise ProvisioningError("manifest_users_mismatch")
        for entry in entries:
            user = manifest_users.get(entry.get("uid"))
            if (not isinstance(user, dict) or user.get("email") != entry.get("email")
                    or user.get("user_id") != entry.get("user_id")
                    or entry.get("status") != "bootstrapped"):
                raise ProvisioningError("manifest_users_mismatch")
    except ProvisioningError:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise ProvisioningError("manifest_unreadable") from None
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    updated: list[dict[str, Any]] = []
    for entry in entries:
        uid = entry.get("uid")
        refresh_token = entry.get("refresh_token")
        if not isinstance(uid, str) or not UID_RE.fullmatch(uid) or not isinstance(refresh_token, str):
            raise ProvisioningError("credentials_entry_invalid")
        result = refresh_client(refresh_token)
        token = result.get("id_token")
        returned_uid = result.get("user_id")
        expires_in = result.get("expires_in")
        if not isinstance(token, str) or returned_uid != uid or type(expires_in) is not int:
            raise QuarantineRequired("refresh_identity_mismatch")
        verified = token_verifier(token)
        exp = verified.get("exp")
        if verified.get("uid") != uid or verified.get("aud") != PROJECT_ID or type(exp) is not int:
            raise QuarantineRequired("refresh_token_verification_failed")
        if exp < int(current.timestamp()) + TOKEN_MIN_REMAINING_SECONDS:
            raise ProvisioningError("token_expiry_window_too_short")
        replacement = dict(entry)
        replacement.update({"id_token": token,
                            "expires_at": datetime.fromtimestamp(exp, timezone.utc).isoformat()})
        if isinstance(result.get("refresh_token"), str) and result["refresh_token"]:
            replacement["refresh_token"] = result["refresh_token"]
        updated.append(replacement)
        _atomic_private_json(credentials_path, {"schema_version": 1, "project_id": PROJECT_ID,
                                                 "plan_digest": plan.plan_digest,
                                                 "credentials": updated + entries[len(updated):]},
                             refuse_existing=False)
    document = {"schema_version": 1, "project_id": PROJECT_ID,
                "plan_digest": plan.plan_digest, "credentials": updated}
    _atomic_private_json(credentials_path, document, refuse_existing=False)
    try:
        by_uid = {entry["uid"]: entry for entry in updated}
        for user in users:
            replacement = by_uid[user["uid"]]
            user["id_token"] = replacement["id_token"]
            user["expires_at"] = replacement["expires_at"]
        manifest["refreshed_at"] = current.isoformat()
        _atomic_private_json(manifest_path, manifest, refuse_existing=False)
    except ProvisioningError:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise ProvisioningError("manifest_unreadable") from None
    return {"status": "REFRESHED", "plan_digest": plan.plan_digest,
            "campus_id": manifest["campus_id"], "chapter_id": manifest["chapter_id"],
            "users": len(updated)}


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
                    credentials_path: Path | None = None,
                    now: datetime | None = None) -> dict[str, Any]:
    if plan.project_id != PROJECT_ID:
        raise ProvisioningError("firebase_project_mismatch")
    if manifest_path is None:
        raise ProvisioningError("manifest_required")
    users = users_for_plan(plan)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not callable(verify_ownership):
        raise ProvisioningError("ownership_verifier_required")
    preflight = getattr(verify_ownership, "preflight", None)
    if preflight is not None:
        if not callable(preflight):
            raise ProvisioningError("ownership_preflight_invalid")
        preflight(users)
    created: list[dict[str, Any]] = []
    credential_state: list[dict[str, Any]] = []
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
            credential_state.append({"uid": user.uid, "email": user.email, "password": password,
                                     "status": "create_pending"})
            if credentials_path is not None:
                _atomic_private_json(credentials_path, {"schema_version": 1, "project_id": PROJECT_ID,
                                                         "plan_digest": plan.plan_digest,
                                                         "credentials": credential_state})
            try:
                record = firebase.create_user(uid=user.uid, email=user.email, password=password,
                                              display_name=user.display_name, project_id=PROJECT_ID)
            except BaseException:
                # A network/SDK exception may follow a successful provider write. Never lookup
                # and adopt or retry: quarantine the run for operator reconciliation.
                state["status"] = "firebase_create_unknown"
                if manifest_path is not None:
                    _atomic_private_json(manifest_path, {**manifest_base, "status": "QUARANTINED",
                                                         "users": row_state}, refuse_existing=False)
                raise QuarantineRequired("firebase_create_unknown") from None
            if getattr(record, "uid", None) != user.uid:
                raise QuarantineRequired("firebase_create_identity_mismatch")
            state["status"] = "firebase_created"
            credential_state[-1]["status"] = "firebase_created"
            if manifest_path is not None:
                _atomic_private_json(manifest_path, {**manifest_base, "status": "RUNNING", "users": row_state}, refuse_existing=False)
            result = token_client(email=user.email, password=password)
            token, expires_at = _verify_token(result, expected_uid=user.uid, now=current, verifier=token_verifier)
            refresh_token = result.get("refreshToken")
            if isinstance(refresh_token, str) and refresh_token:
                credential_state[-1]["refresh_token"] = refresh_token
            credential_state[-1].update({"status": "token_issued", "id_token": token,
                                         "expires_at": expires_at})
            if credentials_path is not None:
                _atomic_private_json(credentials_path, {"schema_version": 1, "project_id": PROJECT_ID,
                                                         "plan_digest": plan.plan_digest,
                                                         "credentials": credential_state}, refuse_existing=False)
            state.update({"status": "token_issued", "expires_at": expires_at})
            if manifest_path is not None:
                _atomic_private_json(manifest_path, {**manifest_base, "status": "RUNNING", "users": row_state}, refuse_existing=False)
            try:
                response = bootstrap(token=token, email=user.email, display_name=user.display_name)
            except BaseException:
                state["status"] = "bootstrap_unknown"
                if manifest_path is not None:
                    _atomic_private_json(manifest_path, {**manifest_base, "status": "QUARANTINED",
                                                         "users": row_state, "credentials": created},
                                         refuse_existing=False)
                raise QuarantineRequired("bootstrap_unknown") from None
            if response.get("firebase_uid") != user.uid or not response.get("id"):
                raise QuarantineRequired("bootstrap_identity_mismatch")
            created.append({"uid": user.uid, "email": user.email, "id_token": token, "expires_at": expires_at,
                           "user_id": str(response["id"])})
            state.update({"status": "bootstrapped", "user_id": str(response["id"])})
            credential_state[-1].update({"status": "bootstrapped", "user_id": str(response["id"])})
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
    except BaseException:
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
    parser.add_argument("--database-url", help=argparse.SUPPRESS)
    parser.add_argument("--credentials")
    parser.add_argument("--firebase-credentials")
    parser.add_argument("--identity-tool-api-key-file")
    parser.add_argument("--bootstrap-url", default=BOOTSTRAP_ORIGIN)
    parser.add_argument("--allow-test-database", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--confirm-plan-digest")
    args = parser.parse_args(argv)
    try:
        plan_path = Path(args.plan)
        with _run_lock(plan_path):
            if not args.execute and not args.refresh:
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
            if args.refresh:
                if not args.credentials or not args.identity_tool_api_key_file:
                    raise ProvisioningError("refresh_inputs_required")
                key_path = Path(args.identity_tool_api_key_file)
                if key_path.stat().st_mode & 0o077:
                    raise ProvisioningError("identity_tool_api_key_permissions")
                api_key = key_path.read_text(encoding="utf-8").strip()
                if not args.manifest:
                    raise ProvisioningError("refresh_inputs_required")
                refreshed = refresh_credentials(credentials_path=Path(args.credentials), plan=plan,
                                                refresh_client=identity_toolkit_refresh_client(api_key=api_key),
                                                token_verifier=firebase_token_verifier(),
                                                manifest_path=Path(args.manifest))
                print(json.dumps(refreshed))
                return 0
            database_url = os.environ.get("C363_DATABASE_URL")
            expected_database = os.environ.get("C363_EXPECTED_DATABASE")
            expected_role = os.environ.get("C363_EXPECTED_DB_ROLE")
            if (args.database_url or not args.manifest or not args.credentials or not database_url
                    or not expected_database or not expected_role or not args.identity_tool_api_key_file):
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
            ownership = postgres_fixture_ownership(database_url=database_url, plan=plan,
                                                   allow_test_database=args.allow_test_database,
                                                   expected_database=expected_database,
                                                   expected_role=expected_role)
            result = provision_users(plan=plan, firebase=firebase, token_client=token_client,
                                     token_verifier=verifier, bootstrap=bootstrap,
                                     verify_ownership=ownership, manifest_path=Path(args.manifest),
                                     credentials_path=Path(args.credentials))
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
