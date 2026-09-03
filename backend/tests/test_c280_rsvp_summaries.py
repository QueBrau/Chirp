"""c280: events-with-rsvps carries a bounded summary, never a campus of rows.

Each test was falsified before being kept (mutation named in its docstring).
"""
from __future__ import annotations

from httpx import AsyncClient

from app.routers.events import GOING_PREVIEW_LIMIT
from tests.conftest import MakeChapterWith
from tests.test_events import _event_body, _outsider


async def _public_event(client: AsyncClient, setup, title: str = "Open Party") -> str:
    created = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body(title, visibility="public"),
        headers=setup.president.headers,
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


async def _rows(client: AsyncClient, setup, headers=None):
    response = await client.get(
        f"/chapters/{setup.chapter_id}/events-with-rsvps",
        headers=headers or setup.member.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_going_preview_is_bounded_and_counts_carry_the_truth(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user
) -> None:
    """More going than the preview holds: preview stops at GOING_PREVIEW_LIMIT, counts
    report the real number, and non-going answers never appear in the preview. Both
    ways: under the limit, the preview is complete.

    Falsified by: (1) dropping the rank <= GOING_PREVIEW_LIMIT filter (preview came
    back with all 10), and (2) dropping the status == 'going' filter - which only a
    FIRST-created maybe can catch: created last it ranks past the cap and the dropped
    filter is invisible, so the fence-sitter answers before anyone else here."""
    setup = await make_chapter_with("member")
    event_id = await _public_event(client, setup)

    fence_sitter = await _outsider(client, make_user, None, verified=False)
    await client.put(
        f"/events/{event_id}/rsvps", json={"status": "maybe"}, headers=fence_sitter.headers
    )
    guests = [await _outsider(client, make_user, None, verified=False) for _ in range(10)]
    for guest in guests:
        rsvp = await client.put(
            f"/events/{event_id}/rsvps", json={"status": "going"}, headers=guest.headers
        )
        assert rsvp.status_code == 200, rsvp.text

    row = (await _rows(client, setup))[0]
    assert row["counts"]["going"] == 10
    assert row["counts"]["maybe"] == 1
    assert len(row["going_preview"]) == GOING_PREVIEW_LIMIT
    preview_ids = [r["user_id"] for r in row["going_preview"]]
    assert fence_sitter.id not in preview_ids
    assert all(r["status"] == "going" for r in row["going_preview"])

    # Both ways: an event under the limit gets its complete going list.
    small_id = await _public_event(client, setup, "Small One")
    for guest in guests[:3]:
        await client.put(
            f"/events/{small_id}/rsvps", json={"status": "going"}, headers=guest.headers
        )
    small = next(r for r in await _rows(client, setup) if r["event"]["id"] == small_id)
    assert small["counts"]["going"] == 3
    assert len(small["going_preview"]) == 3


async def test_going_preview_is_earliest_responders_first(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user
) -> None:
    """The faces shown are the FIRST people who said going, in the order they said it —
    ordered before limited, per the house rule.

    Falsified by: flipping the window's order_by to descending created_at (the
    preview became the LAST responders)."""
    setup = await make_chapter_with("member")
    event_id = await _public_event(client, setup)

    guests = [await _outsider(client, make_user, None, verified=False) for _ in range(10)]
    for guest in guests:
        rsvp = await client.put(
            f"/events/{event_id}/rsvps", json={"status": "going"}, headers=guest.headers
        )
        assert rsvp.status_code == 200, rsvp.text

    row = (await _rows(client, setup))[0]
    expected = [guest.id for guest in guests[:GOING_PREVIEW_LIMIT]]
    assert [r["user_id"] for r in row["going_preview"]] == expected


async def test_my_rsvp_status_is_the_callers_own_answer(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Two members with different answers each see their OWN, and no answer is null.

    Falsified by: dropping the user_id filter from the caller's-own query (the
    member saw the president's answer)."""
    setup = await make_chapter_with("member")
    event_id = await _public_event(client, setup)
    other_id = await _public_event(client, setup, "Second One")

    await client.put(
        f"/events/{event_id}/rsvps", json={"status": "maybe"}, headers=setup.member.headers
    )
    await client.put(
        f"/events/{event_id}/rsvps", json={"status": "going"}, headers=setup.president.headers
    )

    member_row = next(
        r for r in await _rows(client, setup) if r["event"]["id"] == event_id
    )
    assert member_row["my_rsvp_status"] == "maybe"
    president_rows = await _rows(client, setup, headers=setup.president.headers)
    assert next(r for r in president_rows if r["event"]["id"] == event_id)[
        "my_rsvp_status"
    ] == "going"
    assert next(r for r in president_rows if r["event"]["id"] == other_id)[
        "my_rsvp_status"
    ] is None


async def test_invited_unanswered_is_scoped_per_event(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user
) -> None:
    """Each event counts only its own silent invitees; answering one event's invite
    does not answer another's.

    Falsified by: pairing the unanswered join on user alone (dropping the event half
    of the ON clause), so invitee_1's answer on A also 'answered' B and B wrongly
    reported zero silent invitees."""
    setup = await make_chapter_with("member")
    event_a = await _public_event(client, setup, "A")
    event_b = await _public_event(client, setup, "B")

    invitee_1 = await _outsider(client, make_user, None, verified=False)
    invitee_2 = await _outsider(client, make_user, None, verified=False)
    for event_id, user_ids in [
        (event_a, [invitee_1.id, invitee_2.id]),
        (event_b, [invitee_1.id]),
    ]:
        invited = await client.post(
            f"/events/{event_id}/invites",
            json={"user_ids": user_ids},
            headers=setup.president.headers,
        )
        assert invited.status_code == 201, invited.text

    # invitee_1 answers A only: A has one silent invitee left, B still has one.
    await client.put(
        f"/events/{event_a}/rsvps", json={"status": "cant"}, headers=invitee_1.headers
    )

    rows = {r["event"]["id"]: r for r in await _rows(client, setup)}
    assert rows[event_a]["counts"]["invited_unanswered"] == 1
    assert rows[event_b]["counts"]["invited_unanswered"] == 1
    assert rows[event_a]["counts"]["cant"] == 1
