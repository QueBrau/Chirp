"""Preview and transactionally apply scoped c363 synthetic feed cleanup."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
from typing import Mapping
import uuid

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

SCHEMA_VERSION = 1
MAX_USERS = 500
MAX_RUN_WINDOW = timedelta(hours=24)
MAX_CANDIDATES_PER_TABLE = 20_000
MAX_PLAN_BYTES = 8 * 1024 * 1024
TIMEOUT_SECONDS = 30
TABLES = ("posts", "post_comments", "chirps")
_UID_RE = re.compile(r"^[A-Za-z0-9._:@+-]{1,128}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class CleanupError(ValueError):
    """Sanitized error; its code never contains user or connection data."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _uuid(value: object) -> uuid.UUID:
    if not isinstance(value, str):
        raise CleanupError("invalid_scope")
    try:
        result = uuid.UUID(value)
    except (TypeError, ValueError, AttributeError):
        raise CleanupError("invalid_scope") from None
    if str(result) != value.lower():
        raise CleanupError("invalid_scope")
    return result


def _utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise CleanupError("invalid_scope")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise CleanupError("invalid_scope") from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise CleanupError("invalid_scope")
    return result.astimezone(timezone.utc)


def _time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _validated_scope(scope: CleanupScope) -> CleanupScope:
    try:
        if not isinstance(scope, CleanupScope):
            raise CleanupError("invalid_scope")
        return CleanupScope.from_dict(scope.canonical())
    except CleanupError:
        raise
    except Exception:
        raise CleanupError("invalid_scope") from None


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


@dataclass(frozen=True)
class ScopedUser:
    firebase_uid: str
    user_id: uuid.UUID


@dataclass(frozen=True)
class CleanupScope:
    campus_id: uuid.UUID
    chapter_id: uuid.UUID
    expected_database_name: str
    expected_database_role: str
    run_started_at: datetime
    run_ended_at: datetime
    users: tuple[ScopedUser, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "CleanupScope":
        keys = {"schema_version", "campus_id", "chapter_id", "expected_database_name",
                "expected_database_role", "run_started_at", "run_ended_at", "users"}
        if not isinstance(raw, Mapping) or set(raw) != keys or type(raw["schema_version"]) is not int or raw["schema_version"] != SCHEMA_VERSION:
            raise CleanupError("invalid_scope")
        db_name, db_role = raw["expected_database_name"], raw["expected_database_role"]
        if (not isinstance(db_name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,63}", db_name)
                or not isinstance(db_role, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,63}", db_role)):
            raise CleanupError("invalid_scope")
        start, end = _utc(raw["run_started_at"]), _utc(raw["run_ended_at"])
        if end <= start or end - start > MAX_RUN_WINDOW or end > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise CleanupError("invalid_scope")
        items = raw["users"]
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_USERS:
            raise CleanupError("invalid_scope")
        users = []
        for item in items:
            if not isinstance(item, Mapping) or set(item) != {"firebase_uid", "user_id"}:
                raise CleanupError("invalid_scope")
            uid = item["firebase_uid"]
            if not isinstance(uid, str) or not _UID_RE.fullmatch(uid):
                raise CleanupError("invalid_scope")
            users.append(ScopedUser(uid, _uuid(item["user_id"])))
        if len({u.firebase_uid for u in users}) != len(users) or len({u.user_id for u in users}) != len(users):
            raise CleanupError("invalid_scope")
        return cls(_uuid(raw["campus_id"]), _uuid(raw["chapter_id"]), db_name, db_role,
                   start, end, tuple(sorted(users, key=lambda u: (u.user_id.hex, u.firebase_uid))))

    def canonical(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION, "campus_id": str(self.campus_id),
            "chapter_id": str(self.chapter_id), "expected_database_name": self.expected_database_name,
            "expected_database_role": self.expected_database_role, "run_started_at": _time(self.run_started_at),
            "run_ended_at": _time(self.run_ended_at),
            "users": [{"firebase_uid": u.firebase_uid, "user_id": str(u.user_id)} for u in self.users],
        }

    @property
    def digest(self) -> str:
        return _digest(self.canonical())


@dataclass(frozen=True)
class CleanupPlan:
    payload: dict[str, object]
    digest: str


@dataclass(frozen=True)
class CleanupResult:
    deleted_posts: int
    deleted_comments: int
    deleted_chirps: int
    digest: str


def _engine(url: str):
    if not isinstance(url, str) or not url:
        raise CleanupError("database_url_required")
    try:
        parsed = make_url(url)
    except Exception:
        raise CleanupError("database_url_invalid") from None
    if parsed.drivername != "postgresql+asyncpg" or not parsed.host or not parsed.database:
        raise CleanupError("database_url_invalid")
    return create_async_engine(parsed, pool_size=1, max_overflow=0, pool_timeout=4,
                               pool_pre_ping=True, connect_args={"timeout": 5, "command_timeout": 9})


async def _identity(conn, scope: CleanupScope) -> dict[str, str]:
    row = (await conn.execute(text(
        "SELECT current_database() name, current_user role, inet_server_addr()::text address, inet_server_port() port"
    ))).mappings().one()
    if row["name"] != scope.expected_database_name or row["role"] != scope.expected_database_role:
        raise CleanupError("database_identity_mismatch")
    endpoint = f"{row['address'] or 'local-socket'}:{row['port'] or 0}"
    return {"name": row["name"], "role": row["role"],
            "endpoint_digest": hashlib.sha256(endpoint.encode()).hexdigest()}


async def _verify_scope(conn, scope: CleanupScope, lock: bool) -> None:
    suffix = " FOR UPDATE" if lock else ""
    rows = (await conn.execute(text(
        "SELECT u.id::text user_id,u.firebase_uid,u.campus_id::text campus_id,m.status,"
        "c.campus_id::text chapter_campus_id FROM users u "
        "JOIN memberships m ON m.user_id=u.id JOIN chapters c ON c.id=m.chapter_id "
        "WHERE u.id=ANY(:users) AND m.chapter_id=:chapter ORDER BY u.id" + suffix
    ), {"users": [u.user_id for u in scope.users], "chapter": scope.chapter_id})).mappings().all()
    if len(rows) != len(scope.users):
        raise CleanupError("scope_membership_mismatch")
    by_id = {r["user_id"]: r for r in rows}
    for user in scope.users:
        row = by_id.get(str(user.user_id))
        if (row is None or row["firebase_uid"] != user.firebase_uid or
                row["campus_id"] != str(scope.campus_id) or
                row["chapter_campus_id"] != str(scope.campus_id) or row["status"] != "active"):
            raise CleanupError("scope_membership_mismatch")
    chapter_sql = "SELECT campus_id::text FROM chapters WHERE id=:id" + (" FOR UPDATE" if lock else "")
    if (await conn.execute(text(chapter_sql), {"id": scope.chapter_id})).scalar_one_or_none() != str(scope.campus_id):
        raise CleanupError("scope_membership_mismatch")


def _query(table: str) -> str:
    if table == "posts":
        return """SELECT p.id::text id,p.author_id::text author_id,p.campus_id::text campus_id,
        p.chapter_id::text chapter_id,p.created_at,p.body,p.deleted_at,p.removed_reason,
        p.post_type,p.media_urls,p.duration_sec,u.firebase_uid
        FROM posts p JOIN users u ON u.id=p.author_id
        WHERE p.author_id=ANY(:users) AND p.campus_id=:campus AND p.chapter_id=:chapter
        AND p.created_at>=:start AND p.created_at<:end ORDER BY p.id"""
    if table == "chirps":
        return """SELECT p.id::text id,p.author_id::text author_id,p.campus_id::text campus_id,
        NULL::text chapter_id,p.created_at,p.body,p.removed_at deleted_at,p.removed_reason,u.firebase_uid
        FROM chirps p JOIN users u ON u.id=p.author_id
        WHERE p.author_id=ANY(:users) AND p.campus_id=:campus
        AND p.created_at>=:start AND p.created_at<:end ORDER BY p.id"""
    return """SELECT p.id::text id,p.author_id::text author_id,po.campus_id::text campus_id,
    po.chapter_id::text chapter_id,p.created_at,p.body,p.deleted_at,p.removed_reason,u.firebase_uid,
    p.post_id::text post_id,po.author_id::text parent_author_id,
    po.campus_id::text parent_campus_id,po.chapter_id::text parent_chapter_id,
    po.deleted_at parent_deleted_at,po.removed_reason parent_removed_reason
    FROM post_comments p JOIN users u ON u.id=p.author_id
    JOIN posts po ON po.id=p.post_id WHERE p.author_id=ANY(:users) AND po.campus_id=:campus
    AND po.chapter_id=:chapter AND p.created_at>=:start AND p.created_at<:end ORDER BY p.id"""


def _pattern(uid: str, kind: str) -> re.Pattern[str]:
    return re.compile(rf"LOADTEST {kind} from {re.escape(uid)} seq [0-9]{{1,6}} - synthetic content, safe to delete")


async def _collect(conn, scope: CleanupScope, lock: bool) -> tuple[dict[str, list[dict[str, str]]], dict[str, str]]:
    await _verify_scope(conn, scope, lock)
    params = {"users": [u.user_id for u in scope.users], "campus": scope.campus_id,
              "chapter": scope.chapter_id, "start": scope.run_started_at, "end": scope.run_ended_at}
    result: dict[str, list[dict[str, str]]] = {table: [] for table in TABLES}
    exact_bodies: dict[str, str] = {}
    parent_post_ids: set[uuid.UUID] = set()
    parent_snapshots: dict[str, dict[str, object]] = {}
    uid_by_id = {str(u.user_id): u.firebase_uid for u in scope.users}
    for table, kind in (("posts", "post"), ("chirps", "chirp"), ("post_comments", "comment")):
        sql = _query(table) + " LIMIT :row_limit" + (" FOR UPDATE OF p" if lock else "")
        rows = (await conn.execute(text(sql),
                                   {**params, "row_limit": MAX_CANDIDATES_PER_TABLE + 1})).mappings().all()
        if len(rows) > MAX_CANDIDATES_PER_TABLE:
            raise CleanupError("candidate_limit_exceeded")
        for row in rows:
            uid = uid_by_id.get(row["author_id"])
            if uid is None or row["firebase_uid"] != uid or not _pattern(uid, kind).fullmatch(row["body"]):
                continue
            if row["deleted_at"] is not None or row["removed_reason"] is not None:
                raise CleanupError("unsafe_dependencies")
            if table == "posts" and (
                row.get("post_type") != "text" or row.get("media_urls") not in (None, [])
                or row.get("duration_sec") is not None
            ):
                raise CleanupError("unsafe_dependencies")
            if table == "post_comments":
                if (row["parent_author_id"] not in {str(user.user_id) for user in scope.users}
                        or row["parent_campus_id"] != str(scope.campus_id)
                        or row["parent_chapter_id"] != str(scope.chapter_id)
                        or row["parent_deleted_at"] is not None
                        or row["parent_removed_reason"] is not None):
                    raise CleanupError("unsafe_dependencies")
            if table == "posts" and row["chapter_id"] != str(scope.chapter_id):
                raise CleanupError("scope_membership_mismatch")
            fields = {"id": row["id"], "author_id": row["author_id"], "campus_id": row["campus_id"],
                      "chapter_id": row["chapter_id"], "post_id": row.get("post_id"),
                      "created_at": _time(row["created_at"]),
                      "body_sha256": hashlib.sha256(row["body"].encode()).hexdigest(),
                      "post_type": row.get("post_type"),
                      "media_urls": row.get("media_urls"),
                      "duration_sec": row.get("duration_sec"),
                      "parent_author_id": row.get("parent_author_id"),
                      "parent_campus_id": row.get("parent_campus_id"),
                      "parent_chapter_id": row.get("parent_chapter_id"),
                      "parent_deleted_at": _time(row["parent_deleted_at"]) if row.get("parent_deleted_at") else None,
                      "parent_removed_reason": row.get("parent_removed_reason")}
            result[table].append({"id": row["id"], "fingerprint": _digest(fields)})
            exact_bodies[f"{table}:{row['id']}"] = row["body"]
            if table == "post_comments":
                parent_post_ids.add(uuid.UUID(row["post_id"]))
                parent_snapshots[row["id"]] = {
                    "id": row["post_id"], "author_id": row["parent_author_id"],
                    "campus_id": row["parent_campus_id"], "chapter_id": row["parent_chapter_id"],
                    "deleted_at": row["parent_deleted_at"],
                    "removed_reason": row["parent_removed_reason"],
                }

    if lock and parent_post_ids:
        # Parent rows may be historical QA posts outside the run window. Lock
        # them in one bounded query and compare their fresh state to the snapshot.
        parent_rows = (await conn.execute(text(
            "SELECT id::text id,author_id::text author_id,campus_id::text campus_id,"
            "chapter_id::text chapter_id,deleted_at,removed_reason FROM posts "
            "WHERE id=ANY(:ids) ORDER BY id FOR UPDATE"
        ), {"ids": list(parent_post_ids)})).mappings().all()
        parents = {row["id"]: row for row in parent_rows}
        for snapshot in parent_snapshots.values():
            parent = parents.get(snapshot["id"])
            if (parent is None or parent["author_id"] != snapshot["author_id"]
                    or parent["campus_id"] != snapshot["campus_id"]
                    or parent["chapter_id"] != snapshot["chapter_id"]
                    or parent["deleted_at"] != snapshot["deleted_at"]
                    or parent["removed_reason"] != snapshot["removed_reason"]):
                raise CleanupError("plan_mismatch")

    post_ids = [uuid.UUID(r["id"]) for r in result["posts"]]
    chirp_ids = [uuid.UUID(r["id"]) for r in result["chirps"]]
    comment_ids = [uuid.UUID(r["id"]) for r in result["post_comments"]]
    if not (post_ids or chirp_ids or comment_ids):
        return result, exact_bodies
    # target_id is not an FK, so SHARE fences report/audit inserts until commit.
    if post_ids:
        unsafe = (await conn.execute(text("""SELECT
          EXISTS(SELECT 1 FROM post_likes WHERE post_id=ANY(:posts)) OR
          EXISTS(SELECT 1 FROM post_comments WHERE post_id=ANY(:posts)
                 AND NOT(id=ANY(:comments)))"""),
          {"posts": post_ids, "comments": comment_ids})).scalar_one()
        if unsafe:
            raise CleanupError("unsafe_dependencies")
    if chirp_ids and (await conn.execute(text(
        "SELECT EXISTS(SELECT 1 FROM chirp_votes WHERE chirp_id=ANY(:ids))"), {"ids": chirp_ids})).scalar_one():
        raise CleanupError("unsafe_dependencies")
    report_post_ids = list(set(post_ids) | parent_post_ids)
    for target_type, ids in (("post", report_post_ids), ("comment", comment_ids),
                             ("chirp", chirp_ids)):
        if not ids:
            continue
        for table in ("content_reports", "moderation_actions"):
            if (await conn.execute(text(
                f"SELECT EXISTS(SELECT 1 FROM {table} WHERE target_type=:kind AND target_id=ANY(:ids))"
            ), {"kind": target_type, "ids": ids})).scalar_one():
                raise CleanupError("unsafe_dependencies")
    return result, exact_bodies


def _target_identity(database_url: str) -> dict[str, object]:
    parsed = make_url(database_url)
    return {
        "driver": parsed.drivername, "host": parsed.host, "port": parsed.port,
        "database": parsed.database, "username": parsed.username,
    }


async def _make_plan(conn, scope: CleanupScope, lock: bool, target: dict[str, object]) -> tuple[dict[str, object], dict[str, str]]:
    identity = await _identity(conn, scope)
    candidates, exact_bodies = await _collect(conn, scope, lock)
    plan: dict[str, object] = {
        "schema_version": SCHEMA_VERSION, "database": {**identity, "target": target},
        "scope_digest": scope.digest,
        "run_started_at": _time(scope.run_started_at), "run_ended_at": _time(scope.run_ended_at),
        "candidates": candidates, "counts": {table: len(candidates[table]) for table in TABLES},
    }
    plan["plan_digest"] = _digest(plan)
    if sum(len(candidates[table]) for table in TABLES) > MAX_CANDIDATES_PER_TABLE:
        raise CleanupError("candidate_limit_exceeded")
    if len(_canonical(plan)) > MAX_PLAN_BYTES:
        raise CleanupError("plan_too_large")
    return plan, exact_bodies


async def preview_cleanup(scope: CleanupScope, database_url: str) -> CleanupPlan:
    scope = _validated_scope(scope)
    engine = _engine(database_url)
    try:
        async with asyncio.timeout(TIMEOUT_SECONDS):
            async with engine.connect() as conn:
                conn = await conn.execution_options(isolation_level="REPEATABLE READ")
                async with conn.begin():
                    await conn.execute(text("SET TRANSACTION READ ONLY"))
                    await conn.execute(text("SET LOCAL statement_timeout='8s'"))
                    await conn.execute(text("SET LOCAL lock_timeout='3s'"))
                    payload, _ = await _make_plan(conn, scope, False, _target_identity(database_url))
        return CleanupPlan(payload, str(payload["plan_digest"]))
    except CleanupError:
        raise
    except TimeoutError:
        raise CleanupError("cleanup_deadline_exceeded") from None
    except Exception:
        raise CleanupError("cleanup_database_error") from None
    finally:
        await engine.dispose()


def write_plan(plan: CleanupPlan, path: Path) -> None:
    temp_path: Path | None = None
    try:
        path = Path(path)
        data = _canonical(plan.payload) + b"\n"
        if len(data) > MAX_PLAN_BYTES:
            raise CleanupError("plan_too_large")
        fd, temp_name = tempfile.mkstemp(prefix=".cleanup-plan-", dir=path.parent)
        temp_path = Path(temp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=False) as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(fd)
        # Hard-link is atomic and refuses to replace an existing plan.
        os.link(temp_path, path, follow_symlinks=False)
        temp_path.unlink()
        temp_path = None
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except FileExistsError:
        raise CleanupError("plan_output_exists") from None
    except CleanupError:
        raise
    except Exception:
        raise CleanupError("plan_write_failed") from None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


def _read_plan(path: Path) -> dict[str, object]:
    try:
        path = Path(path)
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PLAN_BYTES or info.st_mode & 0o077:
            os.close(fd)
            raise CleanupError("invalid_plan")
        with os.fdopen(fd, "rb") as handle:
            raw = json.loads(handle.read(MAX_PLAN_BYTES + 1))
        required = {"schema_version", "database", "scope_digest", "run_started_at",
                    "run_ended_at", "candidates", "counts", "plan_digest"}
        if (not isinstance(raw, dict) or set(raw) != required
                or type(raw["schema_version"]) is not int or raw["schema_version"] != SCHEMA_VERSION):
            raise CleanupError("invalid_plan")
        if not isinstance(raw["database"], dict) or set(raw["database"]) != {
            "name", "role", "endpoint_digest", "target"
        }:
            raise CleanupError("invalid_plan")
        if (not isinstance(raw["database"]["name"], str)
                or not isinstance(raw["database"]["role"], str)
                or not isinstance(raw["database"]["endpoint_digest"], str)
                or not _DIGEST_RE.fullmatch(raw["database"]["endpoint_digest"])
                or not isinstance(raw["database"]["target"], dict)
                or set(raw["database"]["target"]) != {"driver", "host", "port", "database", "username"}):
            raise CleanupError("invalid_plan")
        if not isinstance(raw["scope_digest"], str) or not _DIGEST_RE.fullmatch(raw["scope_digest"]):
            raise CleanupError("invalid_plan")
        candidates, counts = raw["candidates"], raw["counts"]
        if not isinstance(candidates, dict) or set(candidates) != set(TABLES):
            raise CleanupError("invalid_plan")
        if not isinstance(counts, dict) or set(counts) != set(TABLES):
            raise CleanupError("invalid_plan")
        for table in TABLES:
            rows = candidates[table]
            if (not isinstance(rows, list) or len(rows) > MAX_CANDIDATES_PER_TABLE
                    or type(counts[table]) is not int or counts[table] != len(rows)):
                raise CleanupError("invalid_plan")
            seen = set()
            for item in rows:
                if not isinstance(item, dict) or set(item) != {"id", "fingerprint"}:
                    raise CleanupError("invalid_plan")
                try:
                    row_id = _uuid(item["id"])
                except CleanupError:
                    raise CleanupError("invalid_plan") from None
                fingerprint = item["fingerprint"]
                if (row_id in seen or not isinstance(fingerprint, str)
                        or not _DIGEST_RE.fullmatch(fingerprint)):
                    raise CleanupError("invalid_plan")
                seen.add(row_id)
        digest = raw.pop("plan_digest")
        if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest) or _digest(raw) != digest:
            raise CleanupError("invalid_plan")
        raw["plan_digest"] = digest
        return raw
    except CleanupError:
        raise
    except Exception:
        raise CleanupError("invalid_plan") from None


async def apply_cleanup(scope: CleanupScope, database_url: str, plan_path: Path,
                        confirm_digest: str) -> CleanupResult:
    scope = _validated_scope(scope)
    if not isinstance(confirm_digest, str) or not _DIGEST_RE.fullmatch(confirm_digest):
        raise CleanupError("confirm_digest_mismatch")
    expected = _read_plan(plan_path)
    if not secrets.compare_digest(str(expected["plan_digest"]), confirm_digest):
        raise CleanupError("confirm_digest_mismatch")
    engine = _engine(database_url)
    try:
        async with asyncio.timeout(TIMEOUT_SECONDS):
            async with engine.connect() as conn:
                conn = await conn.execution_options(isolation_level="READ COMMITTED")
                async with conn.begin():
                    await conn.execute(text("SET LOCAL statement_timeout='8s'"))
                    await conn.execute(text("SET LOCAL lock_timeout='3s'"))
                    current_identity = await _identity(conn, scope)
                    if ({**current_identity, "target": _target_identity(database_url)}
                            != expected["database"]):
                        raise CleanupError("plan_mismatch")
                    # FK-less report/audit rows need a table-level fence through commit.
                    await conn.execute(text("LOCK TABLE content_reports IN SHARE MODE"))
                    await conn.execute(text("LOCK TABLE moderation_actions IN SHARE MODE"))
                    actual, exact_bodies = await _make_plan(conn, scope, True, _target_identity(database_url))
                    if actual != expected or actual["plan_digest"] != confirm_digest:
                        raise CleanupError("plan_mismatch")
                    candidates = actual["candidates"]
                    assert isinstance(candidates, dict)
                    ids_by_table = {table: [uuid.UUID(row["id"]) for row in candidates[table]]
                                    for table in TABLES}
                    params = {"ids": [], "bodies": [], "users": [u.user_id for u in scope.users],
                              "campus": scope.campus_id, "chapter": scope.chapter_id,
                              "start": scope.run_started_at, "end": scope.run_ended_at}
                    deleted: dict[str, int] = {}
                    for table in ("post_comments", "chirps", "posts"):
                        ids = ids_by_table[table]
                        if not ids:
                            deleted[table] = 0
                            continue
                        params["ids"] = ids
                        params["bodies"] = [exact_bodies[f"{table}:{value}"] for value in ids]
                        if table == "posts":
                            sql = """WITH selected AS (
                                SELECT * FROM unnest(CAST(:ids AS uuid[]),CAST(:bodies AS text[])) AS x(id,body)
                              ) DELETE FROM posts p USING users u,selected s WHERE p.id=s.id AND p.body=s.body
                              AND p.author_id=u.id AND p.author_id=ANY(:users) AND p.campus_id=:campus
                              AND p.chapter_id=:chapter AND p.created_at>=:start AND p.created_at<:end
                              AND p.deleted_at IS NULL AND p.removed_reason IS NULL AND p.post_type='text'
                              AND (p.media_urls IS NULL OR cardinality(p.media_urls)=0) RETURNING p.id"""
                        elif table == "chirps":
                            sql = """WITH selected AS (
                                SELECT * FROM unnest(CAST(:ids AS uuid[]),CAST(:bodies AS text[])) AS x(id,body)
                              ) DELETE FROM chirps p USING users u,selected s WHERE p.id=s.id AND p.body=s.body
                              AND p.author_id=u.id AND p.author_id=ANY(:users) AND p.campus_id=:campus
                              AND p.created_at>=:start AND p.created_at<:end
                              AND p.removed_at IS NULL AND p.removed_reason IS NULL RETURNING p.id"""
                        else:
                            sql = """WITH selected AS (
                                SELECT * FROM unnest(CAST(:ids AS uuid[]),CAST(:bodies AS text[])) AS x(id,body)
                              ) DELETE FROM post_comments p USING users u,posts po,selected s
                              WHERE p.id=s.id AND p.body=s.body
                              AND p.author_id=u.id AND po.id=p.post_id AND p.author_id=ANY(:users)
                          AND po.author_id=ANY(:users)
                              AND po.campus_id=:campus AND po.chapter_id=:chapter
                              AND p.created_at>=:start AND p.created_at<:end
                              AND p.deleted_at IS NULL AND p.removed_reason IS NULL RETURNING p.id"""
                        returned = {str(row[0]) for row in (await conn.execute(text(sql), params)).fetchall()}
                        if returned != {str(value) for value in ids}:
                            raise CleanupError("delete_rowcount_mismatch")
                        deleted[table] = len(returned)
        return CleanupResult(deleted["posts"], deleted["post_comments"], deleted["chirps"], confirm_digest)
    except CleanupError:
        raise
    except TimeoutError:
        raise CleanupError("cleanup_deadline_exceeded") from None
    except Exception:
        raise CleanupError("cleanup_outcome_unknown") from None
    finally:
        await engine.dispose()


def _load_json(path: Path, limit: int) -> object:
    try:
        fd = os.open(Path(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit or info.st_mode & 0o077:
            os.close(fd)
            raise CleanupError("invalid_scope")
        with os.fdopen(fd, "rb") as handle:
            return json.loads(handle.read(limit + 1))
    except CleanupError:
        raise
    except Exception:
        raise CleanupError("invalid_scope") from None


async def _cli(args: argparse.Namespace) -> int:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise CleanupError("database_url_required")
    raw = _load_json(args.scope, 256 * 1024)
    if not isinstance(raw, dict):
        raise CleanupError("invalid_scope")
    scope = CleanupScope.from_dict(raw)
    if args.apply:
        if not args.plan or not args.confirm_digest:
            raise CleanupError("apply_requires_plan_and_digest")
        result = await apply_cleanup(scope, database_url, args.plan, args.confirm_digest)
        print(json.dumps({"status": "applied", "deleted_posts": result.deleted_posts,
                          "deleted_comments": result.deleted_comments,
                          "deleted_chirps": result.deleted_chirps, "digest": result.digest}, sort_keys=True))
        return 0
    plan = await preview_cleanup(scope, database_url)
    if args.plan_out:
        write_plan(plan, args.plan_out)
    print(json.dumps({"status": "preview", "counts": plan.payload["counts"],
                      "digest": plan.digest, "plan_written": bool(args.plan_out)}, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview or apply scoped c363 load-test cleanup")
    parser.add_argument("--scope", type=Path, required=True, help="private token-free scope JSON")
    parser.add_argument("--plan-out", type=Path, help="exclusive private preview plan output")
    parser.add_argument("--plan", type=Path, help="reviewed preview plan for apply")
    parser.add_argument("--confirm-digest", help="exact SHA-256 plan digest")
    parser.add_argument("--apply", action="store_true", help="apply exact reviewed plan; otherwise preview")
    args = parser.parse_args()
    try:
        return asyncio.run(_cli(args))
    except CleanupError as exc:
        print(json.dumps({"status": "refused", "reason": exc.code}, sort_keys=True))
        return 2
    except KeyboardInterrupt:
        print(json.dumps({"status": "refused", "reason": "interrupted"}, sort_keys=True))
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

