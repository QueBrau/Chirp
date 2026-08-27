"""Touse/Bouse weekly house leaderboard (board card c175).

Every campus-verified student casts ONE ballot a week naming a top house and, optionally,
a bottom house. This module owns casting a ballot, one week's ranking, and the race for
the term title ("Touse of Fall 26").

NOTHING HERE STORES A STANDING. Every number is derived from raw ballots at read time,
which is what keeps c175's "the bottom of the ranking is public" decision reversible:
retreating to an e-board-only Bouse, or to naming only the top half, is an edit to what
these functions SERIALIZE and needs neither a migration nor a data rewrite.

THE GATE IS require_campus_member ON EVERY ROUTE, read and write alike. That dependency
is the .edu check (c88), and it is the only thing standing between a campus popularity
contest and anyone on the internet with an account. A read here is not "public data" in
the sense that would justify relaxing it: the ranking names real organisations, and
being able to watch it without belonging to the campus is exactly what an off-campus
brigade would want.
"""

import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import Integer, and_, func, literal, select, union_all
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.core.campus_access import require_campus_member
from app.core.windows import current_term, current_week_start
from app.db import get_session
from app.schemas.house import (
    HouseBallotCreate,
    HouseBallotOut,
    HouseLeaderboard,
    HouseStanding,
    TermStanding,
    TermTitleRace,
    UnrankedHouse,
)

router = APIRouter(tags=["house"])

# How many votes a house needs before it is placed in the ranking at all.
#
# NOT a performance knob - it is the difference between "this house came last" and "three
# people voted". Publishing a bottom placement drawn from a handful of ballots would name
# a real organisation the worst on campus on the strength of noise, and the Bouse is
# public (c175). Houses under the line are reported as unranked, with their vote count,
# rather than being ranked last or hidden.
#
# Five is a starting value chosen for a small campus, not a tuned one. It is the first
# number to revisit once real turnout exists.
MIN_VOTES_TO_RANK = 5


def _vote_rows(campus_id: uuid.UUID, start: date, end: date):
    """Ballots unpivoted into one row per (week, chapter, vote), Touse +1 and Bouse -1.

    A ballot names up to two DIFFERENT houses, so it contributes to two different groups.
    UNION ALL rather than two aggregates joined afterwards: an outer join between "touse
    counts" and "bouse counts" has to cope with a house appearing on only one side, and
    every way of writing that produces NULLs which then have to be coalesced in three
    places. Unpivoting first means the grouping below sees ordinary rows and a house with
    no Touse votes simply has none in the sum.

    ON THE campus_id FILTER, stated accurately because the obvious claim is wrong. It is
    NOT what scopes this to one campus: the composite FK makes a chapter belong to
    exactly one campus, and the leaderboard is spined on that campus's chapters, so a
    ballot from elsewhere names chapters that simply never join. Removing this filter was
    sabotage-tested and the whole suite still passed, which is the honest evidence that
    it is defence in depth rather than the guarantee.

    It stays for two real reasons - it cuts the rows scanned to one campus's ballots
    instead of every campus's, and it is the line that still holds if the composite FK is
    ever dropped. But do not read it as the thing keeping campuses apart; that is the
    foreign key, and THAT is what test_a_ballot_cannot_name_a_chapter_from_another_campus
    covers.
    """
    window = [
        models.HouseBallot.campus_id == campus_id,
        models.HouseBallot.week_start >= start,
        models.HouseBallot.week_start <= end,
    ]
    touse = select(
        models.HouseBallot.week_start.label("week_start"),
        models.HouseBallot.touse_chapter_id.label("chapter_id"),
        literal(1).label("touse"),
        literal(0).label("bouse"),
    ).where(*window)
    bouse = select(
        models.HouseBallot.week_start.label("week_start"),
        models.HouseBallot.bouse_chapter_id.label("chapter_id"),
        literal(0).label("touse"),
        literal(1).label("bouse"),
    ).where(*window, models.HouseBallot.bouse_chapter_id.is_not(None))
    return union_all(touse, bouse).subquery()


def _per_week_totals(campus_id: uuid.UUID, start: date, end: date):
    """Per (week, chapter) vote totals and net score, as a subquery."""
    votes = _vote_rows(campus_id, start, end)
    touse_sum = func.sum(votes.c.touse).cast(Integer)
    bouse_sum = func.sum(votes.c.bouse).cast(Integer)
    return (
        select(
            votes.c.week_start.label("week_start"),
            votes.c.chapter_id.label("chapter_id"),
            touse_sum.label("touse_votes"),
            bouse_sum.label("bouse_votes"),
            (touse_sum - bouse_sum).label("net"),
            (touse_sum + bouse_sum).label("total_votes"),
        )
        .group_by(votes.c.week_start, votes.c.chapter_id)
        .subquery()
    )


@router.put("/campuses/{campus_id}/house-ballot", status_code=200)
async def cast_house_ballot(
    campus_id: uuid.UUID,
    body: HouseBallotCreate,
    voter: models.User = Depends(require_campus_member),
    session: AsyncSession = Depends(get_session),
) -> HouseBallotOut:
    """Cast or replace this week's ballot; campus-verified students only.

    PUT, not POST, because it is idempotent per week by construction: the primary key is
    (campus_id, week_start, voter_id), so a second call from the same student in the same
    week REPLACES their ballot rather than adding one. Changing your mind on Wednesday is
    a normal thing to do; voting twice is not, and the schema is what makes the second
    impossible rather than a check that could be raced.

    ON CONFLICT DO UPDATE, not read-then-write. Two requests arriving together from the
    same student would both pass a check-then-insert and one would fail on the primary
    key; the upsert makes the last writer win, which is exactly the desired behaviour for
    "change my vote".

    A PAST WEEK CANNOT BE EDITED because the week is not addressable: it is computed here
    from the clock and never read from the request.
    """
    if body.bouse_chapter_id is not None and body.bouse_chapter_id == body.touse_chapter_id:
        # The CHECK constraint would also reject this, but a 422 naming the problem beats
        # an IntegrityError surfacing as a 500 to someone who double-tapped a row.
        raise HTTPException(status_code=422, detail="touse_and_bouse_must_differ")

    week_start = current_week_start()
    now = datetime.now(timezone.utc)
    statement = (
        pg_insert(models.HouseBallot)
        .values(
            campus_id=campus_id,
            week_start=week_start,
            voter_id=voter.id,
            touse_chapter_id=body.touse_chapter_id,
            bouse_chapter_id=body.bouse_chapter_id,
            updated_at=now,
        )
        .on_conflict_do_update(
            constraint="pk_house_ballots",
            set_={
                "touse_chapter_id": body.touse_chapter_id,
                "bouse_chapter_id": body.bouse_chapter_id,
                "updated_at": now,
            },
        )
        .returning(models.HouseBallot)
    )
    try:
        result = await session.execute(statement)
        ballot = result.scalar_one()
        await session.commit()
    except IntegrityError:
        # The composite FKs are what refuse a chapter from another campus, and the CHECK
        # is what refuses naming one house twice. Both are the schema doing its job, but
        # an unhandled IntegrityError reaches the client as a 500 - which reads as "the
        # app is broken" for what is really "that is not a house here". Translated rather
        # than pre-validated: a lookup-then-insert would be a second round trip AND a
        # race, and the constraint has to exist regardless.
        await session.rollback()
        raise HTTPException(status_code=422, detail="unknown_house_for_this_campus") from None
    return HouseBallotOut.model_validate(ballot)


@router.get("/campuses/{campus_id}/house-leaderboard")
async def house_leaderboard(
    campus_id: uuid.UUID,
    week_start: date | None = None,
    viewer: models.User = Depends(require_campus_member),
    session: AsyncSession = Depends(get_session),
) -> HouseLeaderboard:
    """One week's ranking, this week by default; campus-verified students only.

    `week_start` is READ-only addressing of a past week, which is safe in a way that a
    writable week would not be: looking at a finished week changes nothing, while writing
    into one would let a voter act on the result. It is not snapped to a Monday - an
    arbitrary date simply matches no rows, which is a true and harmless answer.

    THE SPINE IS THE CHAPTER LIST, NOT THE VOTES. Every chapter on the campus appears,
    either ranked or explicitly unranked with its vote count. A vote-spined query would
    silently omit houses nobody voted for, and "not on the leaderboard" would be
    indistinguishable from "does not exist".
    """
    week = week_start or current_week_start()
    totals = _per_week_totals(campus_id, week, week)

    rows = await session.execute(
        select(
            models.Chapter.id,
            models.Chapter.org_name,
            models.Chapter.chapter_name,
            func.coalesce(totals.c.touse_votes, 0).label("touse_votes"),
            func.coalesce(totals.c.bouse_votes, 0).label("bouse_votes"),
            func.coalesce(totals.c.net, 0).label("net"),
            func.coalesce(totals.c.total_votes, 0).label("total_votes"),
        )
        .select_from(models.Chapter)
        .outerjoin(totals, totals.c.chapter_id == models.Chapter.id)
        .where(models.Chapter.campus_id == campus_id)
        # net desc puts the Touse first; touse_votes breaks a tie toward the house people
        # actively picked rather than the one fewer people disliked; org_name makes the
        # order stable between calls, which matters when a screenshot of a tie circulates.
        .order_by(
            func.coalesce(totals.c.net, 0).desc(),
            func.coalesce(totals.c.touse_votes, 0).desc(),
            models.Chapter.org_name,
        )
    )

    ranked: list[HouseStanding] = []
    unranked: list[UnrankedHouse] = []
    for row in rows:
        if row.total_votes >= MIN_VOTES_TO_RANK:
            ranked.append(
                HouseStanding(
                    chapter_id=row.id,
                    org_name=row.org_name,
                    chapter_name=row.chapter_name,
                    rank=len(ranked) + 1,
                    touse_votes=row.touse_votes,
                    bouse_votes=row.bouse_votes,
                    net=row.net,
                )
            )
        else:
            unranked.append(
                UnrankedHouse(
                    chapter_id=row.id,
                    org_name=row.org_name,
                    chapter_name=row.chapter_name,
                    votes=row.total_votes,
                )
            )

    # Its own statement, not a subquery folded into the one above: this counts BALLOTS
    # and that one counts VOTES, and a ballot naming two houses is two votes. Deriving
    # one from the other is how a denominator quietly becomes wrong.
    ballots_cast = await session.scalar(
        select(func.count())
        .select_from(models.HouseBallot)
        .where(
            models.HouseBallot.campus_id == campus_id,
            models.HouseBallot.week_start == week,
        )
    )

    mine = await session.scalar(
        select(models.HouseBallot).where(
            models.HouseBallot.campus_id == campus_id,
            models.HouseBallot.week_start == week,
            models.HouseBallot.voter_id == viewer.id,
        )
    )

    return HouseLeaderboard(
        campus_id=campus_id,
        week_start=week,
        ballots_cast=ballots_cast or 0,
        min_votes_to_rank=MIN_VOTES_TO_RANK,
        ranked=ranked,
        unranked=unranked,
        my_ballot=HouseBallotOut.model_validate(mine) if mine is not None else None,
    )


@router.get("/campuses/{campus_id}/house-title-race")
async def house_title_race(
    campus_id: uuid.UUID,
    _viewer: models.User = Depends(require_campus_member),
    session: AsyncSession = Depends(get_session),
) -> TermTitleRace:
    """The race for the term title - "Touse of Fall 26"; campus-verified students only.

    DECIDED ON WEEKLY WINS, tiebroken on cumulative net. A season champion rather than a
    points total, so one enormous week cannot buy a semester and a house that shows up
    every week is rewarded for it. Flagged on c175 as my judgement call rather than a
    stated requirement - swapping to cumulative points is an ORDER BY change here.

    ONE STATEMENT for the whole term, using a window function to rank within each week
    and then counting first places. The obvious alternative - fetch each week and rank it
    in Python - is a query per week of the semester, which is the same N+1 shape c82 and
    c156 were built to remove, and it would grow with the term rather than with anything
    a student asked for.

    THE THRESHOLD IS APPLIED PER WEEK, before ranking, exactly as the weekly leaderboard
    applies it. A house that cleared it in three weeks is ranked in three weeks; a week
    where nobody cleared it produces no winner at all rather than crowning noise.
    """
    term = current_term()
    totals = _per_week_totals(campus_id, term.start, term.end)

    weekly = (
        select(
            totals.c.week_start,
            totals.c.chapter_id,
            totals.c.net,
            func.row_number()
            .over(
                partition_by=totals.c.week_start,
                order_by=(totals.c.net.desc(), totals.c.touse_votes.desc(), totals.c.chapter_id),
            )
            .label("placing"),
        )
        .where(totals.c.total_votes >= MIN_VOTES_TO_RANK)
        .subquery()
    )

    rows = await session.execute(
        select(
            models.Chapter.id,
            models.Chapter.org_name,
            models.Chapter.chapter_name,
            func.count().filter(weekly.c.placing == 1).label("weekly_wins"),
            func.coalesce(func.sum(weekly.c.net), 0).label("net"),
        )
        .select_from(models.Chapter)
        .outerjoin(weekly, weekly.c.chapter_id == models.Chapter.id)
        .where(models.Chapter.campus_id == campus_id)
        .group_by(models.Chapter.id, models.Chapter.org_name, models.Chapter.chapter_name)
        .order_by(
            func.count().filter(weekly.c.placing == 1).desc(),
            func.coalesce(func.sum(weekly.c.net), 0).desc(),
            models.Chapter.org_name,
        )
    )
    standings = [
        TermStanding(
            chapter_id=row.id,
            org_name=row.org_name,
            chapter_name=row.chapter_name,
            weekly_wins=row.weekly_wins,
            net=row.net,
        )
        for row in rows
    ]

    weeks_scored = await session.scalar(
        select(func.count(func.distinct(weekly.c.week_start)))
    )

    # No leader until a week has actually been won. Showing the alphabetically-first
    # house with zero wins as the current Touse of the term would be a fabrication that
    # looks exactly like a result.
    leader = standings[0] if standings and standings[0].weekly_wins > 0 else None

    return TermTitleRace(
        campus_id=campus_id,
        term_label=term.label,
        term_start=term.start,
        term_end=term.end,
        weeks_scored=weeks_scored or 0,
        leader=leader,
        standings=standings,
    )
