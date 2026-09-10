"""Daily-rotating chirp pseudonyms (board card c390).

The unit tests below pin the two properties that decide whether this feature is
safe, both of which have a plausible-looking wrong implementation:

  * keyed on the chirp's CREATION day, not on today - otherwise every old chirp
    re-labels itself each morning and an author's whole history re-links daily,
    which is strictly worse than the stable pseudonym braul rejected;
  * keyed on a stored random SEED, not on the user id - otherwise any chapter
    member, who can already list their chapter's roster, can compute every
    chapter-mate's label for a day and read the board's authorship directly.

The API tests then check the wire actually carries a label and still carries no
author identifier.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy import text

from app.services.pseudonym_service import daily_pseudonym
from tests.conftest import ApiUser, MakeCampus, set_campus

_SEED_A = "seed-aaaaaaaaaaaaaaaa"
_SEED_B = "seed-bbbbbbbbbbbbbbbb"
_CAMPUS = uuid.UUID("11111111-1111-4111-8111-111111111111")
_OTHER_CAMPUS = uuid.UUID("22222222-2222-4222-8222-222222222222")
_MORNING = datetime(2026, 9, 8, 9, 0, tzinfo=timezone.utc)


def test_same_author_same_day_gets_one_label() -> None:
    """The whole point of the feature: a day's chirps read as one person."""
    evening = _MORNING + timedelta(hours=10)
    assert daily_pseudonym(_SEED_A, _CAMPUS, _MORNING) == daily_pseudonym(_SEED_A, _CAMPUS, evening)


def test_label_rotates_the_next_day() -> None:
    """The cap on linkage. Without this the feature is the stable pseudonym."""
    tomorrow = _MORNING + timedelta(days=1)
    assert daily_pseudonym(_SEED_A, _CAMPUS, _MORNING) != daily_pseudonym(_SEED_A, _CAMPUS, tomorrow)


def test_old_chirps_keep_their_own_days_label() -> None:
    """Keyed on created_at, NOT on now().

    The failing implementation passes both tests above and still re-links an
    author's entire history every morning, so this asserts the property directly:
    a label computed for an old timestamp must not drift when 'today' moves.
    """
    old = _MORNING - timedelta(days=30)
    first = daily_pseudonym(_SEED_A, _CAMPUS, old)
    # Recomputing the SAME chirp much later must be unchanged - there is no
    # now()-shaped input for a later 'today' to leak through.
    assert daily_pseudonym(_SEED_A, _CAMPUS, old) == first
    assert first != daily_pseudonym(_SEED_A, _CAMPUS, _MORNING)


def test_different_authors_differ_on_the_same_day() -> None:
    assert daily_pseudonym(_SEED_A, _CAMPUS, _MORNING) != daily_pseudonym(_SEED_B, _CAMPUS, _MORNING)


def test_label_is_scoped_per_campus() -> None:
    """One person on two campuses must not be correlatable across them."""
    assert daily_pseudonym(_SEED_A, _CAMPUS, _MORNING) != daily_pseudonym(_SEED_A, _OTHER_CAMPUS, _MORNING)


def test_label_does_not_contain_the_seed() -> None:
    """One-way by construction; assert it rather than trusting the HMAC by eye."""
    label = daily_pseudonym(_SEED_A, _CAMPUS, _MORNING)
    assert _SEED_A not in label
    assert str(_CAMPUS) not in label


def test_naive_timestamps_do_not_crash_or_drift() -> None:
    """Everything from the database is tz-aware; a naive value is assumed UTC."""
    naive = _MORNING.replace(tzinfo=None)
    assert daily_pseudonym(_SEED_A, _CAMPUS, naive) == daily_pseudonym(_SEED_A, _CAMPUS, _MORNING)


async def _campus_user(client: AsyncClient, campus_id: str, name: str = "Chirp User") -> ApiUser:
    uid = f"uid-{uuid.uuid4().hex}"
    headers = {"X-Debug-Firebase-Uid": uid}
    email = f"{uid}@example.edu"
    response = await client.post(
        "/auth/bootstrap",
        json={"email": email, "display_name": name, "account_type": "non_greek"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    user = ApiUser(id=response.json()["id"], firebase_uid=uid, email=email, headers=headers)
    await set_campus(user.id, campus_id)
    return user


async def test_every_user_gets_a_distinct_seed(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    """Migration 0038 fills per ROW. One shared default would collapse the whole
    board onto a single name and would look like the feature working."""
    campus_id = await make_campus()
    first = await _campus_user(client, campus_id)
    second = await _campus_user(client, campus_id)
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        rows = await session.execute(
            text("SELECT pseudonym_seed FROM users WHERE id = ANY(:ids)"),
            {"ids": [uuid.UUID(first.id), uuid.UUID(second.id)]},
        )
        seeds = [r[0] for r in rows.all()]
    assert len(seeds) == 2
    assert all(s for s in seeds), "seed must never be null"
    assert seeds[0] != seeds[1]


async def test_feed_carries_a_label_and_still_no_author_id(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    campus_id = await make_campus()
    author = await _campus_user(client, campus_id)
    reader = await _campus_user(client, campus_id)

    created = await client.post(
        f"/campuses/{campus_id}/chirps", json={"body": "hello"}, headers=author.headers
    )
    assert created.status_code == 201, created.text
    assert created.json()["author_label"], "create response must carry the label too"

    listed = await client.get(f"/campuses/{campus_id}/chirps", headers=reader.headers)
    assert listed.status_code == 200, listed.text
    row = listed.json()[0]
    assert row["author_label"]
    # The exception c390 opened is the LABEL and nothing else: no id, no seed.
    for leaked in ("author_id", "pseudonym_seed", "user_id", "firebase_uid", "email"):
        assert leaked not in row, f"{leaked} must never reach a peer (SPEC 8.3)"
    assert author.id not in listed.text


async def test_two_chirps_by_one_author_share_a_label_over_the_api(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    """End to end, this is what braul asked for."""
    campus_id = await make_campus()
    author = await _campus_user(client, campus_id)
    other = await _campus_user(client, campus_id)
    for body in ("first", "second"):
        r = await client.post(
            f"/campuses/{campus_id}/chirps", json={"body": body}, headers=author.headers
        )
        assert r.status_code == 201, r.text
    r = await client.post(
        f"/campuses/{campus_id}/chirps", json={"body": "third"}, headers=other.headers
    )
    assert r.status_code == 201, r.text

    listed = await client.get(f"/campuses/{campus_id}/chirps", headers=author.headers)
    by_body = {row["body"]: row["author_label"] for row in listed.json()}
    assert by_body["first"] == by_body["second"]
    assert by_body["third"] != by_body["first"]


async def test_every_viewer_sees_the_same_label_for_one_chirp(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    """The label must be a property of the CHIRP, not of who is looking at it.

    The wrong implementation this pins is not far-fetched and would pass every
    other test in this file: salt the pseudonym with the requesting user as well
    as the author, on the reasonable-sounding grounds that it leaks less. It does
    not leak less, and it breaks the feature outright - two people cannot talk
    about "what Quiet-Magnolia-07 said" if the board reads differently for each of
    them, and the label stops being the shared handle braul asked for.

    Three vantage points, because two would not separate the cases: the author's
    own create response, and two DIFFERENT readers. A per-viewer salt could still
    agree between one reader and the author by coincidence of ordering; it cannot
    agree across all three.
    """
    campus_id = await make_campus()
    author = await _campus_user(client, campus_id, name="Author")
    reader_b = await _campus_user(client, campus_id, name="Reader B")
    reader_c = await _campus_user(client, campus_id, name="Reader C")

    created = await client.post(
        f"/campuses/{campus_id}/chirps", json={"body": "one chirp, three viewers"}, headers=author.headers
    )
    assert created.status_code == 201, created.text
    chirp_id = created.json()["id"]
    author_view = created.json()["author_label"]
    assert author_view

    seen = {"author (create response)": author_view}
    for who, reader in (("reader B", reader_b), ("reader C", reader_c)):
        listed = await client.get(f"/campuses/{campus_id}/chirps", headers=reader.headers)
        assert listed.status_code == 200, listed.text
        row = next(r for r in listed.json() if r["id"] == chirp_id)
        seen[who] = row["author_label"]

    assert len(set(seen.values())) == 1, f"one chirp must carry one label for everyone: {seen}"
