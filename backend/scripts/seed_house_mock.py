"""Seed a LOCAL dev database with a voted-on Touse/Bouse week (board card c175).

    DATABASE_URL='postgresql+asyncpg://chirp:chirp@localhost:5442/chirp' \
      .venv/bin/python scripts/seed_house_mock.py

Why this exists: houses.tsx renders a leaderboard, an unranked section, and a term
title race, and none of those three states is reachable from an empty database. The
weekly ranking needs more houses than the two seed_dev_accounts.py creates, the
unranked section needs houses UNDER the five-vote threshold, and the title race needs
several FINISHED weeks behind the current one. Running the app against a fresh dev
database shows an empty screen that looks broken but is merely unvoted.

Run seed_dev_accounts.py FIRST - this script extends what that one creates and reads
the campus and the two chapters from it rather than inventing its own.

WHAT THIS DELIBERATELY DOES NOT DO: it does not go through the API. Ballots are
written straight to the table because the route computes `week_start` from the clock
and refuses a client-supplied week (routers/house.py), which is exactly the property
that makes finished weeks unforgeable - and exactly why a title race spanning five
weeks cannot be produced by calling it. Writing rows directly is the honest way to
build backdated history; weakening the route to allow it would be a real regression.

NO PASSWORDS ARE INVOLVED. The invented voters exist only as `users` rows to satisfy
the ballot foreign key. They authenticate to nothing.

Idempotent: it clears its own mock voters and their ballots, then rebuilds them, so
re-running produces the same board rather than stacking a second election on top.
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

# Import-time safety: `app` must resolve to this checkout.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from app import models  # noqa: E402
from app.core.windows import current_week_start  # noqa: E402
from app.db import get_session_factory  # noqa: E402

NOW = datetime.now(timezone.utc)

# Same guard rail as seed_dev_accounts.py, and for the same reason: this script writes
# invented people and must never be pointed at a real database.
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def assert_local(url: str) -> None:
    lowered = url.lower()
    if "/cloudsql/" in lowered or "host=/" in lowered:
        raise SystemExit(
            "REFUSING: that DATABASE_URL is a Cloud SQL socket URL. This script "
            "seeds fake ballots and must only ever touch a local database."
        )
    if not any(host in lowered for host in LOCAL_HOSTS):
        raise SystemExit(
            f"REFUSING: DATABASE_URL host is not local ({url!r}). This script seeds "
            "fake ballots and must only ever touch a local database."
        )


# --------------------------------------------------------------------------
# The field. Sigma Chi and Alpha Delta Pi already exist from seed_dev_accounts.py;
# the rest are added here so a ranking has something to rank.
# --------------------------------------------------------------------------
HOUSES = {
    "sigma_chi": ("Sigma Chi", "Epsilon Mu"),
    "adpi": ("Alpha Delta Pi", "Beta Kappa"),
    "kappa_sig": ("Kappa Sigma", "Gamma Nu"),
    "phi_delt": ("Phi Delta Theta", "North Carolina Beta"),
    "chi_o": ("Chi Omega", "Lambda Alpha"),
    "delta_gamma": ("Delta Gamma", "Eta Xi"),
    "pike": ("Pi Kappa Alpha", "Theta Rho"),
    "zta": ("Zeta Tau Alpha", "Iota Psi"),
    "sigma_nu": ("Sigma Nu", "Kappa Upsilon"),
}

# Per week, per house: (touse_votes, bouse_votes).
#
# Shaped rather than random, so every state the screen can render is actually on
# screen in the current week:
#   * a decisive Touse (kappa_sig) and a decisive Bouse (pike, deep negative net)
#   * houses with a NEGATIVE net that are still ranked - being disliked is not the
#     same as being unranked, and the screen has to show that difference
#   * two houses under MIN_VOTES_TO_RANK (zta, sigma_nu) to fill the unranked section
#   * fewer bouse votes than ballots, because naming a Bouse is optional
#
# Week 0 is the live week; -1..-3 are finished weeks, and they exist only to give the
# term title race a real record. Their winners are varied on purpose so the title is
# decided on weekly wins rather than looking like one house ran away with everything.
#
# IT STOPS AT FOUR WEEKS BECAUSE THE TERM DOES. current_term() puts Fall at August 1st,
# and the title race only counts weeks inside the term, so a fifth week seeded here
# would land in July, score nothing, and quietly make the seeded record disagree with
# the screen. Move this list forward if you re-run deep into a semester.
WEEKS: list[dict[str, tuple[int, int]]] = [
    # Week 0 - the current, live week. 73 ballots, 42 of which named a Bouse.
    {
        "kappa_sig": (22, 1),
        "sigma_chi": (15, 2),
        "chi_o": (12, 1),
        "adpi": (9, 3),
        "delta_gamma": (6, 4),
        "phi_delt": (4, 9),
        "pike": (2, 19),
        "zta": (2, 1),
        "sigma_nu": (1, 2),
    },
    # Week -1 - kappa_sig again.
    {
        "kappa_sig": (19, 2),
        "chi_o": (14, 2),
        "sigma_chi": (11, 3),
        "delta_gamma": (8, 3),
        "adpi": (7, 5),
        "pike": (3, 15),
        "phi_delt": (3, 8),
        "sigma_nu": (2, 1),
        "zta": (1, 1),
    },
    # Week -2 - sigma_chi takes one.
    {
        "sigma_chi": (21, 1),
        "kappa_sig": (16, 3),
        "adpi": (11, 2),
        "chi_o": (9, 4),
        "phi_delt": (6, 6),
        "delta_gamma": (5, 5),
        "pike": (2, 14),
        "zta": (3, 2),
        "sigma_nu": (1, 1),
    },
    # Week -3 - chi_o's week, and the first one inside the Fall term.
    {
        "chi_o": (18, 2),
        "kappa_sig": (15, 3),
        "sigma_chi": (12, 2),
        "adpi": (10, 4),
        "delta_gamma": (7, 4),
        "pike": (4, 13),
        "phi_delt": (3, 9),
        "sigma_nu": (3, 1),
        "zta": (1, 2),
    },
]

# The account you actually look at the screen as. Its ballot is pinned rather than
# left to the shuffle, so "Your vote this week" is a known, stable pair instead of
# whatever the generator happened to hand Marcus Webb.
VIEWER_UID = "dev-president"
VIEWER_TOUSE = "sigma_chi"
VIEWER_BOUSE = "pike"

MOCK_VOTER_PREFIX = "mock-voter-"

# Deterministic: the same board every run. A shuffled-but-unseeded generator would
# quietly change the pairing on every re-seed, which makes a screenshot impossible to
# reproduce and any bug found on this data impossible to re-hit.
RNG = random.Random(1175)

FIRST_NAMES = [
    "Ava", "Noah", "Mia", "Liam", "Zoe", "Ethan", "Maya", "Caleb", "Nina", "Owen",
    "Iris", "Jonah", "Elena", "Miles", "Ruby", "Silas", "Talia", "Rowan", "June",
    "Felix", "Cora", "Desmond", "Wren", "Amir", "Lena", "Hugo", "Sasha", "Theo",
    "Nadia", "Emmett", "Paloma", "Kai", "Freya", "Andre", "Yusuf", "Beatriz",
]
LAST_NAMES = [
    "Ortega", "Bennett", "Haruki", "Salas", "Whitmore", "Duarte", "Kovacs", "Ellison",
    "Nakamura", "Feld", "Abara", "Lindqvist", "Moreau", "Castellanos", "Boone",
    "Petrov", "Adeyemi", "Vance", "Quinn", "Rasmussen", "Okonkwo", "Silva",
]


async def upsert_chapter(session, campus_id, org_name: str, chapter_name: str):
    existing = await session.scalar(
        select(models.Chapter).where(
            models.Chapter.campus_id == campus_id,
            models.Chapter.org_name == org_name,
        )
    )
    if existing is not None:
        existing.chapter_name = chapter_name
        return existing
    chapter = models.Chapter(
        campus_id=campus_id, org_name=org_name, chapter_name=chapter_name
    )
    session.add(chapter)
    await session.flush()
    return chapter


def build_ballots(
    plan: dict[str, tuple[int, int]],
    pinned: tuple[str, str | None] | None,
) -> list[tuple[str, str | None]]:
    """Turn per-house vote totals into (touse_key, bouse_key) ballot pairs.

    The totals are the input because they are what the screen shows; ballots are what
    the table stores. One ballot carries one Touse and at most one Bouse, so the two
    columns are shuffled independently and then zipped, with the Bouse list padded with
    None up to the ballot count - that padding IS the "naming a Bouse is optional"
    behaviour rather than a special case bolted on afterwards.

    A pair naming the same house twice is re-drawn rather than dropped: the CHECK
    constraint would reject it, and dropping it would silently make the totals on
    screen disagree with the totals asked for here.
    """
    touse_pool: list[str] = []
    bouse_pool: list[str] = []
    for key, (touse_votes, bouse_votes) in plan.items():
        touse_pool.extend([key] * touse_votes)
        bouse_pool.extend([key] * bouse_votes)

    if pinned is not None:
        # Spend the viewer's ballot out of the pools so the published totals stay
        # exactly what WEEKS asks for instead of gaining one extra vote.
        touse_pool.remove(pinned[0])
        if pinned[1] is not None:
            bouse_pool.remove(pinned[1])

    RNG.shuffle(touse_pool)
    bouse_pool.extend([None] * (len(touse_pool) - len(bouse_pool)))
    RNG.shuffle(bouse_pool)

    pairs: list[tuple[str, str | None]] = []
    for index, touse in enumerate(touse_pool):
        bouse = bouse_pool[index]
        if bouse == touse:
            # Swap with any later slot that does not create the same collision.
            for other in range(index + 1, len(bouse_pool)):
                candidate = bouse_pool[other]
                if candidate != touse and candidate != touse_pool[other]:
                    bouse_pool[index], bouse_pool[other] = candidate, bouse
                    bouse = candidate
                    break
            else:
                bouse = None
        pairs.append((touse, bouse))

    if pinned is not None:
        pairs.insert(0, pinned)
    return pairs


async def main() -> None:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise SystemExit("Set DATABASE_URL to a LOCAL database first.")
    assert_local(url)

    session_factory = get_session_factory()
    async with session_factory() as session:
        campus = await session.scalar(select(models.Campus))
        if campus is None:
            raise SystemExit(
                "No campus found. Run scripts/seed_dev_accounts.py against this "
                "database first - this script extends it."
            )

        viewer = await session.scalar(
            select(models.User).where(models.User.firebase_uid == VIEWER_UID)
        )
        if viewer is None:
            raise SystemExit(
                f"No {VIEWER_UID} user. Run scripts/seed_dev_accounts.py first."
            )

        chapters = {}
        for key, (org_name, chapter_name) in HOUSES.items():
            chapters[key] = await upsert_chapter(
                session, campus.id, org_name, chapter_name
            )

        # Clear this script's own rows before rebuilding. Ballots first: they carry the
        # FK to the voters. Only mock voters are touched - the twelve dev accounts and
        # any ballot cast through the UI by a real dev account are left alone, except
        # the viewer's own pinned ballot, which is replaced below.
        mock_voter_ids = (
            await session.scalars(
                select(models.User.id).where(
                    models.User.firebase_uid.like(f"{MOCK_VOTER_PREFIX}%")
                )
            )
        ).all()
        if mock_voter_ids:
            await session.execute(
                delete(models.HouseBallot).where(
                    models.HouseBallot.voter_id.in_(mock_voter_ids)
                )
            )
            await session.execute(
                delete(models.User).where(models.User.id.in_(mock_voter_ids))
            )
        await session.execute(
            delete(models.HouseBallot).where(models.HouseBallot.voter_id == viewer.id)
        )
        await session.flush()

        # One voter pool, reused across weeks: the same students voting week after week
        # is what a campus actually looks like, and it keeps the user count sane.
        biggest_week = max(sum(t for t, _ in plan.values()) for plan in WEEKS)
        voters = []
        for index in range(biggest_week):
            first = FIRST_NAMES[index % len(FIRST_NAMES)]
            last = LAST_NAMES[(index // len(FIRST_NAMES) + index) % len(LAST_NAMES)]
            voter = models.User(
                firebase_uid=f"{MOCK_VOTER_PREFIX}{index:03d}",
                email=f"mockvoter{index:03d}@uncg.edu",
                display_name=f"{first} {last}",
                account_type="greek" if index % 3 else "non_greek",
                campus_id=campus.id,
                campus_verified_at=NOW - timedelta(days=30),
            )
            session.add(voter)
            voters.append(voter)
        await session.flush()

        this_monday = current_week_start(NOW)
        total_ballots = 0
        for offset, plan in enumerate(WEEKS):
            week_start = this_monday - timedelta(weeks=offset)
            pinned = (VIEWER_TOUSE, VIEWER_BOUSE) if offset == 0 else None
            pairs = build_ballots(plan, pinned)
            for index, (touse_key, bouse_key) in enumerate(pairs):
                # The viewer casts the pinned ballot; everyone else is a mock voter.
                voter_id = (
                    viewer.id if (offset == 0 and index == 0) else voters[index].id
                )
                session.add(
                    models.HouseBallot(
                        campus_id=campus.id,
                        week_start=week_start,
                        voter_id=voter_id,
                        touse_chapter_id=chapters[touse_key].id,
                        bouse_chapter_id=(
                            chapters[bouse_key].id if bouse_key is not None else None
                        ),
                        created_at=NOW - timedelta(weeks=offset, hours=6),
                        updated_at=NOW - timedelta(weeks=offset, hours=6),
                    )
                )
                total_ballots += 1
        await session.commit()

    live = WEEKS[0]
    ranked = {k: v for k, v in live.items() if sum(v) >= 5}
    unranked = {k: v for k, v in live.items() if sum(v) < 5}
    order = sorted(ranked.items(), key=lambda kv: (kv[1][0] - kv[1][1]), reverse=True)

    print(f"Seeded {total_ballots} ballots across {len(WEEKS)} weeks.\n")
    print(f"  Week of {this_monday} - {sum(t for t, _ in live.values())} ballots cast\n")
    for place, (key, (touse_votes, bouse_votes)) in enumerate(order, start=1):
        net = touse_votes - bouse_votes
        print(
            f"  {place}. {HOUSES[key][0]:<18} "
            f"{touse_votes:>3} Touse  {bouse_votes:>3} Bouse   net {net:+d}"
        )
    print("\n  Not enough votes to rank (under 5):")
    for key, (touse_votes, bouse_votes) in unranked.items():
        print(f"     {HOUSES[key][0]:<18} {touse_votes + bouse_votes} votes")
    print(
        f"\n  Your ballot as {VIEWER_UID}: "
        f"Touse {HOUSES[VIEWER_TOUSE][0]}, Bouse {HOUSES[VIEWER_BOUSE][0]}"
    )


if __name__ == "__main__":
    asyncio.run(main())
