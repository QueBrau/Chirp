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
    """The c43 batch route returns every event by start time with exactly its own RSVP
    summary — no cross-event bleed, and an RSVP-less event still appears with zeros.
    Shape migrated by c280 (full rsvps array -> counts + going_preview + my_rsvp_status);
    the properties pinned here (batching, per-event scoping, empty-event presence) are
    unchanged and asserted at least as strongly against the new shape."""
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
    assert rows[0]["counts"] == {"going": 0, "maybe": 0, "cant": 0, "invited_unanswered": 0}
    assert rows[0]["going_preview"] == []
    assert rows[0]["my_rsvp_status"] is None
    assert rows[1]["counts"] == {"going": 1, "maybe": 1, "cant": 0, "invited_unanswered": 0}
    assert [r["user_id"] for r in rows[1]["going_preview"]] == [setup.member.id]
    assert rows[1]["my_rsvp_status"] == "going"  # the caller (member) said going


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

    # The guest list is never reachable without an account, at any tier - both
    # halves of it (c275 split the old /guests wrapper into these two routes).
    assert (await client.get(f"/events/{event_id}/rsvps")).status_code == 401
    assert (await client.get(f"/events/{event_id}/invites")).status_code == 401


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

    # Both halves of the split guest list (c275) refuse with the same string.
    for path in ("rsvps", "invites"):
        refused = await client.get(f"/events/{event_id}/{path}", headers=stranger.headers)
        assert refused.status_code == 403, refused.text
        assert refused.json() == {"detail": "not_on_the_guest_list"}

    # Answering the invitation makes you part of the event, and the list opens.
    await client.put(
        f"/events/{event_id}/rsvps", json={"status": "going"}, headers=stranger.headers
    )
    allowed = await client.get(f"/events/{event_id}/rsvps", headers=stranger.headers)
    assert allowed.status_code == 200, allowed.text
    assert [r["user_id"] for r in allowed.json()] == [stranger.id]
    assert (
        await client.get(f"/events/{event_id}/invites", headers=stranger.headers)
    ).status_code == 200


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
    rsvps = await client.get(f"/events/{event_id}/rsvps", headers=setup.member.headers)
    assert rsvps.status_code == 200, rsvps.text
    assert [r["user_id"] for r in rsvps.json()] == [setup.member.id]


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


# ---------------------------------------------------------------------------
# c201: list_events, list_events_with_rsvps and list_my_invites had no limit at all.
# Cursor-paginated on (starts_at, id) - the same compound shape as chirps.py /
# messages.py / feed.py (SECURITY-REVIEW finding 10 class).
# ---------------------------------------------------------------------------


async def test_events_list_limit_is_capped_and_out_of_range_limit_is_422(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """`limit` truncates the page; values outside Query(ge=1, le=200) are 422."""
    setup = await make_chapter_with("member")
    for i in range(3):
        created = await client.post(
            f"/chapters/{setup.chapter_id}/events",
            json=_event_body(f"Event {i}", starts_at=f"2026-09-2{i}T19:00:00Z"),
            headers=setup.member.headers,
        )
        assert created.status_code == 201, created.text

    capped = await client.get(
        f"/chapters/{setup.chapter_id}/events",
        params={"limit": 2},
        headers=setup.member.headers,
    )
    assert capped.status_code == 200, capped.text
    assert len(capped.json()) == 2

    too_big = await client.get(
        f"/chapters/{setup.chapter_id}/events",
        params={"limit": 201},
        headers=setup.member.headers,
    )
    assert too_big.status_code == 422, too_big.text

    too_small = await client.get(
        f"/chapters/{setup.chapter_id}/events",
        params={"limit": 0},
        headers=setup.member.headers,
    )
    assert too_small.status_code == 422, too_small.text


async def test_events_list_pages_through_cursor_without_overlap_or_loss(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Paging with before/before_id across the whole set returns every event exactly
    once, in the same newest-first order as an unpaginated call."""
    setup = await make_chapter_with("member")
    created_ids: list[str] = []
    for i in range(5):
        created = await client.post(
            f"/chapters/{setup.chapter_id}/events",
            json=_event_body(f"Event {i}", starts_at=f"2026-09-{10 + i}T19:00:00Z"),
            headers=setup.member.headers,
        )
        assert created.status_code == 201, created.text
        created_ids.append(created.json()["id"])

    full = await client.get(
        f"/chapters/{setup.chapter_id}/events", headers=setup.member.headers
    )
    assert full.status_code == 200, full.text
    expected_order = [e["id"] for e in full.json()]
    assert expected_order == list(reversed(created_ids))

    collected: list[str] = []
    before: str | None = None
    before_id: str | None = None
    for _ in range(10):  # generous cap so a regression can't spin the loop forever
        params: dict[str, str | int] = {"limit": 2}
        if before is not None:
            params["before"] = before
            params["before_id"] = before_id
        page = await client.get(
            f"/chapters/{setup.chapter_id}/events",
            params=params,
            headers=setup.member.headers,
        )
        assert page.status_code == 200, page.text
        items = page.json()
        if not items:
            break
        collected.extend(e["id"] for e in items)
        before = items[-1]["starts_at"]
        before_id = items[-1]["id"]

    assert collected == expected_order


async def test_tied_starts_at_at_page_boundary_is_lossless(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Several events sharing one starts_at, straddled across a page boundary: the
    compound (starts_at, id) cursor must not drop any of them - same tie-break failure
    mode as messages.py's created_at cursor (SECURITY-REVIEW finding 10)."""
    setup = await make_chapter_with("member")

    later = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body("Later", starts_at="2026-10-05T19:00:00Z"),
        headers=setup.member.headers,
    )
    assert later.status_code == 201, later.text

    tied_ids: list[str] = []
    for i in range(3):
        tied = await client.post(
            f"/chapters/{setup.chapter_id}/events",
            json=_event_body(f"Tied {i}", starts_at="2026-10-01T19:00:00Z"),
            headers=setup.member.headers,
        )
        assert tied.status_code == 201, tied.text
        tied_ids.append(tied.json()["id"])

    earlier = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body("Earlier", starts_at="2026-09-25T19:00:00Z"),
        headers=setup.member.headers,
    )
    assert earlier.status_code == 201, earlier.text

    all_ids = {later.json()["id"], *tied_ids, earlier.json()["id"]}

    collected: list[str] = []
    before: str | None = None
    before_id: str | None = None
    for _ in range(10):
        params: dict[str, str | int] = {"limit": 2}
        if before is not None:
            params["before"] = before
            params["before_id"] = before_id
        page = await client.get(
            f"/chapters/{setup.chapter_id}/events",
            params=params,
            headers=setup.member.headers,
        )
        assert page.status_code == 200, page.text
        items = page.json()
        if not items:
            break
        collected.extend(e["id"] for e in items)
        before = items[-1]["starts_at"]
        before_id = items[-1]["id"]

    assert len(collected) == len(set(collected)), "no duplicate rows across pages"
    assert set(collected) == all_ids, "no rows dropped at the tied-timestamp boundary"
    # The bounding events keep their fixed position; the tie group can land in
    # either id order within itself, since starts_at alone doesn't determine it.
    assert collected[0] == later.json()["id"]
    assert collected[-1] == earlier.json()["id"]
    assert set(collected[1:-1]) == set(tied_ids)


async def test_events_with_rsvps_page_only_includes_rsvps_for_returned_events(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The RSVP fan-out must stay bounded by the event page it rides along with: an
    event left off the page must not have its RSVPs (or itself) show up anywhere in
    the response, and paging in the leftover event must bring its RSVPs back."""
    setup = await make_chapter_with("member")

    first = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body("First", starts_at="2026-09-20T19:00:00Z"),
        headers=setup.member.headers,
    )
    second = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body("Second", starts_at="2026-09-25T19:00:00Z"),
        headers=setup.president.headers,
    )
    third = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body("Third", starts_at="2026-09-30T19:00:00Z"),
        headers=setup.member.headers,
    )
    assert first.status_code == second.status_code == third.status_code == 201

    for event in (first, second, third):
        rsvp = await client.put(
            f"/events/{event.json()['id']}/rsvps",
            json={"status": "going"},
            headers=setup.member.headers,
        )
        assert rsvp.status_code == 200, rsvp.text

    page = await client.get(
        f"/chapters/{setup.chapter_id}/events-with-rsvps",
        params={"limit": 2},
        headers=setup.member.headers,
    )
    assert page.status_code == 200, page.text
    rows = page.json()
    assert [row["event"]["title"] for row in rows] == ["Third", "Second"]
    for row in rows:
        # c280 shape: the summary plays the old rsvps-array role here.
        assert row["counts"]["going"] == 1, "each returned event kept its own RSVP"
        assert [r["user_id"] for r in row["going_preview"]] == [setup.member.id]
    returned_event_ids = {row["event"]["id"] for row in rows}
    assert first.json()["id"] not in returned_event_ids

    second_page = await client.get(
        f"/chapters/{setup.chapter_id}/events-with-rsvps",
        params={
            "limit": 2,
            "before": rows[-1]["event"]["starts_at"],
            "before_id": rows[-1]["event"]["id"],
        },
        headers=setup.member.headers,
    )
    assert second_page.status_code == 200, second_page.text
    remaining = second_page.json()
    assert [row["event"]["title"] for row in remaining] == ["First"]
    assert remaining[0]["counts"]["going"] == 1, (
        "the leftover event's own RSVP came back on page two"
    )
    assert [r["user_id"] for r in remaining[0]["going_preview"]] == [setup.member.id]


async def test_my_invites_lists_soonest_first_and_pages_forward_without_loss(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user
) -> None:
    """list_my_invites is ASCENDING (soonest starts_at first), the opposite direction
    from every other route in this module - its own docstring always said so, but the
    query sorted descending until c201 fixed it alongside the pagination work.
    Paginating forward with before/before_id must move to LATER starts_at values, not
    earlier ones, to match.
    """
    setup = await make_chapter_with("member")
    guest = await _outsider(client, make_user, None, verified=False)

    created_ids: list[str] = []
    for i in range(4):
        created = await client.post(
            f"/chapters/{setup.chapter_id}/events",
            json=_event_body(f"Invite Event {i}", starts_at=f"2026-09-{10 + i}T19:00:00Z"),
            headers=setup.president.headers,
        )
        assert created.status_code == 201, created.text
        event_id = created.json()["id"]
        created_ids.append(event_id)
        invited = await client.post(
            f"/events/{event_id}/invites",
            json={"user_ids": [guest.id]},
            headers=setup.president.headers,
        )
        assert invited.status_code == 201, invited.text

    full = await client.get("/me/event-invites", headers=guest.headers)
    assert full.status_code == 200, full.text
    # soonest-first (ascending) is the same order the events were created in here,
    # since each successive event was given a LATER starts_at than the last.
    assert [e["id"] for e in full.json()] == created_ids

    collected: list[str] = []
    before: str | None = None
    before_id: str | None = None
    for _ in range(10):  # generous cap so a regression can't spin the loop forever
        params: dict[str, str | int] = {"limit": 2}
        if before is not None:
            params["before"] = before
            params["before_id"] = before_id
        page = await client.get(
            "/me/event-invites", params=params, headers=guest.headers
        )
        assert page.status_code == 200, page.text
        items = page.json()
        if not items:
            break
        collected.extend(e["id"] for e in items)
        before = items[-1]["starts_at"]
        before_id = items[-1]["id"]

    assert collected == created_ids
async def test_explicit_null_clears_ends_at_and_description_omission_leaves_them(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """c202: clearing an end time and a description is reachable through PATCH today.

    body.model_dump(exclude_unset=True) in update_event relies on a pydantic v2
    distinction the EventUpdate docstring understated: {"ends_at": null} in the request
    body IS an explicit assignment (the field lands in model_fields_set), which is a
    different thing from the client leaving ends_at out of the JSON altogether (never
    set, excluded by exclude_unset). So an explicit null already flows through to
    setattr(event, "ends_at", None) and clears the column - no sentinel needed.
    """
    setup = await make_chapter_with("member")
    created = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body(
            starts_at="2026-09-27T19:00:00Z",
            ends_at="2026-09-27T23:00:00Z",
            description="BYOB, RSVP required",
        ),
        headers=setup.president.headers,
    )
    assert created.status_code == 201, created.text
    event_id = created.json()["id"]
    assert created.json()["ends_at"] is not None
    assert created.json()["description"] == "BYOB, RSVP required"

    # Omitting the fields entirely leaves them alone - the ordinary "not_supplied" case.
    untouched = await client.patch(
        f"/events/{event_id}", json={"title": "Rush Week Mixer (updated)"},
        headers=setup.president.headers,
    )
    assert untouched.status_code == 200, untouched.text
    assert untouched.json()["ends_at"] is not None, "omitted field must be left alone"
    assert untouched.json()["description"] == "BYOB, RSVP required"

    # Explicit null clears both. Note ends_at=None must also survive the merged-state
    # ordering re-check (`event.ends_at is not None and ...`), which it does by being
    # skipped rather than compared.
    cleared = await client.patch(
        f"/events/{event_id}",
        json={"ends_at": None, "description": None},
        headers=setup.president.headers,
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["ends_at"] is None
    assert cleared.json()["description"] is None

    fetched = await client.get(f"/events/{event_id}", headers=setup.president.headers)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["ends_at"] is None
    assert fetched.json()["description"] is None

    # A cleared event still edits normally afterwards - clearing did not brick the row.
    edited_again = await client.patch(
        f"/events/{event_id}", json={"location": "The Annex"}, headers=setup.president.headers
    )
    assert edited_again.status_code == 200, edited_again.text
    assert edited_again.json()["location"] == "The Annex"
    assert edited_again.json()["ends_at"] is None, "still cleared"
    assert edited_again.json()["title"] == "Rush Week Mixer (updated)"


# ---- c204: GET /me/event-invites-with-rsvps ----


async def test_invites_with_rsvps_resolves_chapter_label_without_membership_and_hides_others_rsvps(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user
) -> None:
    """The whole point of c204: a cross-chapter invitee gets a real chapter label without
    ever being a member (the "Another chapter" fallback this replaces existed only
    because GET /chapters/{id} is member-scoped), and their row carries their OWN rsvp
    status only - never a fellow invitee's, even for the same event.
    """
    setup = await make_chapter_with("member")
    outsider = await _outsider(client, make_user, None, verified=False)

    chapter = await client.get(
        f"/chapters/{setup.chapter_id}", headers=setup.president.headers
    )
    assert chapter.status_code == 200, chapter.text
    expected_label = f"{chapter.json()['org_name']} {chapter.json()['chapter_name']}"

    created = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body(),
        headers=setup.president.headers,
    )
    assert created.status_code == 201, created.text
    event_id = created.json()["id"]

    # Confirm the outsider is genuinely not a member - the label below has to come from
    # somewhere other than membership.
    membership_check = await client.get(
        f"/chapters/{setup.chapter_id}/events", headers=outsider.headers
    )
    assert membership_check.status_code == 403, membership_check.text

    invited = await client.post(
        f"/events/{event_id}/invites",
        json={"user_ids": [outsider.id, setup.member.id]},
        headers=setup.president.headers,
    )
    assert invited.status_code == 201, invited.text

    # Nobody has answered yet - the outsider's own status is null, not missing.
    before_rsvp = await client.get(
        "/me/event-invites-with-rsvps", headers=outsider.headers
    )
    assert before_rsvp.status_code == 200, before_rsvp.text
    rows = before_rsvp.json()
    assert len(rows) == 1
    assert rows[0]["event"]["id"] == event_id
    assert rows[0]["my_rsvp_status"] is None
    assert rows[0]["hosted_by"] == expected_label

    # The chapter's own member answers the SAME event first.
    member_rsvp = await client.put(
        f"/events/{event_id}/rsvps", json={"status": "cant"}, headers=setup.member.headers
    )
    assert member_rsvp.status_code == 200, member_rsvp.text

    # The outsider's row must still show null - the member's answer must not leak in.
    still_null = await client.get(
        "/me/event-invites-with-rsvps", headers=outsider.headers
    )
    assert still_null.status_code == 200, still_null.text
    assert still_null.json()[0]["my_rsvp_status"] is None

    # Now the outsider answers. Their own row updates to their OWN status, not the
    # member's "cant" from above.
    outsider_rsvp = await client.put(
        f"/events/{event_id}/rsvps", json={"status": "going"}, headers=outsider.headers
    )
    assert outsider_rsvp.status_code == 200, outsider_rsvp.text

    after_rsvp = await client.get(
        "/me/event-invites-with-rsvps", headers=outsider.headers
    )
    assert after_rsvp.status_code == 200, after_rsvp.text
    assert after_rsvp.json()[0]["my_rsvp_status"] == "going"
    assert after_rsvp.json()[0]["hosted_by"] == expected_label

    # And the member's own row (same event) shows THEIR status, "cant" - proving each
    # invitee's row is scoped to themselves, not a shared view of the first responder.
    member_view = await client.get(
        "/me/event-invites-with-rsvps", headers=setup.member.headers
    )
    assert member_view.status_code == 200, member_view.text
    assert member_view.json()[0]["my_rsvp_status"] == "cant"


async def test_invites_with_rsvps_includes_cancelled_events(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user
) -> None:
    """A cancelled event stays on the list - "the party is off" is the row that matters
    most, mirroring list_my_invites' same rule."""
    setup = await make_chapter_with("member")
    outsider = await _outsider(client, make_user, None, verified=False)

    created = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body(),
        headers=setup.president.headers,
    )
    assert created.status_code == 201, created.text
    event_id = created.json()["id"]

    invited = await client.post(
        f"/events/{event_id}/invites",
        json={"user_ids": [outsider.id]},
        headers=setup.president.headers,
    )
    assert invited.status_code == 201, invited.text

    canceled = await client.post(
        f"/events/{event_id}/cancel", headers=setup.president.headers
    )
    assert canceled.status_code == 200, canceled.text

    listed = await client.get(
        "/me/event-invites-with-rsvps", headers=outsider.headers
    )
    assert listed.status_code == 200, listed.text
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0]["event"]["id"] == event_id
    assert rows[0]["event"]["canceled_at"] is not None
    assert rows[0]["my_rsvp_status"] is None


async def test_invites_with_rsvps_pages_forward_ascending_same_as_my_invites(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user
) -> None:
    """Same (starts_at, id) ascending cursor contract as list_my_invites (c201) - see
    that test's docstring for why ascending, not the DESC every other route uses."""
    setup = await make_chapter_with("member")
    guest = await _outsider(client, make_user, None, verified=False)

    created_ids: list[str] = []
    for i in range(4):
        created = await client.post(
            f"/chapters/{setup.chapter_id}/events",
            json=_event_body(f"Bulk Invite Event {i}", starts_at=f"2026-10-{10 + i}T19:00:00Z"),
            headers=setup.president.headers,
        )
        assert created.status_code == 201, created.text
        event_id = created.json()["id"]
        created_ids.append(event_id)
        invited = await client.post(
            f"/events/{event_id}/invites",
            json={"user_ids": [guest.id]},
            headers=setup.president.headers,
        )
        assert invited.status_code == 201, invited.text

    full = await client.get("/me/event-invites-with-rsvps", headers=guest.headers)
    assert full.status_code == 200, full.text
    assert [row["event"]["id"] for row in full.json()] == created_ids

    collected: list[str] = []
    before: str | None = None
    before_id: str | None = None
    for _ in range(10):  # generous cap so a regression can't spin the loop forever
        params: dict[str, str | int] = {"limit": 2}
        if before is not None:
            params["before"] = before
            params["before_id"] = before_id
        page = await client.get(
            "/me/event-invites-with-rsvps", params=params, headers=guest.headers
        )
        assert page.status_code == 200, page.text
        items = page.json()
        if not items:
            break
        collected.extend(row["event"]["id"] for row in items)
        before = items[-1]["event"]["starts_at"]
        before_id = items[-1]["event"]["id"]

    assert collected == created_ids


async def test_old_my_invites_route_still_works_unchanged(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user
) -> None:
    """c204 is additive: GET /me/event-invites keeps returning a bare EventOut list."""
    setup = await make_chapter_with("member")
    outsider = await _outsider(client, make_user, None, verified=False)

    created = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body(),
        headers=setup.president.headers,
    )
    assert created.status_code == 201, created.text
    event_id = created.json()["id"]
    invited = await client.post(
        f"/events/{event_id}/invites",
        json={"user_ids": [outsider.id]},
        headers=setup.president.headers,
    )
    assert invited.status_code == 201, invited.text

    listed = await client.get("/me/event-invites", headers=outsider.headers)
    assert listed.status_code == 200, listed.text
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0]["id"] == event_id
    # Bare EventOut - no my_rsvp_status, no hosted_by. The response shape of the
    # existing route must not change.
    assert set(rows[0].keys()) == {
        "id",
        "chapter_id",
        "title",
        "cover_url",
        "description",
        "starts_at",
        "ends_at",
        "location",
        "visibility",
        "canceled_at",
        "host_id",
        "created_at",
    }
