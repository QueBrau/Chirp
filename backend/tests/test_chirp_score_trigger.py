"""chirps.score is maintained by the DATABASE, not by the vote route (board card c206).

WHAT CHANGED AND WHY THESE TESTS EXIST. vote_chirp used to recompute the score with a
full `SELECT SUM(value)` over the chirp's votes on every single vote - correct, but
O(n) per vote, O(n^2) over the chirp's life, with the chirps row lock held for the whole
scan. Migration 0025 replaces that with an AFTER trigger on chirp_votes that applies a
delta, so the cost stops growing with vote count.

WHY NOT JUST LEAN ON test_chirps.py. Its vote tests go through the API, so they pass
both before AND after this change - they pin the BEHAVIOUR, which is exactly what must
not change, and they are the reason this card can claim it did not. What they cannot
show is WHERE the score comes from: an app-side recompute and a trigger are
indistinguishable from outside the route.

So every test here writes to chirp_votes DIRECTLY, bypassing the route entirely. That
is the whole point. Against the pre-0025 schema nothing maintains the score when the
route is not involved, so all four fail; after 0025 the counter is driven by the source
of truth and they pass. That difference IS the property the card bought - drift stops
being a discipline every present and future write path has to remember, and becomes
structurally impossible.
"""
from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy import text

from tests.conftest import ApiUser, MakeCampus, set_campus


async def _make_campus_user(
    client: AsyncClient, campus_id: str, display_name: str = "Chirp Voter"
) -> ApiUser:
    """Same shape as test_chirps.py's helper; duplicated for the reason recorded there."""
    uid = f"uid-{uuid.uuid4().hex}"
    headers = {"X-Debug-Firebase-Uid": uid}
    email = f"{uid}@example.edu"
    response = await client.post(
        "/auth/bootstrap",
        json={"email": email, "display_name": display_name, "account_type": "non_greek"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    user = ApiUser(id=response.json()["id"], firebase_uid=uid, email=email, headers=headers)
    await set_campus(user.id, campus_id)
    return user


async def _score(chirp_id: str) -> int:
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        result = await session.execute(
            text("SELECT score FROM chirps WHERE id = :id"), {"id": chirp_id}
        )
        return int(result.scalar_one())


async def _write_vote(chirp_id: str, user_id: str, value: int) -> None:
    """INSERT a vote straight into the table, with no route involved."""
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        await session.execute(
            text(
                "INSERT INTO chirp_votes (chirp_id, user_id, value) "
                "VALUES (:chirp, :user, :value)"
            ),
            {"chirp": chirp_id, "user": user_id, "value": value},
        )
        await session.commit()


async def _rewrite_vote(chirp_id: str, user_id: str, value: int) -> None:
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        await session.execute(
            text(
                "UPDATE chirp_votes SET value = :value "
                "WHERE chirp_id = :chirp AND user_id = :user"
            ),
            {"chirp": chirp_id, "user": user_id, "value": value},
        )
        await session.commit()


async def _erase_vote(chirp_id: str, user_id: str) -> None:
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        await session.execute(
            text("DELETE FROM chirp_votes WHERE chirp_id = :chirp AND user_id = :user"),
            {"chirp": chirp_id, "user": user_id},
        )
        await session.commit()


async def _post_chirp(client: AsyncClient, campus_id: str, author: ApiUser) -> str:
    response = await client.post(
        f"/campuses/{campus_id}/chirps", json={"body": "score me"}, headers=author.headers
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def test_a_vote_inserted_directly_moves_the_score(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    """The INSERT branch, and the headline claim of c206.

    No route touches the score here - the vote row is written straight to the table. If
    the score still moves, the database is maintaining it.
    """
    campus_id = await make_campus()
    author = await _make_campus_user(client, campus_id, "Author")
    voter = await _make_campus_user(client, campus_id, "Direct Voter")
    chirp_id = await _post_chirp(client, campus_id, author)

    assert await _score(chirp_id) == 0
    await _write_vote(chirp_id, voter.id, 1)
    assert await _score(chirp_id) == 1, (
        "a vote written straight to chirp_votes did not move chirps.score — the counter "
        "is not being maintained by the database (c206 trigger missing?)"
    )


async def test_flipping_a_vote_directly_applies_a_delta_of_two(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    """The UPDATE branch, on the arithmetic most likely to be got wrong.

    Going from -1 to +1 is a delta of TWO, not one. A trigger that naively added
    NEW.value would land on 0 here and look plausible while being wrong.
    """
    campus_id = await make_campus()
    author = await _make_campus_user(client, campus_id, "Author")
    voter = await _make_campus_user(client, campus_id, "Mind Changer")
    chirp_id = await _post_chirp(client, campus_id, author)

    await _write_vote(chirp_id, voter.id, -1)
    assert await _score(chirp_id) == -1

    await _rewrite_vote(chirp_id, voter.id, 1)
    assert await _score(chirp_id) == 1, "flipping -1 to +1 must move the score by 2"


async def test_deleting_a_vote_directly_takes_the_score_back(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    """The DELETE branch.

    No route deletes votes today - chirp removal is soft. This covers the path a
    moderation tool or a cleanup script would take, which is precisely the kind of
    future writer an application-side delta would not know about.
    """
    campus_id = await make_campus()
    author = await _make_campus_user(client, campus_id, "Author")
    voter = await _make_campus_user(client, campus_id, "Retractor")
    chirp_id = await _post_chirp(client, campus_id, author)

    await _write_vote(chirp_id, voter.id, 1)
    assert await _score(chirp_id) == 1

    await _erase_vote(chirp_id, voter.id)
    assert await _score(chirp_id) == 0, "removing a vote row must take its value back out"


async def test_many_voters_accumulate_without_the_route(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    """Several voters, mixed directions, none of them going through the API.

    Guards the delta arithmetic across repeated application rather than a single step -
    a trigger that assigned instead of accumulating would pass the single-vote test and
    fail here.
    """
    campus_id = await make_campus()
    author = await _make_campus_user(client, campus_id, "Author")
    chirp_id = await _post_chirp(client, campus_id, author)

    values = [1, 1, 1, -1, 1, -1, 1]
    for index, value in enumerate(values):
        voter = await _make_campus_user(client, campus_id, f"Voter {index}")
        await _write_vote(chirp_id, voter.id, value)

    assert await _score(chirp_id) == sum(values)


async def test_the_api_vote_path_still_produces_the_same_score(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    """The behaviour that must NOT change, asserted from this file too.

    test_chirps.py already covers the route's own contract, but this card removed a
    write from that route — so it is worth one assertion here that the removal did not
    quietly cost a point, and specifically that the score is not DOUBLE-counted by both
    a leftover recompute and the trigger.
    """
    campus_id = await make_campus()
    author = await _make_campus_user(client, campus_id, "Author")
    voter = await _make_campus_user(client, campus_id, "Api Voter")
    chirp_id = await _post_chirp(client, campus_id, author)

    response = await client.put(
        f"/chirps/{chirp_id}/vote", json={"value": 1}, headers=voter.headers
    )
    assert response.status_code == 200, response.text
    assert await _score(chirp_id) == 1, "one API vote must be worth exactly one point"

    # Same caller voting again is an upsert, not a second vote.
    response = await client.put(
        f"/chirps/{chirp_id}/vote", json={"value": 1}, headers=voter.headers
    )
    assert response.status_code == 200, response.text
    assert await _score(chirp_id) == 1, "a repeated identical vote must not double-count"
