"""Seed a LOCAL database with the c226 load-test population and emit its manifest.

    DATABASE_URL='postgresql+asyncpg://chirp:chirp@localhost:5432/chirp_load' \
      .venv/bin/python scripts/seed_loadtest.py --users 60 --manifest-out loadtest-manifest.json

Creates one campus, one chapter, and N verified member accounts (uids
load-u0001..), then writes the manifest loadtest/accounts.py consumes. Direct DB
writes are required because .edu verification and chapter membership have no
open API — which is also why this script can never run against prod: it reuses
seed_dev_accounts.py's refusal to touch any non-local DATABASE_URL, and prod
provisioning (if the park is ever lifted) is a Jose/manager step done elsewhere.

Idempotent: re-running updates rows in place and emits the same manifest.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

from sqlalchemy import select

# Import-time safety: `app` (and the sibling seed script) must resolve to this checkout.
_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_here, ".."))
sys.path.insert(0, _here)

from app import models  # noqa: E402
from app.db import get_session_factory  # noqa: E402
from seed_dev_accounts import assert_local  # noqa: E402

CAMPUS_SLUG = "load-test-university"
CAMPUS_NAME = "Load Test University"
ORG_NAME = "Load Test Org"


async def seed(user_count: int, manifest_out: str) -> None:
    factory = get_session_factory()
    now = datetime.now(timezone.utc)
    async with factory() as session:
        campus = (
            await session.execute(select(models.Campus).where(models.Campus.slug == CAMPUS_SLUG))
        ).scalar_one_or_none()
        if campus is None:
            campus = models.Campus(name=CAMPUS_NAME, slug=CAMPUS_SLUG, email_domains=None)
            session.add(campus)
            await session.flush()

        chapter = (
            await session.execute(
                select(models.Chapter).where(
                    models.Chapter.campus_id == campus.id,
                    models.Chapter.org_name == ORG_NAME,
                )
            )
        ).scalar_one_or_none()
        if chapter is None:
            chapter = models.Chapter(campus_id=campus.id, org_name=ORG_NAME, chapter_name="Alpha")
            session.add(chapter)
            await session.flush()

        uids = [f"load-u{i:04d}" for i in range(1, user_count + 1)]
        existing_users = {
            u.firebase_uid: u
            for u in (
                await session.execute(
                    select(models.User).where(models.User.firebase_uid.in_(uids))
                )
            ).scalars()
        }
        users: list[models.User] = []
        for uid in uids:
            user = existing_users.get(uid)
            if user is None:
                user = models.User(
                    firebase_uid=uid,
                    email=f"{uid}@loadtest.invalid",
                    display_name=f"Load User {uid[-4:]}",
                    account_type="greek",
                )
                session.add(user)
            user.campus_id = campus.id
            user.campus_verified_at = now
            users.append(user)
        await session.flush()

        user_ids = [u.id for u in users]
        existing_memberships = {
            m.user_id
            for m in (
                await session.execute(
                    select(models.Membership).where(
                        models.Membership.chapter_id == chapter.id,
                        models.Membership.user_id.in_(user_ids),
                    )
                )
            ).scalars()
        }
        for user in users:
            if user.id not in existing_memberships:
                session.add(
                    models.Membership(
                        user_id=user.id,
                        chapter_id=chapter.id,
                        role="member",
                        status="active",
                    )
                )
        await session.commit()

        manifest = {
            "campus_id": str(campus.id),
            "chapter_id": str(chapter.id),
            "users": [{"uid": uid} for uid in uids],
        }
    with open(manifest_out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    print(f"seeded {user_count} users on campus {campus.id}, chapter {chapter.id}")
    print(f"manifest written to {manifest_out}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--users", type=int, default=60)
    parser.add_argument("--manifest-out", default="loadtest-manifest.json")
    args = parser.parse_args()
    if args.users < 1:
        raise SystemExit("--users must be at least 1")
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise SystemExit("set DATABASE_URL explicitly (a local URL) — no default here on purpose")
    assert_local(url)
    asyncio.run(seed(args.users, args.manifest_out))


if __name__ == "__main__":
    main()
