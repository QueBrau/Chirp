"""Events (board card c33): member-created events + RSVP upsert, org-scoped like feed.py."""
from __future__ import annotations

import uuid

from httpx import AsyncClient

from tests.conftest import MakeChapterWith


def _event_body(title: str = "Rush Week Mixer") -> dict[str, str]:
    return {
        "title": title,
        "date_label": "Sat, Sep 27 - 7:00 PM",
        "location": "Chapter House",
        "cover_url": "https://picsum.photos/seed/rush/800/600",
    }


async def test_member_creates_and_lists_events_newest_first(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Any active member (not just e-board) can host an event; list is newest first."""
    setup = await make_chapter_with("member")

    first = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body("First Mixer"),
        headers=setup.member.headers,
    )
    assert first.status_code == 201, first.text
    assert first.json()["chapter_id"] == setup.chapter_id
    assert first.json()["host_id"] == setup.member.id
    assert first.json()["title"] == "First Mixer"

    second = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body("Second Mixer"),
        headers=setup.president.headers,
    )
    assert second.status_code == 201, second.text

    listed = await client.get(
        f"/chapters/{setup.chapter_id}/events", headers=setup.member.headers
    )
    assert listed.status_code == 200, listed.text
    titles = [e["title"] for e in listed.json()]
    assert titles == ["Second Mixer", "First Mixer"]


async def test_non_member_is_403_on_list_and_create(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """A member of chapter A gets 403 not_a_member on chapter B's events."""
    chapter_a = await make_chapter_with("member")
    chapter_b = await make_chapter_with("president")

    listed = await client.get(
        f"/chapters/{chapter_b.chapter_id}/events", headers=chapter_a.member.headers
    )
    assert listed.status_code == 403, listed.text
    assert listed.json() == {"detail": "not_a_member"}

    created = await client.post(
        f"/chapters/{chapter_b.chapter_id}/events",
        json=_event_body(),
        headers=chapter_a.member.headers,
    )
    assert created.status_code == 403, created.text
    assert created.json() == {"detail": "not_a_member"}


async def test_rsvp_upsert_flips_status_single_row(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """PUT twice with different statuses updates the same row, not a second one."""
    setup = await make_chapter_with("member")
    created = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body(),
        headers=setup.president.headers,
    )
    assert created.status_code == 201, created.text
    event_id = created.json()["id"]

    first_rsvp = await client.put(
        f"/events/{event_id}/rsvps",
        json={"status": "going"},
        headers=setup.member.headers,
    )
    assert first_rsvp.status_code == 200, first_rsvp.text
    assert first_rsvp.json() == {
        "event_id": event_id,
        "user_id": setup.member.id,
        "status": "going",
        "created_at": first_rsvp.json()["created_at"],
    }

    second_rsvp = await client.put(
        f"/events/{event_id}/rsvps",
        json={"status": "maybe"},
        headers=setup.member.headers,
    )
    assert second_rsvp.status_code == 200, second_rsvp.text
    assert second_rsvp.json()["status"] == "maybe"

    listed = await client.get(f"/events/{event_id}/rsvps", headers=setup.member.headers)
    assert listed.status_code == 200, listed.text
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0]["status"] == "maybe"
    assert rows[0]["user_id"] == setup.member.id


async def test_rsvp_non_member_is_403(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """A member of chapter A cannot list or set an RSVP on chapter B's event."""
    chapter_a = await make_chapter_with("member")
    chapter_b = await make_chapter_with("president")
    created = await client.post(
        f"/chapters/{chapter_b.chapter_id}/events",
        json=_event_body(),
        headers=chapter_b.president.headers,
    )
    assert created.status_code == 201, created.text
    event_id = created.json()["id"]

    listed = await client.get(f"/events/{event_id}/rsvps", headers=chapter_a.member.headers)
    assert listed.status_code == 403, listed.text
    assert listed.json() == {"detail": "not_a_member"}

    put = await client.put(
        f"/events/{event_id}/rsvps",
        json={"status": "going"},
        headers=chapter_a.member.headers,
    )
    assert put.status_code == 403, put.text
    assert put.json() == {"detail": "not_a_member"}


async def test_unauthenticated_is_401(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """No X-Debug-Firebase-Uid header (emulated mode) is 401 on every events endpoint."""
    setup = await make_chapter_with("member")
    created = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body(),
        headers=setup.president.headers,
    )
    assert created.status_code == 201, created.text
    event_id = created.json()["id"]

    list_events_resp = await client.get(f"/chapters/{setup.chapter_id}/events")
    assert list_events_resp.status_code == 401, list_events_resp.text

    create_event_resp = await client.post(
        f"/chapters/{setup.chapter_id}/events", json=_event_body()
    )
    assert create_event_resp.status_code == 401, create_event_resp.text

    list_rsvps_resp = await client.get(f"/events/{event_id}/rsvps")
    assert list_rsvps_resp.status_code == 401, list_rsvps_resp.text

    put_rsvp_resp = await client.put(f"/events/{event_id}/rsvps", json={"status": "going"})
    assert put_rsvp_resp.status_code == 401, put_rsvp_resp.text


async def test_unknown_event_id_is_404(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """A well-formed but nonexistent event_id 404s instead of 403, on both RSVP routes."""
    setup = await make_chapter_with("member")
    fake_event_id = str(uuid.uuid4())

    listed = await client.get(
        f"/events/{fake_event_id}/rsvps", headers=setup.member.headers
    )
    assert listed.status_code == 404, listed.text
    assert listed.json() == {"detail": "event_not_found"}

    put = await client.put(
        f"/events/{fake_event_id}/rsvps",
        json={"status": "going"},
        headers=setup.member.headers,
    )
    assert put.status_code == 404, put.text
    assert put.json() == {"detail": "event_not_found"}


async def test_events_with_rsvps_batches_correctly(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The c43 batch route returns every event newest-first with exactly its own RSVPs —
    no cross-event bleed, and an RSVP-less event still appears with an empty list."""
    setup = await make_chapter_with("member")

    first = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body("First Mixer"),
        headers=setup.member.headers,
    )
    second = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body("Second Mixer"),
        headers=setup.president.headers,
    )
    assert first.status_code == 201 and second.status_code == 201

    for headers, status in ((setup.member.headers, "going"), (setup.president.headers, "maybe")):
        rsvp = await client.put(
            f"/events/{first.json()['id']}/rsvps", json={"status": status}, headers=headers
        )
        assert rsvp.status_code == 200, rsvp.text

    response = await client.get(
        f"/chapters/{setup.chapter_id}/events-with-rsvps", headers=setup.member.headers
    )

    assert response.status_code == 200, response.text
    rows = response.json()
    assert [row["event"]["title"] for row in rows] == ["Second Mixer", "First Mixer"]
    assert rows[0]["rsvps"] == []
    first_rsvps = {(r["user_id"], r["status"]) for r in rows[1]["rsvps"]}
    assert first_rsvps == {(setup.member.id, "going"), (setup.president.id, "maybe")}


async def test_events_with_rsvps_is_org_scoped(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Same §8.4 rule as the plain events list: chapter A's member gets 403 on chapter B."""
    chapter_a = await make_chapter_with("member")
    chapter_b = await make_chapter_with("president")

    response = await client.get(
        f"/chapters/{chapter_b.chapter_id}/events-with-rsvps",
        headers=chapter_a.member.headers,
    )
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "not_a_member"}
