"""PATCH /chapters/{chapter_id} — chapter identity editing (board card c77).

Found by driving the real president screen, not by reading the router: the client
type (ChapterUpdate), the client function (updateChapter) and a real call site all
existed, and none of it worked, because the route itself was never wired up — every
call was a 405. The c77 card's own text said "updateChapter() is uncalled" as if the
endpoint just lacked a caller; it lacked an endpoint.

These tests pin the two things worth pinning: who may call it (president only, same
authorization as update_member), and the None-means-unchanged convention that keeps
this route consistent with update_member's existing pledge_class behavior rather than
inventing a second partial-update idiom for one endpoint.
"""

from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import MakeChapterWith


async def test_president_can_update_org_name(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("president")
    updated = await client.patch(
        f"/chapters/{setup.chapter_id}",
        json={"org_name": "Sigma Renamed"},
        headers=setup.president.headers,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["org_name"] == "Sigma Renamed"


async def test_president_can_set_chapter_name(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("president")
    updated = await client.patch(
        f"/chapters/{setup.chapter_id}",
        json={"chapter_name": "Beta"},
        headers=setup.president.headers,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["chapter_name"] == "Beta"


async def test_a_field_left_out_of_the_body_stays_unchanged(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The convention this route deliberately matches: omitting chapter_name must
    not touch it, exactly like update_member already treats pledge_class."""
    setup = await make_chapter_with("president")
    first = await client.patch(
        f"/chapters/{setup.chapter_id}",
        json={"chapter_name": "Alpha"},
        headers=setup.president.headers,
    )
    assert first.status_code == 200, first.text

    second = await client.patch(
        f"/chapters/{setup.chapter_id}",
        json={"org_name": "New Org Name"},
        headers=setup.president.headers,
    )
    assert second.status_code == 200, second.text
    assert second.json()["chapter_name"] == "Alpha", (
        "leaving chapter_name out of the body must not clear or touch it"
    )


async def test_plain_member_cannot_update_chapter_identity(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member")
    attempt = await client.patch(
        f"/chapters/{setup.chapter_id}",
        json={"org_name": "Hijacked"},
        headers=setup.member.headers,
    )
    assert attempt.status_code == 403, attempt.text


async def test_treasurer_cannot_update_chapter_identity(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Same authorization as update_member — MEMBERS_ADMIN is president-only,
    unlike DUES_ADMIN/MINUTES_ADMIN which also admit a second role."""
    setup = await make_chapter_with("treasurer")
    attempt = await client.patch(
        f"/chapters/{setup.chapter_id}",
        json={"org_name": "Hijacked"},
        headers=setup.member.headers,
    )
    assert attempt.status_code == 403, attempt.text


async def test_a_chapter_you_have_no_membership_in_is_403_not_404(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Not this route's own not_found check firing - require_role depends on
    get_current_membership, which looks up (chapter_id, caller) BEFORE this
    handler's body ever runs, and raises 403 for any chapter the caller has no
    active membership in. A chapter with zero members - including a genuinely
    nonexistent one - can never produce a membership row, so 403 is what every
    route gated this way returns here, not a 404. Matches get_chapter,
    update_member and every other Depends(require_role(...))/get_current_membership
    route in this file - checked against get_chapter's identical shape rather
    than assumed."""
    setup = await make_chapter_with("president")
    attempt = await client.patch(
        "/chapters/00000000-0000-0000-0000-000000000000",
        json={"org_name": "Nowhere"},
        headers=setup.president.headers,
    )
    assert attempt.status_code == 403, attempt.text
