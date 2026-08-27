"""Events (c33, c198): member-created events, invites, RSVPs, visibility, edit/cancel."""
from __future__ import annotations

import uuid

from httpx import AsyncClient

from tests.conftest import MakeChapterWith


def _event_body(
    title: str = "Rush Week Mixer",
    *,
    starts_at: str = "2026-09-27T19:00:00Z",
    **extra: object,
) -> dict[str, object]:
    body: dict[str, object] = {
        "title": title,
        "starts_at": starts_at,
        "location": "Chapter House",
        "cover_url": "https://picsum.photos/seed/rush/800/600",
    }
    body.update(extra)
    return body


async def test_member_creates_and_lists_events_newest_first(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Any active member (not just e-board) can host; list is soonest-first by start."""
    setup = await make_chapter_with("member")

    first = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body("First Mixer", starts_at="2026-09-20T19:00:00Z"),
        headers=setup.member.headers,
    )
    assert first.status_code == 201, first.text
    assert first.json()["chapter_id"] == setup.chapter_id
    assert first.json()["host_id"] == setup.member.id
    assert first.json()["title"] == "First Mixer"

    second = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body("Second Mixer", starts_at="2026-09-27T19:00:00Z"),
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
    """The c43 batch route returns every event by start time with exactly its own RSVPs —
    no cross-event bleed, and an RSVP-less event still appears with an empty list."""
    setup = await make_chapter_with("member")

    first = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body("First Mixer", starts_at="2026-09-20T19:00:00Z"),
        headers=setup.member.headers,
    )
    second = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body("Second Mixer", starts_at="2026-09-27T19:00:00Z"),
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


# ---------------------------------------------------------------------------
# c198: visibility tiers, invites, edit/cancel.
#
# The visibility tests are the security surface of this module. Each one pins ONE tier
# against ONE kind of caller, because the failure that matters is not "the gate is
# missing" but "the gate admits one more person than intended", and a test that only
# proves the happy path cannot see that.
# ---------------------------------------------------------------------------


async def _campus_of_chapter(chapter_id: str) -> str:
    """The chapter's campus id. make_chapter_with builds a campus but does not expose it."""
    from sqlalchemy import text as sql_text

    from app.db import get_session_factory

    async with get_session_factory()() as session:
        row = await session.execute(
            sql_text("SELECT campus_id FROM chapters WHERE id = :id"), {"id": chapter_id}
        )
        return str(row.scalar_one())


async def _outsider(client: AsyncClient, make_user, campus_id: str | None, *, verified: bool):
    """A user who is in NO chapter, optionally pinned to a campus and verified there."""
    from tests.conftest import set_campus

    user = await make_user("Campus Outsider")
    if campus_id is not None:
        await set_campus(user.id, campus_id, verified=verified)
    return user


async def test_default_visibility_is_chapter_and_excludes_the_whole_campus(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user
) -> None:
    """An event created without a visibility is members-only, not campus-wide.

    This is the guard on the retroactive-widening problem: every events row predating
    0024 takes the column default, so if the default admitted the campus, every past
    party would have been republished to every verified student on it.
    """
    setup = await make_chapter_with("member")
    campus_id = await _campus_of_chapter(setup.chapter_id)
    created = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body(),
        headers=setup.president.headers,
    )
    assert created.status_code == 201, created.text
    assert created.json()["visibility"] == "chapter"
    event_id = created.json()["id"]

    # Verified, same campus, and still refused - membership is the only way in.
    stranger = await _outsider(client, make_user, campus_id, verified=True)
    read = await client.get(f"/events/{event_id}", headers=stranger.headers)
    assert read.status_code == 403, read.text
    assert read.json() == {"detail": "not_a_member"}


async def test_campus_tier_admits_a_verified_student_of_that_campus(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user
) -> None:
    """visibility='campus' lets a verified student of the same campus read AND answer."""
    setup = await make_chapter_with("member")
    campus_id = await _campus_of_chapter(setup.chapter_id)
    created = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body(visibility="campus"),
        headers=setup.president.headers,
    )
    assert created.status_code == 201, created.text
    event_id = created.json()["id"]

    student = await _outsider(client, make_user, campus_id, verified=True)
    read = await client.get(f"/events/{event_id}", headers=student.headers)
    assert read.status_code == 200, read.text

    rsvp = await client.put(
        f"/events/{event_id}/rsvps", json={"status": "going"}, headers=student.headers
    )
    assert rsvp.status_code == 200, rsvp.text


async def test_campus_tier_refuses_unverified_and_other_campus(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user, make_campus
) -> None:
    """The two ways of failing the campus tier report DIFFERENT reasons, on purpose."""
    setup = await make_chapter_with("member")
    campus_id = await _campus_of_chapter(setup.chapter_id)
    created = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body(visibility="campus"),
        headers=setup.president.headers,
    )
    event_id = created.json()["id"]

    # Right campus, never proved an .edu - the c88 gate state.
    unverified = await _outsider(client, make_user, campus_id, verified=False)
    resp = await client.get(f"/events/{event_id}", headers=unverified.headers)
    assert resp.status_code == 403, resp.text
    assert resp.json() == {"detail": "campus_unverified"}

    # Verified, but at a different school.
    other_campus = await make_campus()
    elsewhere = await _outsider(client, make_user, other_campus, verified=True)
    resp = await client.get(f"/events/{event_id}", headers=elsewhere.headers)
    assert resp.status_code == 403, resp.text
    assert resp.json() == {"detail": "not_your_campus"}


async def test_verified_tier_admits_another_campus_but_not_the_unverified(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user, make_campus
) -> None:
    """visibility='verified' is what makes a sister-chapter or inter-campus party work."""
    setup = await make_chapter_with("member")
    created = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body(visibility="verified"),
        headers=setup.president.headers,
    )
    event_id = created.json()["id"]

    other_campus = await make_campus()
    elsewhere = await _outsider(client, make_user, other_campus, verified=True)
    assert (await client.get(f"/events/{event_id}", headers=elsewhere.headers)).status_code == 200

    nobody = await _outsider(client, make_user, None, verified=False)
    resp = await client.get(f"/events/{event_id}", headers=nobody.headers)
    assert resp.status_code == 403, resp.text
    assert resp.json() == {"detail": "campus_unverified"}


async def test_an_invite_admits_someone_the_tier_would_refuse(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user
) -> None:
    """The central claim of the design: an invite grants access, or it means nothing.

    The invitee here is in no chapter, on no campus and unverified - refused by every
    tier. The invite alone is what lets them in.
    """
    setup = await make_chapter_with("member")
    created = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body(),  # 'chapter' - the narrowest tier there is
        headers=setup.president.headers,
    )
    event_id = created.json()["id"]

    guest = await _outsider(client, make_user, None, verified=False)
    before = await client.get(f"/events/{event_id}", headers=guest.headers)
    assert before.status_code == 403, before.text

    invited = await client.post(
        f"/events/{event_id}/invites",
        json={"user_ids": [guest.id]},
        headers=setup.president.headers,
    )
    assert invited.status_code == 201, invited.text

    after = await client.get(f"/events/{event_id}", headers=guest.headers)
    assert after.status_code == 200, after.text

    mine = await client.get("/me/event-invites", headers=guest.headers)
    assert mine.status_code == 200, mine.text
    assert [e["id"] for e in mine.json()] == [event_id]


async def test_inviting_is_gated_to_host_or_eboard_and_is_idempotent(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user
) -> None:
    """Inviting GRANTS ACCESS, so it is gated like a permission grant, not a write.

    A rank-and-file member must not be able to use somebody else's event to let an
    outsider in.
    """
    setup = await make_chapter_with("member")
    created = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body(),
        headers=setup.president.headers,  # hosted by the president
    )
    event_id = created.json()["id"]
    guest = await _outsider(client, make_user, None, verified=False)

    refused = await client.post(
        f"/events/{event_id}/invites",
        json={"user_ids": [guest.id]},
        headers=setup.member.headers,
    )
    assert refused.status_code == 403, refused.text
    assert refused.json() == {"detail": "not_the_host"}

    first = await client.post(
        f"/events/{event_id}/invites",
        json={"user_ids": [guest.id]},
        headers=setup.president.headers,
    )
    second = await client.post(
        f"/events/{event_id}/invites",
        json={"user_ids": [guest.id, guest.id]},
        headers=setup.president.headers,
    )
    assert first.status_code == 201 and second.status_code == 201, second.text
    assert len(second.json()) == 1, "double-tap must not create a second invite row"

    unknown = await client.post(
        f"/events/{event_id}/invites",
        json={"user_ids": [str(uuid.uuid4())]},
        headers=setup.president.headers,
    )
    assert unknown.status_code == 422, unknown.text
    assert unknown.json() == {"detail": "unknown_user_in_invite_list"}


async def test_public_event_is_readable_unauthenticated_and_leaks_no_guest_list(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The one unauthenticated route. It must expose the party and NOT who is at it.

    This is the c198 hole through c88, and this test is what pins its size. If someone
    later returns EventOut here, host_id and chapter_id reappear and this fails.
    """
    setup = await make_chapter_with("member")
    created = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body(visibility="public"),
        headers=setup.president.headers,
    )
    event_id = created.json()["id"]
    rsvp = await client.put(
        f"/events/{event_id}/rsvps", json={"status": "going"}, headers=setup.member.headers
    )
    assert rsvp.status_code == 200, rsvp.text

    public = await client.get(f"/public/events/{event_id}")  # no headers at all
    assert public.status_code == 200, public.text
    body = public.json()
    assert body["title"] == "Rush Week Mixer"
    assert body["going_count"] == 1
    assert body["hosted_by"].startswith("Sigma Test")
    for leaked in ("host_id", "chapter_id", "rsvps", "invites", "visibility"):
        assert leaked not in body, f"public serializer leaked {leaked}"

    # The guest list is never reachable without an account, at any tier.
    assert (await client.get(f"/events/{event_id}/guests")).status_code == 401


async def test_public_route_404s_for_a_non_public_event(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """404 rather than 403: an anonymous caller learns nothing about which ids exist."""
    setup = await make_chapter_with("member")
    for tier in ("chapter", "campus", "verified"):
        created = await client.post(
            f"/chapters/{setup.chapter_id}/events",
            json=_event_body(visibility=tier),
            headers=setup.president.headers,
        )
        assert created.status_code == 201, created.text
        resp = await client.get(f"/public/events/{created.json()['id']}")
        assert resp.status_code == 404, f"{tier}: {resp.text}"
        assert resp.json() == {"detail": "event_not_found"}


async def test_guest_list_needs_membership_an_invite_or_your_own_rsvp(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user
) -> None:
    """Reading a public event does not entitle you to the roster of who is attending."""
    setup = await make_chapter_with("member")
    created = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body(visibility="public"),
        headers=setup.president.headers,
    )
    event_id = created.json()["id"]

    stranger = await _outsider(client, make_user, None, verified=False)
    assert (await client.get(f"/events/{event_id}", headers=stranger.headers)).status_code == 200

    refused = await client.get(f"/events/{event_id}/guests", headers=stranger.headers)
    assert refused.status_code == 403, refused.text
    assert refused.json() == {"detail": "not_on_the_guest_list"}

    # Answering the invitation makes you part of the event, and the list opens.
    await client.put(
        f"/events/{event_id}/rsvps", json={"status": "going"}, headers=stranger.headers
    )
    allowed = await client.get(f"/events/{event_id}/guests", headers=stranger.headers)
    assert allowed.status_code == 200, allowed.text
    assert [r["user_id"] for r in allowed.json()["rsvps"]] == [stranger.id]


async def test_edit_is_host_or_eboard_only(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Hosting is open to any member; rewriting someone else's event is not."""
    setup = await make_chapter_with("member")
    created = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body(),
        headers=setup.member.headers,  # hosted by the rank-and-file member
    )
    event_id = created.json()["id"]

    by_host = await client.patch(
        f"/events/{event_id}", json={"title": "Moved Indoors"}, headers=setup.member.headers
    )
    assert by_host.status_code == 200, by_host.text
    assert by_host.json()["title"] == "Moved Indoors"

    # The e-board may also fix it, even though they are not the host.
    by_eboard = await client.patch(
        f"/events/{event_id}",
        json={"location": "The Annex"},
        headers=setup.president.headers,
    )
    assert by_eboard.status_code == 200, by_eboard.text
    assert by_eboard.json()["location"] == "The Annex"
    assert by_eboard.json()["title"] == "Moved Indoors", "omitted fields must be left alone"

    other = await make_chapter_with("member")
    refused = await client.patch(
        f"/events/{event_id}", json={"title": "Hijacked"}, headers=other.member.headers
    )
    assert refused.status_code == 403, refused.text
    assert refused.json() == {"detail": "not_a_member"}


async def test_cancel_is_idempotent_and_stops_new_rsvps(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """A cancelled event keeps its row and its guest list, and refuses new answers."""
    setup = await make_chapter_with("member")
    created = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body(),
        headers=setup.president.headers,
    )
    event_id = created.json()["id"]
    await client.put(
        f"/events/{event_id}/rsvps", json={"status": "going"}, headers=setup.member.headers
    )

    first = await client.post(f"/events/{event_id}/cancel", headers=setup.president.headers)
    assert first.status_code == 200, first.text
    assert first.json()["canceled_at"] is not None

    second = await client.post(f"/events/{event_id}/cancel", headers=setup.president.headers)
    assert second.status_code == 200, second.text
    assert second.json()["canceled_at"] == first.json()["canceled_at"], "keep the first moment"

    late = await client.put(
        f"/events/{event_id}/rsvps", json={"status": "going"}, headers=setup.president.headers
    )
    assert late.status_code == 403, late.text
    assert late.json() == {"detail": "event_canceled"}

    edit = await client.patch(
        f"/events/{event_id}", json={"title": "Back On"}, headers=setup.president.headers
    )
    assert edit.status_code == 403, edit.text
    assert edit.json() == {"detail": "event_canceled"}

    # The guest list survives - the people who need telling are still on it.
    guests = await client.get(f"/events/{event_id}/guests", headers=setup.member.headers)
    assert guests.status_code == 200, guests.text
    assert [r["user_id"] for r in guests.json()["rsvps"]] == [setup.member.id]


async def test_end_before_start_is_422_on_create_and_on_edit(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The edit case is the one worth having: it can only be caught on the MERGED state.

    Moving starts_at past an already-stored ends_at is invalid, and the submitted body
    alone cannot see it - it names only starts_at.
    """
    setup = await make_chapter_with("member")
    bad = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body(starts_at="2026-09-27T19:00:00Z", ends_at="2026-09-27T18:00:00Z"),
        headers=setup.president.headers,
    )
    assert bad.status_code == 422, bad.text

    created = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body(starts_at="2026-09-27T19:00:00Z", ends_at="2026-09-27T23:00:00Z"),
        headers=setup.president.headers,
    )
    assert created.status_code == 201, created.text
    event_id = created.json()["id"]

    moved = await client.patch(
        f"/events/{event_id}",
        json={"starts_at": "2026-09-28T02:00:00Z"},  # now after the stored ends_at
        headers=setup.president.headers,
    )
    assert moved.status_code == 422, moved.text
    assert moved.json() == {"detail": "ends_at_must_be_after_starts_at"}

    unchanged = await client.get(f"/events/{event_id}", headers=setup.president.headers)
    assert unchanged.json()["starts_at"].startswith("2026-09-27T19:00"), "rejected edit persisted"


async def test_naive_and_aware_datetimes_mix_is_422_not_500(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """c198 review finding: one naive and one aware datetime used to raise a raw
    TypeError inside the ordering checks - a 500, not the documented 422. A naive
    datetime is taken as UTC (the chapters.py / core/invites.py convention).
    """
    setup = await make_chapter_with("member")
    bad = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body(starts_at="2026-09-27T19:00:00", ends_at="2026-09-27T18:00:00Z"),
        headers=setup.president.headers,
    )
    assert bad.status_code == 422, bad.text

    created = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body(starts_at="2026-09-27T19:00:00", ends_at="2026-09-27T23:00:00"),
        headers=setup.president.headers,
    )
    assert created.status_code == 201, created.text
    event_id = created.json()["id"]
    assert created.json()["starts_at"].startswith("2026-09-27T19:00")

    moved = await client.patch(
        f"/events/{event_id}",
        json={"starts_at": "2026-09-28T02:00:00"},
        headers=setup.president.headers,
    )
    assert moved.status_code == 422, moved.text
    assert moved.json() == {"detail": "ends_at_must_be_after_starts_at"}
