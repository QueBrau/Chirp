"""Seed a LOCAL dev database with one account per user type (board card c159).

    DATABASE_URL='postgresql+asyncpg://chirp:chirp@localhost:5440/chirp' \
      .venv/bin/python scripts/seed_dev_accounts.py

Why this exists: eight roles are defined in app/core/permissions.py and until now
six of them had never been seen rendered, because the app is hardwired to the
chirps-prod Firebase project — so looking at the treasurer dashboard meant being a
real treasurer of a real chapter in production.

NO PASSWORDS ARE INVOLVED ANYWHERE. These accounts only work under
AUTH_MODE=emulated, where the backend trusts an `X-Debug-Firebase-Uid` header and
never touches Firebase. That mode is refused in production by the ENV guard
(SECURITY-REVIEW finding 5), which is the outer of the two locks keeping these
users out of prod; the inner one is that this script refuses to run against a
non-local database at all.

Idempotent: re-running updates the existing rows in place rather than duplicating
them, so it is safe to run after every migration or schema change.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

# Import-time safety: `app` must resolve to this checkout.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from app import models  # noqa: E402
from app.db import get_session_factory  # noqa: E402

NOW = datetime.now(timezone.utc)

# --------------------------------------------------------------------------
# Guard rail. This script writes invented people; it must never be pointed at a
# real database. The check is on the URL rather than on ENV because ENV is easy
# to forget and a socket-form Cloud SQL URL is unmistakable.
# --------------------------------------------------------------------------
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def assert_local(url: str) -> None:
    lowered = url.lower()
    if "/cloudsql/" in lowered or "host=/" in lowered:
        raise SystemExit(
            "REFUSING: that DATABASE_URL is a Cloud SQL socket URL. This script "
            "seeds fake users and must only ever touch a local database."
        )
    if not any(host in lowered for host in LOCAL_HOSTS):
        raise SystemExit(
            f"REFUSING: DATABASE_URL host is not local ({url!r}). This script seeds "
            "fake users and must only ever touch a local database."
        )


# --------------------------------------------------------------------------
# The cast. `uid` is what you put in X-Debug-Firebase-Uid (or ?uid= on web).
# --------------------------------------------------------------------------
CAMPUS = {"name": "UNC Greensboro", "slug": "uncg", "email_domains": ["uncg.edu"]}

SIGMA_CHI = {"org_name": "Sigma Chi", "chapter_name": "Epsilon Mu"}
ALPHA_DELTA_PI = {"org_name": "Alpha Delta Pi", "chapter_name": "Beta Kappa"}

# (uid, display name, email local part, account_type, role|None, chapter, verified)
CAST = [
    ("dev-president", "Marcus Webb", "marcus", "greek", "president", "sigma_chi", True),
    ("dev-vice-president", "Andre Coleman", "andre", "greek", "vice_president", "sigma_chi", True),
    ("dev-treasurer", "Priya Raman", "priya", "greek", "treasurer", "sigma_chi", True),
    ("dev-secretary", "Jordan Ellis", "jordan", "greek", "secretary", "sigma_chi", True),
    ("dev-historian", "Sam Okafor", "sam", "greek", "historian", "sigma_chi", True),
    ("dev-member", "Chris Delgado", "chris", "greek", "member", "sigma_chi", True),
    ("dev-pledge", "Tyler Nguyen", "tyler", "greek", "pledge", "sigma_chi", True),
    ("dev-alumni", "Ray Whitfield", "ray", "alumni", "alumni", "sigma_chi", True),
    # A second chapter, so org colour scoping (DESIGN §8.6) is provable rather than
    # assumed: this president's whole Orgs stack should render in ADPi's colours.
    ("dev-sorority-president", "Naomi Frazier", "naomi", "greek", "president", "adpi", True),
    # c71's first-class user: belongs to no org at all. Campus tab and Yak work,
    # the Orgs tab shows the "find your org" state.
    ("dev-campus-student", "Dana Brooks", "dana", "non_greek", None, None, True),
    # The c88 gate state. Has a campus but NO .edu verification, so campus content
    # is refused and c90's verify screen is the designed destination. This account
    # is the only way to see that screen without deliberately breaking a good one.
    ("dev-unverified", "Alex Moreno", "alex", "non_greek", None, None, False),
    # c28: platform admin. No API grants this, so it is set here the same way the
    # test suite sets it.
    ("dev-admin", "Platform Admin", "admin", "non_greek", None, None, True),
]

# (days ago, entry_type, cents, category, description, payer uid or None)
#
# THE PAYER UID IS LOAD-BEARING, not decoration. Every dues_payment here used to name
# its payer in the DESCRIPTION only and leave related_user_id NULL, so the money existed
# and belonged to nobody. Anything that answers "who has paid" by grouping on that column
# read zero: the President overview (c171) showed "$2,250 collected / 0 of 8 paid" on a
# freshly seeded stack, and the double-charge guard (payments.py) would have let every
# seeded member pay again. Production was never affected - the Stripe webhook sets
# related_user_id - so this was purely a fixture lying to whoever opened the app next.
LEDGER = [
    # Opening carryover, so the running-balance line starts somewhere real rather
    # than at zero and the chapter is not left looking insolvent on a demo screen.
    (104, "budget_allocation", 150_000, "carryover", "Carryover from spring term", None),
    (95, "dues_payment", 45_000, "dues", "Dues - Marcus Webb", "dev-president"),
    (94, "dues_payment", 45_000, "dues", "Dues - Andre Coleman", "dev-vice-president"),
    (93, "dues_payment", 45_000, "dues", "Dues - Priya Raman", "dev-treasurer"),
    (88, "expense", -48_000, "rush", "Rush week cookout", None),
    (74, "expense", -96_000, "formal", "Formal venue deposit", None),
    (66, "dues_payment", 45_000, "dues", "Dues - Jordan Ellis", "dev-secretary"),
    (54, "expense", -31_000, "house", "House repairs", None),
    (43, "expense", -22_000, "philanthropy", "Derby Days donation", None),
    (36, "budget_allocation", 30_000, "operations", "Operating float", None),
    (31, "expense", -14_500, "letters", "Chapter letters and merch", None),
    (22, "dues_payment", 45_000, "dues", "Dues - Chris Delgado", "dev-member"),
    (15, "expense", -9_000, "socials", "Mixer supplies", None),
    (8, "expense", -6_200, "operations", "Printing and postage", None),
    (3, "expense", -4_100, "philanthropy", "Canned food drive", None),
]


async def upsert_campus(session) -> models.Campus:
    existing = (
        await session.execute(select(models.Campus).where(models.Campus.slug == CAMPUS["slug"]))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    campus = models.Campus(**CAMPUS)
    session.add(campus)
    await session.flush()
    return campus


async def upsert_chapter(session, campus_id, spec: dict) -> models.Chapter:
    existing = (
        await session.execute(
            select(models.Chapter).where(
                models.Chapter.campus_id == campus_id,
                models.Chapter.org_name == spec["org_name"],
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    chapter = models.Chapter(campus_id=campus_id, **spec)
    session.add(chapter)
    await session.flush()
    return chapter


async def main() -> None:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise SystemExit("Set DATABASE_URL (see the module docstring for the exact command).")
    assert_local(url)

    factory = get_session_factory()
    async with factory() as session:
        campus = await upsert_campus(session)
        chapters = {
            "sigma_chi": await upsert_chapter(session, campus.id, SIGMA_CHI),
            "adpi": await upsert_chapter(session, campus.id, ALPHA_DELTA_PI),
        }

        users: dict[str, models.User] = {}
        for uid, name, local, account_type, role, chapter_key, verified in CAST:
            user = (
                await session.execute(
                    select(models.User).where(models.User.firebase_uid == uid)
                )
            ).scalar_one_or_none()
            if user is None:
                user = models.User(firebase_uid=uid, email=f"{local}@uncg.edu")
                session.add(user)
            user.display_name = name
            user.email = f"{local}@uncg.edu"
            user.account_type = account_type
            user.campus_id = campus.id
            user.is_platform_admin = uid == "dev-admin"
            # The c88 gate keys on this timestamp, never on campus_id.
            user.campus_verified_at = NOW - timedelta(days=10) if verified else None
            await session.flush()
            users[uid] = user

            if role is not None and chapter_key is not None:
                chapter = chapters[chapter_key]
                membership = (
                    await session.execute(
                        select(models.Membership).where(
                            models.Membership.user_id == user.id,
                            models.Membership.chapter_id == chapter.id,
                        )
                    )
                ).scalar_one_or_none()
                if membership is None:
                    membership = models.Membership(user_id=user.id, chapter_id=chapter.id)
                    session.add(membership)
                membership.role = role
                membership.status = "active"
                membership.pledge_class = "Fall 2025" if role == "pledge" else "Spring 2024"
                await session.flush()

        sigma = chapters["sigma_chi"]
        treasurer = users["dev-treasurer"]

        # --- finance: enough shape for the c118 charts to be worth looking at ---
        cycle = (
            await session.execute(
                select(models.DuesCycle).where(models.DuesCycle.chapter_id == sigma.id)
            )
        ).scalar_one_or_none()
        if cycle is None:
            cycle = models.DuesCycle(
                chapter_id=sigma.id,
                name="Fall 2026 dues",
                amount_cents=45_000,
                due_date=date.today() + timedelta(days=21),
            )
            session.add(cycle)
            await session.flush()

        existing_entries = (
            await session.execute(
                select(models.LedgerEntry).where(models.LedgerEntry.chapter_id == sigma.id)
            )
        ).scalars().all()
        if not existing_entries:
            for days_ago, entry_type, cents, category, description, payer_uid in LEDGER:
                session.add(
                    models.LedgerEntry(
                        chapter_id=sigma.id,
                        entry_type=entry_type,
                        amount_cents=cents,
                        category=category,
                        description=description,
                        created_by=treasurer.id,
                        created_at=NOW - timedelta(days=days_ago),
                        dues_cycle_id=cycle.id if entry_type == "dues_payment" else None,
                        # Attributed, so "who has paid" is answerable from the column
                        # rather than only from the description text. Deliberately leaves
                        # three of the eight actives unpaid - a roster where everyone has
                        # paid cannot show the outstanding state any dues screen exists to
                        # surface.
                        related_user_id=users[payer_uid].id if payer_uid else None,
                    )
                )

        existing_approvals = (
            await session.execute(
                select(models.SpendApproval).where(models.SpendApproval.chapter_id == sigma.id)
            )
        ).scalars().all()
        if not existing_approvals:
            session.add(
                models.SpendApproval(
                    chapter_id=sigma.id,
                    requested_by=users["dev-secretary"].id,
                    amount_cents=18_500,
                    description="Banquet catering deposit",
                    status="pending",
                    created_at=NOW - timedelta(days=2),
                )
            )
            session.add(
                models.SpendApproval(
                    chapter_id=sigma.id,
                    requested_by=users["dev-member"].id,
                    amount_cents=7_400,
                    description="Intramural jerseys",
                    status="approved",
                    decided_by=treasurer.id,
                    decided_at=NOW - timedelta(days=9),
                    created_at=NOW - timedelta(days=11),
                )
            )

        # --- content, so the feeds are not empty on every account ---
        existing_posts = (
            await session.execute(select(models.Post).where(models.Post.campus_id == campus.id))
        ).scalars().all()
        if not existing_posts:
            session.add(
                models.Post(
                    chapter_id=sigma.id,
                    campus_id=campus.id,
                    author_id=users["dev-president"].id,
                    body="Chapter meeting moved to Thursday, 7pm, EUC Claxton.",
                    audience="org",
                    created_at=NOW - timedelta(hours=20),
                )
            )
            session.add(
                models.Post(
                    chapter_id=None,
                    campus_id=campus.id,
                    author_id=users["dev-campus-student"].id,
                    body="Anyone else stuck in the College Ave construction detour every morning?",
                    audience="campus",
                    created_at=NOW - timedelta(hours=6),
                )
            )
            session.add(
                models.Post(
                    chapter_id=sigma.id,
                    campus_id=campus.id,
                    author_id=users["dev-member"].id,
                    body="Derby Days raised $2,200 for the food bank. Thanks to everyone who showed up.",
                    audience="campus",
                    created_at=NOW - timedelta(days=2),
                )
            )

        await session.commit()

    print("Seeded. Accounts (use as X-Debug-Firebase-Uid, or ?uid= on web):\n")
    for uid, name, _local, account_type, role, chapter_key, verified in CAST:
        where = {"sigma_chi": "Sigma Chi", "adpi": "Alpha Delta Pi"}.get(chapter_key, "no org")
        flags = [] if verified else ["UNVERIFIED"]
        if uid == "dev-admin":
            flags.append("platform admin")
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        print(f"  {uid:24} {name:18} {role or '-':16} {where:16} {account_type}{suffix}")


if __name__ == "__main__":
    asyncio.run(main())
