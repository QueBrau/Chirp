"""c275: the split, paginated guest-list routes, and the wrapper's deliberate absence.

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


async def _walk(client: AsyncClient, url: str, headers, *, limit: int, user_key: str):
    """Page forward until a short page; return every row in arrival order."""
    rows: list[dict] = []
    params: dict = {"limit": limit}
    while True:
        page = await client.get(url, params=params, headers=headers)
        assert page.status_code == 200, page.text
        batch = page.json()
        rows.extend(batch)
        if len(batch) < limit:
            return rows
        params = {
            "limit": limit,
            "after": batch[-1]["created_at"],
            "after_user_id": batch[-1][user_key],
        }


async def test_rsvps_page_forward_without_loss_or_duplicates(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user
) -> None:
    """Seven answers walked at limit 3 come back complete, unduplicated, and in the
    order they were given - the both-ways proof that the cursor neither drops nor
    re-serves rows.

    Falsified by: flipping the compound comparison from > to >= (every page
    re-served its predecessor's last row; 9 rows for 7 answers)."""
    setup = await make_chapter_with("member")
    event_id = await _public_event(client, setup)

    guests = [await _outsider(client, make_user, None, verified=False) for _ in range(7)]
    for guest in guests:
        rsvp = await client.put(
            f"/events/{event_id}/rsvps", json={"status": "going"}, headers=guest.headers
        )
        assert rsvp.status_code == 200, rsvp.text

    rows = await _walk(
        client,
        f"/events/{event_id}/rsvps",
        setup.member.headers,
        limit=3,
        user_key="user_id",
    )
    assert [r["user_id"] for r in rows] == [guest.id for guest in guests]


async def test_invites_page_forward_without_loss_or_duplicates(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user
) -> None:
    """Same walk for the invites twin, keyed on invited_user_id.

    Falsified by: flipping the invites route's compound comparison from > to >=
    (duplicated rows across pages)."""
    setup = await make_chapter_with("member")
    event_id = await _public_event(client, setup)

    invitees = [await _outsider(client, make_user, None, verified=False) for _ in range(5)]
    # One POST per invitee so created_at strictly increases with the invite order.
    for invitee in invitees:
        invited = await client.post(
            f"/events/{event_id}/invites",
            json={"user_ids": [invitee.id]},
            headers=setup.president.headers,
        )
        assert invited.status_code == 201, invited.text

    rows = await _walk(
        client,
        f"/events/{event_id}/invites",
        setup.member.headers,
        limit=2,
        user_key="invited_user_id",
    )
    assert [r["invited_user_id"] for r in rows] == [invitee.id for invitee in invitees]


async def test_guests_wrapper_is_gone(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """GET /events/{id}/guests no longer exists - deleted with its one consumer
    migrated, per the c275 amended ruling; a 200 here means someone resurrected the
    unbounded wrapper.

    Falsified by: re-adding a stub @router.get('/events/{event_id}/guests') route
    (this test then saw its 200 and went red)."""
    setup = await make_chapter_with("member")
    event_id = await _public_event(client, setup)
    gone = await client.get(f"/events/{event_id}/guests", headers=setup.member.headers)
    assert gone.status_code == 404, gone.text
