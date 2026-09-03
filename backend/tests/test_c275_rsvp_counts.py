"""c275 PR1: GET /events/{id}/rsvp-counts - the host's planning number.

Each test was falsified before being kept (mutation named in its docstring).
"""
from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import MakeChapterWith
from tests.test_events import _event_body, _outsider


async def _public_event(client: AsyncClient, setup) -> str:
    created = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body(visibility="public"),
        headers=setup.president.headers,
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


async def test_counts_by_status_and_invited_unanswered(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user
) -> None:
    """going/maybe/cant bucket by answer; unanswered counts invites with NO rsvp row,
    so an invitee who said cant is answered, and an uninvited rsvp still buckets.

    Falsified by: (1) swapping the going/maybe keys in the route's return, and
    (2) dropping the rsvp-is-null filter from the unanswered join (counted all 3
    invites instead of 2)."""
    setup = await make_chapter_with("member")
    event_id = await _public_event(client, setup)

    invited_a = await _outsider(client, make_user, None, verified=False)
    invited_b = await _outsider(client, make_user, None, verified=False)
    invited_c = await _outsider(client, make_user, None, verified=False)
    walk_in = await _outsider(client, make_user, None, verified=False)

    invited = await client.post(
        f"/events/{event_id}/invites",
        json={"user_ids": [invited_a.id, invited_b.id, invited_c.id]},
        headers=setup.president.headers,
    )
    assert invited.status_code == 201, invited.text

    # invited_a answers cant (answered, NOT unanswered); b and c never answer.
    for user, status in [
        (invited_a, "cant"),
        (walk_in, "going"),  # rsvp without an invite still buckets
        (setup.member, "going"),
        (setup.president, "maybe"),
    ]:
        rsvp = await client.put(
            f"/events/{event_id}/rsvps", json={"status": status}, headers=user.headers
        )
        assert rsvp.status_code == 200, rsvp.text

    counts = await client.get(f"/events/{event_id}/rsvp-counts", headers=setup.member.headers)
    assert counts.status_code == 200, counts.text
    assert counts.json() == {
        "going": 2,
        "maybe": 1,
        "cant": 1,
        "invited_unanswered": 2,
    }


async def test_counts_are_all_zero_on_a_fresh_event(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """No invites, no rsvps - four zeros, not a 500.

    Falsified by: indexing by_status["going"] instead of .get with a default
    (fresh event 500s on KeyError)."""
    setup = await make_chapter_with("member")
    event_id = await _public_event(client, setup)
    counts = await client.get(f"/events/{event_id}/rsvp-counts", headers=setup.member.headers)
    assert counts.status_code == 200, counts.text
    assert counts.json() == {"going": 0, "maybe": 0, "cant": 0, "invited_unanswered": 0}


async def test_counts_are_scoped_to_the_one_event(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """A sibling event's rsvps must not bleed into this event's counts, and the counts
    must agree with what /guests actually returns (the c109 rule).

    Falsified by: dropping the event_id filter from the status GROUP BY (sibling's
    going leaked in; 2 != 1)."""
    setup = await make_chapter_with("member")
    event_id = await _public_event(client, setup)
    sibling_id = await _public_event(client, setup)

    for target, user, status in [
        (event_id, setup.member, "going"),
        (sibling_id, setup.president, "going"),
    ]:
        rsvp = await client.put(
            f"/events/{target}/rsvps", json={"status": status}, headers=user.headers
        )
        assert rsvp.status_code == 200, rsvp.text

    counts = (
        await client.get(f"/events/{event_id}/rsvp-counts", headers=setup.member.headers)
    ).json()
    guests = (
        await client.get(f"/events/{event_id}/guests", headers=setup.member.headers)
    ).json()
    assert counts["going"] == 1
    assert counts["going"] + counts["maybe"] + counts["cant"] == len(guests["rsvps"])


async def test_counts_gate_matches_the_guest_list_both_ways(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user
) -> None:
    """Reading a public event does not entitle you to its headcount; being part of it
    does. Same rule, same refusal string as /guests; anonymous is 401.

    Falsified by: removing the _require_guest_list_access call from the route
    (stranger got a 200 headcount)."""
    setup = await make_chapter_with("member")
    event_id = await _public_event(client, setup)

    assert (await client.get(f"/events/{event_id}/rsvp-counts")).status_code == 401

    stranger = await _outsider(client, make_user, None, verified=False)
    refused = await client.get(f"/events/{event_id}/rsvp-counts", headers=stranger.headers)
    assert refused.status_code == 403, refused.text
    assert refused.json() == {"detail": "not_on_the_guest_list"}

    rsvp = await client.put(
        f"/events/{event_id}/rsvps", json={"status": "going"}, headers=stranger.headers
    )
    assert rsvp.status_code == 200, rsvp.text
    allowed = await client.get(f"/events/{event_id}/rsvp-counts", headers=stranger.headers)
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["going"] == 1
