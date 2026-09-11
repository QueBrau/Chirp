"""Seed/teardown a fixed roster of prod-safe QA fixtures for GET /users/search (c380).

WHY THIS EXISTS: pre-launch, a fresh campus has no rows that look like search
results, so there is nothing to demo GET /users/search (board c322) against. This
is a standalone, Jose-only operator tool that seeds eight harmless, obviously-fake
people onto one real campus in PRODUCTION, over the cloud-sql-proxy, so search can
be demonstrated before real students exist. It is paired with
`scripts/seed-prod-search-fixtures` at the repo root.

    DATABASE_URL='postgresql+asyncpg://chirp:...@localhost:5433/chirp' \
      scripts/seed-prod-search-fixtures --campus-slug uncg --apply

THE MARKER (reused columns, no migration — Alembic head stays at 0039): every
fixture's email is `<slug>@fixtures.chirp.invalid` (RFC 2606: `.invalid` can never
be a real deliverable address and can never collide with a real student's), every
display_name is prefixed `QA Fixture: ` so a human looking at a list sees it
immediately, and every firebase_uid is prefixed `qa-fixture-`. Seeding is an
upsert keyed on the email marker (idempotent). Teardown deletes exactly the rows
matching `campus_id = :cid AND email LIKE '%@fixtures.chirp.invalid'` — nothing
broader, so a real user who happens to share a campus with fixtures is never at
risk.

WHY A FIXTURE CAN NEVER AUTHENTICATE: Firebase issues its own opaque, ~28-character
base62 uid for every real account, generated server-side at signup — a client
never chooses it and can never predict it. The literal, human-typed string
`qa-fixture-<slug>` this script writes can therefore never appear as the `uid`
claim of a token Firebase actually signs, so `middleware.auth`'s
`select(User).where(User.firebase_uid == uid)` lookup (fed only by a verified
Firebase ID token, or by AUTH_MODE=emulated's debug header) can never match a
fixture row through the real auth path. AUTH_MODE=emulated is itself refused in
production by the ENV guard (SECURITY-REVIEW finding 5) — a second, independent
lock. Fixtures carry no chapter membership and no other identity-bearing state;
they exist to be *found*, never to *act*.

THE PROXY GUARD is the opposite shape of `seed_dev_accounts.py`'s `assert_local`:
that script refuses anything but a local database because it seeds throwaway dev
data. This script deliberately WRITES TO PRODUCTION, so it refuses anything that
is not a cloud-sql-proxy connection forwarded to localhost/127.0.0.1 on port 5433
exactly — a bare dev port (5432/5440), a Cloud SQL unix-socket URL, or a remote
host are all refused before any connection is opened. DATABASE_URL is read only
from the environment, is never logged, and no part of it (including host/port) is
ever printed back — refusal messages name only the REQUIRED target, never what was
given.

Dry-run is the default for both seeding and teardown; --apply is required to
write. Every mode prints one JSON summary line to stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime for --help
    from sqlalchemy.ext.asyncio import AsyncSession

# Import-time safety: `app` must resolve to this checkout, exactly like
# seed_dev_accounts.py. This only edits sys.path; it never imports `app` itself,
# so `--help` works even in an environment where `app` is not importable.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

# --------------------------------------------------------------------------
# The proxy guard. Checked before any DB connection is opened.
# --------------------------------------------------------------------------
PROXY_HOSTS = ("localhost", "127.0.0.1")
PROXY_PORT = 5433


def _refuse(message: str) -> None:
    """Print a refusal to stderr and exit 2 — every guard in this module refuses
    the same way, distinct from an ordinary uncaught-exception exit."""
    print(message, file=sys.stderr)
    raise SystemExit(2)


def _assert_via_proxy(url: str) -> None:
    """Raise SystemExit(2) unless `url` is a cloud-sql-proxy connection on the
    expected host/port. Never echoes any part of `url` back."""
    lowered = url.lower()
    if "/cloudsql/" in lowered or "host=/" in lowered:
        _refuse(
            "REFUSING: that DATABASE_URL names a Cloud SQL unix socket, not a proxy "
            "connection. This script writes to production and must go through "
            f"cloud-sql-proxy listening on localhost or 127.0.0.1, port {PROXY_PORT} exactly."
        )
    parsed = urlsplit(url)
    if parsed.hostname not in PROXY_HOSTS or parsed.port != PROXY_PORT:
        _refuse(
            "REFUSING: DATABASE_URL must point at cloud-sql-proxy on localhost or "
            f"127.0.0.1, port {PROXY_PORT} exactly. This script writes to the "
            "production database and refuses anything else."
        )


# --------------------------------------------------------------------------
# The roster. Fixed at eight (manager ruling R2) — no --count flag.
# --------------------------------------------------------------------------
FIXTURE_EMAIL_DOMAIN = "fixtures.chirp.invalid"
FIXTURE_NAME_PREFIX = "QA Fixture: "
FIXTURE_UID_PREFIX = "qa-fixture-"

ROSTER: list[tuple[str, str]] = [
    ("avery-chen", "Avery Chen"),
    ("jordan-blake", "Jordan Blake"),
    ("riley-thompson", "Riley Thompson"),
    ("morgan-diaz", "Morgan Diaz"),
    ("casey-nguyen", "Casey Nguyen"),
    ("peyton-martinez", "Peyton Martinez"),
    ("skyler-brooks", "Skyler Brooks"),
    ("reese-patel", "Reese Patel"),
]


def _fixture_specs() -> list[dict[str, str]]:
    return [
        {
            "slug": slug,
            "email": f"{slug}@{FIXTURE_EMAIL_DOMAIN}",
            "display_name": f"{FIXTURE_NAME_PREFIX}{name}",
            "firebase_uid": f"{FIXTURE_UID_PREFIX}{slug}",
        }
        for slug, name in ROSTER
    ]


class CampusNotFound(ValueError):
    """Raised by `_resolve_campus` when the given id/slug matches no campus row."""


async def _resolve_campus(
    session: "AsyncSession", campus_id: str | uuid.UUID | None, campus_slug: str | None
) -> Any:
    """SELECT-only campus lookup, checked before any write in either job.

    Deliberately raises rather than letting a bad campus_id fall through to the
    users.campus_id foreign key, which would surface as a raw IntegrityError after
    a partially built session instead of a clean, checked refusal.
    """
    from sqlalchemy import select

    from app import models

    if campus_id is not None:
        try:
            cid = campus_id if isinstance(campus_id, uuid.UUID) else uuid.UUID(str(campus_id))
        except (ValueError, AttributeError, TypeError) as exc:
            raise CampusNotFound(f"{campus_id!r} is not a valid campus id (uuid)") from exc
        campus = (
            await session.execute(select(models.Campus).where(models.Campus.id == cid))
        ).scalar_one_or_none()
    elif campus_slug is not None:
        campus = (
            await session.execute(select(models.Campus).where(models.Campus.slug == campus_slug))
        ).scalar_one_or_none()
    else:
        raise ValueError("campus_id or campus_slug is required")
    if campus is None:
        raise CampusNotFound(
            f"no campus found for campus_id={campus_id!r} campus_slug={campus_slug!r}"
        )
    return campus


# --------------------------------------------------------------------------
# The two job functions. Each opens its own session; both are plain async
# functions independent of argv/env-guard checks (mirrors app.jobs.purge), which
# is what lets tests call them directly against a scratch DB without ever
# exercising (or needing to bypass) the proxy-port guard.
# --------------------------------------------------------------------------


async def run_seed_job(
    *,
    campus_id: str | uuid.UUID | None = None,
    campus_slug: str | None = None,
    apply: bool = False,
) -> dict[str, object]:
    """Upsert the fixed 8-fixture roster onto one campus, keyed by the email marker.

    Idempotent by construction: re-running updates the existing 8 rows in place
    rather than duplicating them.
    """
    from sqlalchemy import select

    from app import models
    from app.db import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        campus = await _resolve_campus(session, campus_id, campus_slug)
        specs = _fixture_specs()
        existing = (
            await session.execute(
                select(models.User).where(
                    models.User.email.in_([spec["email"] for spec in specs])
                )
            )
        ).scalars().all()
        by_email = {u.email: u for u in existing}

        created = 0
        updated = 0
        fixtures_out: list[dict[str, str]] = []
        for spec in specs:
            user = by_email.get(spec["email"])
            if user is None:
                created += 1
                if apply:
                    session.add(
                        models.User(
                            firebase_uid=spec["firebase_uid"],
                            email=spec["email"],
                            display_name=spec["display_name"],
                            account_type="non_greek",
                            campus_id=campus.id,
                            is_ghost=False,
                            suspended_at=None,
                            campus_verified_at=None,
                        )
                    )
            else:
                updated += 1
                if apply:
                    user.display_name = spec["display_name"]
                    user.firebase_uid = spec["firebase_uid"]
                    user.campus_id = campus.id
                    user.is_ghost = False
                    user.suspended_at = None
            fixtures_out.append({"display_name": spec["display_name"], "email": spec["email"]})

        # Captured BEFORE commit/rollback: either expires the `campus` ORM object,
        # and touching its attributes afterward would trigger an implicit refresh
        # outside the greenlet context the async driver needs for that.
        campus_id_str = str(campus.id)
        if apply:
            await session.commit()
            return {
                "mode": "apply", "action": "seed", "campus_id": campus_id_str,
                "created": created, "updated": updated,
            }
        await session.rollback()
        return {
            "mode": "dry_run", "action": "seed", "campus_id": campus_id_str,
            "would_create": created, "would_update": updated, "fixtures": fixtures_out,
        }


async def run_teardown_job(
    *,
    campus_id: str | uuid.UUID | None = None,
    campus_slug: str | None = None,
    apply: bool = False,
) -> dict[str, object]:
    """Delete exactly the marker-matched rows: campus_id = :cid AND email LIKE
    '%@fixtures.chirp.invalid'. Never anything broader."""
    from sqlalchemy import delete, select

    from app import models
    from app.db import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        campus = await _resolve_campus(session, campus_id, campus_slug)
        matched_ids = (
            await session.execute(
                select(models.User.id).where(
                    models.User.campus_id == campus.id,
                    models.User.email.like(f"%@{FIXTURE_EMAIL_DOMAIN}"),
                )
            )
        ).scalars().all()
        campus_id_str = str(campus.id)  # captured before commit/rollback expires `campus`

        if apply:
            if matched_ids:
                await session.execute(
                    delete(models.User).where(models.User.id.in_(matched_ids))
                )
            await session.commit()
            return {
                "mode": "apply", "action": "teardown", "campus_id": campus_id_str,
                "deleted": len(matched_ids),
            }
        await session.rollback()
        return {
            "mode": "dry_run", "action": "teardown", "campus_id": campus_id_str,
            "would_delete": len(matched_ids), "ids": [str(i) for i in matched_ids],
        }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    """Dry-run by default for both seed and teardown; --apply writes."""
    parser = argparse.ArgumentParser(description=__doc__)
    campus_group = parser.add_mutually_exclusive_group(required=True)
    campus_group.add_argument("--campus-id", help="target campus UUID")
    campus_group.add_argument("--campus-slug", help="target campus slug")
    parser.add_argument(
        "--apply", action="store_true", help="write changes (default: preview only)"
    )
    parser.add_argument(
        "--teardown", action="store_true", help="delete fixtures instead of seeding them"
    )
    args = parser.parse_args(argv)

    url = os.environ.get("DATABASE_URL", "")
    if not url:
        _refuse(
            "REFUSING: DATABASE_URL is not set. Point it at a cloud-sql-proxy connection "
            f"on localhost or 127.0.0.1, port {PROXY_PORT} exactly, then re-run."
        )
    _assert_via_proxy(url)

    job = run_teardown_job if args.teardown else run_seed_job
    try:
        result = asyncio.run(
            job(campus_id=args.campus_id, campus_slug=args.campus_slug, apply=args.apply)
        )
    except CampusNotFound as exc:
        _refuse(f"REFUSING: {exc}")
        return
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
