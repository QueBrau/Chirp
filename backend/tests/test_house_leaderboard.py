"""Touse/Bouse weekly house leaderboard (board card c175).

The feature ranks REAL organisations publicly, bottom included, so most of these tests
exist to hold lines that a plausible rewrite would cross quietly:

  * a ballot cannot name a chapter from another campus (composite FK, not validation)
  * one ballot per student per week is the primary key, so voting twice REPLACES
  * a house under the vote threshold is unranked, never ranked last - "came last" and
    "three people voted" are different statements about a real org
  * voter_id is never serialized by anything
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from httpx import AsyncClient
from sqlalchemy import text

from tests.conftest import ApiUser, MakeCampus, MakeUser, set_campus
from app.core.windows import current_week_start


async def _campus_student(
    make_user: MakeUser, campus_id: str, name: str = "Student", *, verified: bool = True
) -> ApiUser:
    user = await make_user(name)
    await set_campus(user.id, campus_id, verified=verified)
    return user


async def _make_chapter(campus_id: str, org_name: str) -> str:
    """Insert a chapter directly - POST /chapters is platform-admin-only and this suite
    is about ballots, not about chapter creation."""
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        result = await session.execute(
            text(
                "INSERT INTO chapters (campus_id, org_name) VALUES (:c, :n) RETURNING id"
            ),
            {"c": campus_id, "n": org_name},
        )
        chapter_id = str(result.scalar_one())
        await session.commit()
    return chapter_id


async def _cast(
    client: AsyncClient, campus_id: str, voter: ApiUser, touse: str, bouse: str | None = None
):
    body: dict = {"touse_chapter_id": touse}
    if bouse is not None:
        body["bouse_chapter_id"] = bouse
    return await client.put(
        f"/campuses/{campus_id}/house-ballot", json=body, headers=voter.headers
    )


async def _board(client: AsyncClient, campus_id: str, viewer: ApiUser) -> dict:
    response = await client.get(
        f"/campuses/{campus_id}/house-leaderboard", headers=viewer.headers
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _vote_block(
    client: AsyncClient, make_user: MakeUser, campus_id: str, touse: str,
    bouse: str | None, count: int,
) -> None:
    """`count` distinct students all casting the same ballot."""
    for i in range(count):
        voter = await _campus_student(make_user, campus_id, f"Voter {uuid.uuid4().hex[:6]}")
        response = await _cast(client, campus_id, voter, touse, bouse)
        assert response.status_code == 200, response.text


# ---- the gate ----


async def test_an_unverified_student_cannot_vote(
    client: AsyncClient, make_user: MakeUser, make_campus: MakeCampus
) -> None:
    """The .edu check is the only thing between a campus contest and the whole internet."""
    campus = await make_campus()
    house = await _make_chapter(campus, "Sigma Chi")
    voter = await _campus_student(make_user, campus, "Unverified", verified=False)

    response = await _cast(client, campus, voter, house)

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "campus_unverified"


async def test_a_student_from_another_campus_cannot_read_the_board(
    client: AsyncClient, make_user: MakeUser, make_campus: MakeCampus
) -> None:
    """Reads are gated too. A ranking that names real orgs is not "public data" that
    an off-campus account should be able to watch."""
    home = await make_campus()
    away = await make_campus()
    outsider = await _campus_student(make_user, away, "Outsider")

    response = await client.get(
        f"/campuses/{home}/house-leaderboard", headers=outsider.headers
    )

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "not_your_campus"


# ---- one ballot per week ----


async def test_voting_twice_in_a_week_replaces_rather_than_adds(
    client: AsyncClient, make_user: MakeUser, make_campus: MakeCampus
) -> None:
    """The primary key is (campus, week, voter), so this is structural, not a check."""
    campus = await make_campus()
    first = await _make_chapter(campus, "Sigma Chi")
    second = await _make_chapter(campus, "Kappa Sigma")
    voter = await _campus_student(make_user, campus, "Fickle Voter")

    assert (await _cast(client, campus, voter, first)).status_code == 200
    assert (await _cast(client, campus, voter, second)).status_code == 200

    payload = await _board(client, campus, voter)

    assert payload["ballots_cast"] == 1
    assert payload["my_ballot"]["touse_chapter_id"] == second


async def test_a_ballot_never_reports_who_cast_it(
    client: AsyncClient, make_user: MakeUser, make_campus: MakeCampus
) -> None:
    """voter_id exists for abuse investigation and is serialized by nothing - the same
    rule Yak.author_id follows. Not even back to its own author, because a field that
    is "safe for the owner" ends up in a shared component that renders for someone else."""
    campus = await make_campus()
    house = await _make_chapter(campus, "Sigma Chi")
    voter = await _campus_student(make_user, campus, "Private Voter")

    cast = await _cast(client, campus, voter, house)
    payload = await _board(client, campus, voter)

    assert "voter_id" not in cast.json()
    assert "voter_id" not in payload["my_ballot"]


async def test_last_weeks_ballots_do_not_count_this_week(
    client: AsyncClient, make_user: MakeUser, make_campus: MakeCampus
) -> None:
    """A weekly contest that quietly accumulates is not weekly."""
    campus = await make_campus()
    house = await _make_chapter(campus, "Sigma Chi")
    voter = await _campus_student(make_user, campus, "Last Week Voter")
    assert (await _cast(client, campus, voter, house)).status_code == 200

    # Move the ballot back a week. The route cannot address a past week at all, which is
    # the point - it has to be done in the database.
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        await session.execute(
            text("UPDATE house_ballots SET week_start = week_start - INTERVAL '7 days'"),
        )
        await session.commit()

    payload = await _board(client, campus, voter)

    assert payload["ballots_cast"] == 0
    assert payload["my_ballot"] is None


# ---- what a ballot may name ----


async def test_a_ballot_cannot_name_a_chapter_from_another_campus(
    client: AsyncClient, make_user: MakeUser, make_campus: MakeCampus
) -> None:
    """HELD BY A COMPOSITE FOREIGN KEY, not by a validation somebody can forget.

    This repo has shipped two cross-campus leaks already (c82's dual-chapter attendance
    join, SECURITY-REVIEW finding 1 on moderation) and both passed every single-campus
    test that existed. (chapter_id, campus_id) -> chapters(id, campus_id) makes the
    mistake unrepresentable rather than merely untested.
    """
    home = await make_campus()
    away = await make_campus()
    foreign_house = await _make_chapter(away, "Not Our Problem")
    voter = await _campus_student(make_user, home, "Home Voter")

    response = await _cast(client, home, voter, foreign_house)

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "unknown_house_for_this_campus"


async def test_the_same_house_cannot_be_both_touse_and_bouse(
    client: AsyncClient, make_user: MakeUser, make_campus: MakeCampus
) -> None:
    campus = await make_campus()
    house = await _make_chapter(campus, "Sigma Chi")
    voter = await _campus_student(make_user, campus, "Confused Voter")

    response = await _cast(client, campus, voter, house, house)

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "touse_and_bouse_must_differ"


async def test_a_touse_only_ballot_is_complete(
    client: AsyncClient, make_user: MakeUser, make_campus: MakeCampus
) -> None:
    """Naming a bottom house is optional. Requiring it would manufacture Bouse votes
    from people who did not want to cast one, which is how the bottom of a leaderboard
    stops meaning anything."""
    campus = await make_campus()
    house = await _make_chapter(campus, "Sigma Chi")
    voter = await _campus_student(make_user, campus, "Positive Voter")

    response = await _cast(client, campus, voter, house)

    assert response.status_code == 200, response.text
    assert response.json()["bouse_chapter_id"] is None


# ---- the ranking ----


async def test_a_house_under_the_threshold_is_unranked_not_last(
    client: AsyncClient, make_user: MakeUser, make_campus: MakeCampus
) -> None:
    """THE LINE THIS FEATURE MOST NEEDS.

    Being last and having three votes are different statements about a real
    organisation, and the Bouse is public. A rewrite that simply sorts everything and
    calls the tail the Bouse would pass every other test here.
    """
    campus = await make_campus()
    popular = await _make_chapter(campus, "Sigma Chi")
    barely = await _make_chapter(campus, "Tiny Sample")
    viewer = await _campus_student(make_user, campus, "Viewer")

    await _vote_block(client, make_user, campus, popular, None, 5)
    await _vote_block(client, make_user, campus, barely, None, 2)

    payload = await _board(client, campus, viewer)

    ranked_ids = [row["chapter_id"] for row in payload["ranked"]]
    unranked = {row["chapter_id"]: row for row in payload["unranked"]}
    assert popular in ranked_ids
    assert barely not in ranked_ids
    assert unranked[barely]["votes"] == 2


async def test_a_house_nobody_voted_for_still_appears(
    client: AsyncClient, make_user: MakeUser, make_campus: MakeCampus
) -> None:
    """The spine is the chapter list, not the votes. A vote-spined query would omit
    houses nobody mentioned, making "not on the leaderboard" indistinguishable from
    "does not exist"."""
    campus = await make_campus()
    voted_for = await _make_chapter(campus, "Sigma Chi")
    ignored = await _make_chapter(campus, "Nobody Mentioned Us")
    viewer = await _campus_student(make_user, campus, "Viewer")
    await _vote_block(client, make_user, campus, voted_for, None, 5)

    payload = await _board(client, campus, viewer)

    everyone = [r["chapter_id"] for r in payload["ranked"]] + [
        r["chapter_id"] for r in payload["unranked"]
    ]
    assert ignored in everyone


async def test_bouse_votes_pull_a_house_below_one_with_fewer_touse_votes(
    client: AsyncClient, make_user: MakeUser, make_campus: MakeCampus
) -> None:
    """Net, not raw popularity: the ranking has to be able to express dislike, or the
    Bouse half of the product does nothing."""
    campus = await make_campus()
    loved = await _make_chapter(campus, "Sigma Chi")
    divisive = await _make_chapter(campus, "Kappa Sigma")
    viewer = await _campus_student(make_user, campus, "Viewer")

    # divisive gets more Touse votes but a pile of Bouse votes with it
    await _vote_block(client, make_user, campus, divisive, None, 6)
    await _vote_block(client, make_user, campus, loved, divisive, 5)

    payload = await _board(client, campus, viewer)
    order = [row["chapter_id"] for row in payload["ranked"]]

    assert order.index(loved) < order.index(divisive)
    divisive_row = next(r for r in payload["ranked"] if r["chapter_id"] == divisive)
    assert divisive_row["touse_votes"] == 6
    assert divisive_row["bouse_votes"] == 5
    assert divisive_row["net"] == 1


async def test_ranks_are_dense_and_start_at_one(
    client: AsyncClient, make_user: MakeUser, make_campus: MakeCampus
) -> None:
    campus = await make_campus()
    first = await _make_chapter(campus, "Alpha House")
    second = await _make_chapter(campus, "Beta House")
    viewer = await _campus_student(make_user, campus, "Viewer")
    await _vote_block(client, make_user, campus, first, None, 7)
    await _vote_block(client, make_user, campus, second, None, 5)

    payload = await _board(client, campus, viewer)

    assert [row["rank"] for row in payload["ranked"]] == [1, 2]
    assert payload["ranked"][0]["chapter_id"] == first


async def test_ballots_cast_counts_ballots_not_votes(
    client: AsyncClient, make_user: MakeUser, make_campus: MakeCampus
) -> None:
    """One ballot naming two houses is two votes and one ballot. Deriving the
    denominator from the vote count is how it quietly becomes wrong."""
    campus = await make_campus()
    top = await _make_chapter(campus, "Sigma Chi")
    bottom = await _make_chapter(campus, "Kappa Sigma")
    viewer = await _campus_student(make_user, campus, "Viewer")
    await _vote_block(client, make_user, campus, top, bottom, 3)

    payload = await _board(client, campus, viewer)

    assert payload["ballots_cast"] == 3


# ---- the term title ----


async def test_no_leader_until_a_week_has_actually_been_won(
    client: AsyncClient, make_user: MakeUser, make_campus: MakeCampus
) -> None:
    """Showing the alphabetically-first house with zero wins as the current Touse of
    the term is a fabrication that looks exactly like a result."""
    campus = await make_campus()
    await _make_chapter(campus, "Sigma Chi")
    viewer = await _campus_student(make_user, campus, "Viewer")

    response = await client.get(
        f"/campuses/{campus}/house-title-race", headers=viewer.headers
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["leader"] is None
    assert payload["weeks_scored"] == 0
    assert payload["term_label"].startswith(("Fall", "Spring"))


async def test_the_title_leader_is_the_house_with_the_most_weekly_wins(
    client: AsyncClient, make_user: MakeUser, make_campus: MakeCampus
) -> None:
    """Weekly wins, not cumulative points, so one enormous week cannot buy a semester.

    THE SCENARIO HAS TO DISCRIMINATE, and the first version of this test did not: it
    tied the two houses on wins, so cumulative net was the tiebreak under BOTH rules and
    the test passed against an implementation ordered purely by points. Caught by
    sabotaging the ORDER BY and watching it pass. Now built so the two rules give
    OPPOSITE answers - spiky has a far higher season net, steady has more wins - and the
    assertion is only satisfiable by the wins rule.
    """
    campus = await make_campus()
    steady = await _make_chapter(campus, "Steady House")
    spiky = await _make_chapter(campus, "Spiky House")
    viewer = await _campus_student(make_user, campus, "Viewer")
    from app.db import get_session_factory

    async def _age_all_by_a_week() -> None:
        """Shift EVERY ballot back one week, so each cast block lands in its own week.

        Deliberately not "shift only the current week": that version leaves already-aged
        rows where they are, so the next block shifts down on top of them and two weeks
        collapse into one. The collapse is invisible in the standings - the totals still
        look plausible - and it silently made weeks_scored 2 instead of 3.
        """
        async with get_session_factory()() as session:
            await session.execute(
                text("UPDATE house_ballots SET week_start = week_start - INTERVAL '7 days'")
            )
            await session.commit()

    # Week one: spiky wins enormously (net +30 against +5).
    await _vote_block(client, make_user, campus, spiky, None, 30)
    await _vote_block(client, make_user, campus, steady, None, 5)
    await _age_all_by_a_week()

    # Week two: steady wins narrowly (+7 against +5).
    await _vote_block(client, make_user, campus, steady, None, 7)
    await _vote_block(client, make_user, campus, spiky, None, 5)
    await _age_all_by_a_week()

    # Week three: steady wins narrowly again.
    await _vote_block(client, make_user, campus, steady, None, 7)
    await _vote_block(client, make_user, campus, spiky, None, 5)

    response = await client.get(
        f"/campuses/{campus}/house-title-race", headers=viewer.headers
    )
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["weeks_scored"] == 3
    by_id = {row["chapter_id"]: row for row in payload["standings"]}
    assert by_id[steady]["weekly_wins"] == 2
    assert by_id[spiky]["weekly_wins"] == 1
    # Spiky's season net is far higher, so ordering by points would crown spiky here.
    assert by_id[spiky]["net"] > by_id[steady]["net"]
    assert payload["leader"]["chapter_id"] == steady


async def test_a_week_where_nobody_cleared_the_threshold_crowns_nobody(
    client: AsyncClient, make_user: MakeUser, make_campus: MakeCampus
) -> None:
    campus = await make_campus()
    house = await _make_chapter(campus, "Sigma Chi")
    viewer = await _campus_student(make_user, campus, "Viewer")
    await _vote_block(client, make_user, campus, house, None, 2)

    response = await client.get(
        f"/campuses/{campus}/house-title-race", headers=viewer.headers
    )

    payload = response.json()
    assert payload["weeks_scored"] == 0
    assert payload["leader"] is None
