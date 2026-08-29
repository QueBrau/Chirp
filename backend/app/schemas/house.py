"""Touse/Bouse weekly house leaderboard schemas (board card c175)."""

import uuid
from datetime import date, datetime

from app.schemas.base import _Schema


class HouseBallotCreate(_Schema):
    """Body for casting or replacing this week's ballot.

    THERE IS NO WEEK FIELD, on purpose. The week is whatever week the server is in when
    the request lands; accepting one would let a client vote into a finished week.

    bouse_chapter_id is optional: naming a top house without naming a bottom one is a
    complete ballot. Requiring both would manufacture Bouse votes from people who did
    not want to cast one, which is the fastest way to make the bottom of the leaderboard
    meaningless.
    """

    touse_chapter_id: uuid.UUID
    bouse_chapter_id: uuid.UUID | None = None


class HouseBallotOut(_Schema):
    """A ballot as its own voter sees it.

    NO voter_id, ever - not even back to the person who cast it. The column exists so
    abuse can be investigated and is never serialized by any route, the same rule
    Chirp.author_id follows (SPEC 8.3). Serializing it "only for the owner" is how a field
    ends up in a shared component that later renders for someone else.
    """

    campus_id: uuid.UUID
    week_start: date
    touse_chapter_id: uuid.UUID
    bouse_chapter_id: uuid.UUID | None = None
    updated_at: datetime


class HouseStanding(_Schema):
    """One house's placing for one week."""

    chapter_id: uuid.UUID
    org_name: str
    chapter_name: str | None = None
    rank: int
    touse_votes: int
    bouse_votes: int
    net: int


class UnrankedHouse(_Schema):
    """A house that exists but has not cleared the ballot threshold this week.

    Reported separately rather than ranked last. Being last and having three votes are
    completely different statements about a real organisation, and collapsing them is
    how a house gets called the worst on campus by a handful of people.
    """

    chapter_id: uuid.UUID
    org_name: str
    chapter_name: str | None = None
    votes: int


class HouseLeaderboard(_Schema):
    """One week's ranking.

    `ballots_cast` is the sample the whole thing is drawn from and is always reported,
    for the same reason the Secretary dashboard always captions its meeting count: a
    ranking with no denominator invites people to read noise as a result.
    """

    campus_id: uuid.UUID
    week_start: date
    ballots_cast: int
    min_votes_to_rank: int
    ranked: list[HouseStanding]
    unranked: list[UnrankedHouse]
    my_ballot: HouseBallotOut | None = None


class TermStanding(_Schema):
    """One house's season record across the term."""

    chapter_id: uuid.UUID
    org_name: str
    chapter_name: str | None = None
    weekly_wins: int
    net: int


class TermTitleRace(_Schema):
    """The race for "Touse of Fall 26".

    `leader` is whoever currently holds the most weekly wins; it is None until at least
    one week has produced a ranked result. The title is decided on WEEKLY WINS rather
    than cumulative points, so one enormous week cannot buy a semester - flagged on c175
    as a judgement call rather than a stated requirement.
    """

    campus_id: uuid.UUID
    term_label: str
    term_start: date
    term_end: date
    weeks_scored: int
    leader: TermStanding | None = None
    standings: list[TermStanding]
