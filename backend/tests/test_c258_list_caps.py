"""c258 PR 1: bounded-by-construction list endpoints are capped, and never silently.

These endpoints return rows limited by something real - a chapter's roster, one
member's role history, one meeting's sheet - rather than by time, so they get a
generous hard cap instead of a cursor. Endpoints that grow with TIME (comments,
ledger, reports) are the opposite case and take real cursors in later PRs.

TWO THINGS ARE PROVEN PER ENDPOINT, because a cap that only ever truncates is as
wrong as no cap at all:
  1. over the cap, the response is bounded AND a warning is logged, so a truncation
     can never be silent;
  2. under the cap, every row still comes back - the ordinary case is untouched.

The caps are patched down to a small number rather than seeding 500 real members:
seeding the real ceiling would cost hundreds of bootstraps per assertion to prove
the same wiring. The REAL values are asserted separately in
test_the_shipped_caps_are_the_intended_values, so a silent change to either
constant fails here even though the behaviour tests run at a patched size.
"""

from __future__ import annotations

import logging

import pytest
from httpx import AsyncClient

from app.core.pagination import MAX_HISTORY_PAGE, MAX_ROSTER_PAGE
from tests.conftest import ApiUser, MakeChapterWith, MakeUser


async def _invite_and_join(
    client: AsyncClient,
    chapter_id: str,
    inviter_headers: dict[str, str],
    display_name: str,
    make_user: MakeUser,
) -> ApiUser:
    invite = await client.post(
        f"/chapters/{chapter_id}/invites", json={"role": "member"}, headers=inviter_headers
    )
    assert invite.status_code == 201, invite.text
    member = await make_user(display_name)
    joined = await client.post(
        "/chapters/join", json={"code": invite.json()["code"]}, headers=member.headers
    )
    assert joined.status_code == 201, joined.text
    return member


def _capped_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "hit its cap" in r.getMessage()]


# ---------------------------------------------------------------------------
# The shipped values
# ---------------------------------------------------------------------------


def test_the_shipped_caps_are_the_intended_values() -> None:
    """The behaviour tests below run at a patched size, so the real numbers are
    pinned here. MAX_ROSTER_PAGE is deliberately the SAME 500 c264 put on the write
    side: a read that could not return what a write was allowed to store would be its
    own bug, so the two ceilings are one number and changing either alone fails."""
    from app.schemas.meetings import MeetingAttendanceUpdate

    assert MAX_ROSTER_PAGE == 500
    assert MAX_HISTORY_PAGE == 200
    write_cap = MeetingAttendanceUpdate.model_fields["entries"].metadata
    assert any(getattr(m, "max_length", None) == MAX_ROSTER_PAGE for m in write_cap), (
        "the attendance WRITE cap and this READ cap must stay the same number"
    )


# ---------------------------------------------------------------------------
# GET /chapters/{chapter_id}/members  (roster-bounded)
# ---------------------------------------------------------------------------


async def test_member_list_is_capped_and_says_so(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    setup = await make_chapter_with("member")
    await _invite_and_join(client, setup.chapter_id, setup.president.headers, "Third", make_user)

    monkeypatch.setattr("app.routers.chapters.MAX_ROSTER_PAGE", 2)
    with caplog.at_level(logging.WARNING):
        response = await client.get(
            f"/chapters/{setup.chapter_id}/members", headers=setup.president.headers
        )

    assert response.status_code == 200, response.text
    assert len(response.json()) == 2, "the cap must bound the response"
    assert _capped_warnings(caplog), (
        "a truncated list MUST log - a silent cap is the bug this introduces"
    )


async def test_member_list_below_the_cap_returns_everyone(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """The ordinary case at the REAL cap: nothing is lost from a normal roster."""
    setup = await make_chapter_with("member")
    await _invite_and_join(client, setup.chapter_id, setup.president.headers, "Third", make_user)

    response = await client.get(
        f"/chapters/{setup.chapter_id}/members", headers=setup.president.headers
    )
    assert response.status_code == 200, response.text
    assert len(response.json()) == 3, "president + member + the third member"


# ---------------------------------------------------------------------------
# GET /chapters/{chapter_id}/invites  (history-bounded)
# ---------------------------------------------------------------------------


async def test_invite_list_is_capped_and_says_so(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    setup = await make_chapter_with("member")
    for _ in range(3):
        minted = await client.post(
            f"/chapters/{setup.chapter_id}/invites",
            json={"role": "member"},
            headers=setup.president.headers,
        )
        assert minted.status_code == 201, minted.text

    monkeypatch.setattr("app.routers.chapters.MAX_HISTORY_PAGE", 2)
    with caplog.at_level(logging.WARNING):
        response = await client.get(
            f"/chapters/{setup.chapter_id}/invites", headers=setup.president.headers
        )

    assert response.status_code == 200, response.text
    assert len(response.json()) == 2
    assert _capped_warnings(caplog)


async def test_invite_list_below_the_cap_returns_all_of_them(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member")
    # Counted from a baseline rather than hardcoded: make_chapter_with mints its own
    # invite to create the member, so asserting a literal here would be asserting the
    # fixture's internals and would break the next time it changes.
    baseline = await client.get(
        f"/chapters/{setup.chapter_id}/invites", headers=setup.president.headers
    )
    assert baseline.status_code == 200, baseline.text
    before = len(baseline.json())

    for _ in range(3):
        minted = await client.post(
            f"/chapters/{setup.chapter_id}/invites",
            json={"role": "member"},
            headers=setup.president.headers,
        )
        assert minted.status_code == 201, minted.text

    response = await client.get(
        f"/chapters/{setup.chapter_id}/invites", headers=setup.president.headers
    )
    assert response.status_code == 200, response.text
    assert len(response.json()) == before + 3, "every minted invite must still come back"


# ---------------------------------------------------------------------------
# GET /me/memberships
# ---------------------------------------------------------------------------


async def test_my_memberships_below_the_cap_returns_all_of_them(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member")
    response = await client.get("/me/memberships", headers=setup.member.headers)
    assert response.status_code == 200, response.text
    assert len(response.json()) == 1
