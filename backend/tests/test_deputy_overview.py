"""GET /chapters/{chapter_id}/deputy-overview (board card c163).

Jose's product ruling: the Vice President dashboard is DEPUTY PRESIDENT — a READ view
of president-admin data (roster, open invites, dues status), framed as a stand-in,
with delegation explicitly out of the alpha build.

These tests hold the lines a convenient-looking rewrite would cross without failing
anything else:

  * the gate is deputy_overview (vice_president + president), not members_admin and
    not "any e-board" (test_a_treasurer_cannot_read_it, test_a_secretary_cannot_read_it,
    test_a_historian_cannot_read_it)
  * the response has NO attendance/lineage keys at all — not empty, ABSENT — because
    the VP holds neither minutes_admin nor lineage_admin (test_the_response_has_no_
    attendance_or_lineage_fields)
  * the numbers agree exactly with chapter_overview's roster/dues/invites, since both
    read the same shared helpers (test_deputy_and_president_overviews_report_the_
    same_roster_dues_and_invites)
"""

from __future__ import annotations

from datetime import date

from httpx import AsyncClient

from tests.conftest import ChapterSetup, MakeChapterWith


async def _deputy_overview(client: AsyncClient, setup: ChapterSetup, headers: dict) -> dict:
    response = await client.get(
        f"/chapters/{setup.chapter_id}/deputy-overview", headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---- the gate ----


async def test_a_plain_member_cannot_read_it(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member")
    response = await client.get(
        f"/chapters/{setup.chapter_id}/deputy-overview", headers=setup.member.headers
    )
    assert response.status_code == 403, response.text


async def test_a_treasurer_cannot_read_it(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """dues_admin is not deputy_overview — a treasurer must not get this for free."""
    setup = await make_chapter_with("treasurer")
    response = await client.get(
        f"/chapters/{setup.chapter_id}/deputy-overview", headers=setup.member.headers
    )
    assert response.status_code == 403, response.text


async def test_a_secretary_cannot_read_it(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("secretary")
    response = await client.get(
        f"/chapters/{setup.chapter_id}/deputy-overview", headers=setup.member.headers
    )
    assert response.status_code == 403, response.text


async def test_a_historian_cannot_read_it(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Historian is EBOARD (moderation, lineage_admin) but not deputy_overview."""
    setup = await make_chapter_with("historian")
    response = await client.get(
        f"/chapters/{setup.chapter_id}/deputy-overview", headers=setup.member.headers
    )
    assert response.status_code == 403, response.text


async def test_the_vice_president_can_read_it(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("vice_president")
    payload = await _deputy_overview(client, setup, setup.member.headers)
    assert payload["roster"]["active"] == 2  # president + vice president
    assert payload["dues"]["cycle_id"] is None
    # make_chapter_with mints the VP's own join code, which is still live (unexpired,
    # unrevoked, under max_uses) — the deputy view is meant to see exactly that.
    assert payload["invites"]["live_codes"] == 1


async def test_the_president_can_also_read_it(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """President holds every capability, deputy_overview included — same invariant
    test_role_capabilities.py's test_president_holds_every_capability asserts."""
    setup = await make_chapter_with("vice_president")
    payload = await _deputy_overview(client, setup, setup.president.headers)
    assert payload["roster"]["active"] == 2


# ---- the shape ----


async def test_the_response_has_no_attendance_or_lineage_fields(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Not merely empty — ABSENT. chapter_overview's docstring explains why that
    payload is gated tighter than any single officer capability: it mixes attendance
    (Secretary's domain) and lineage (Historian's/e-board's). The VP holds neither
    minutes_admin nor lineage_admin, so this endpoint must never carry those keys."""
    setup = await make_chapter_with("vice_president")
    payload = await _deputy_overview(client, setup, setup.member.headers)
    assert "attendance" not in payload
    assert "lineage" not in payload
    assert set(payload.keys()) == {"chapter_id", "generated_at", "roster", "dues", "invites"}


# ---- agreement with the president's overview ----


async def test_deputy_and_president_overviews_report_the_same_roster_dues_and_invites(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Both dashboards read the same _roster_overview/_dues_overview/_invite_overview
    helpers, so a chapter with real activity must not be able to see two different
    dues pictures depending on which officer is looking."""
    setup = await make_chapter_with("vice_president")

    cycle = await client.post(
        f"/chapters/{setup.chapter_id}/dues-cycles",
        json={"name": "Fall 2026", "amount_cents": 10_000, "due_date": date(2026, 12, 1).isoformat()},
        headers=setup.president.headers,
    )
    assert cycle.status_code == 201, cycle.text
    paid = await client.post(
        f"/chapters/{setup.chapter_id}/ledger",
        json={
            "entry_type": "dues_payment",
            "amount_cents": 10_000,
            "related_user_id": setup.member.id,
            "dues_cycle_id": cycle.json()["id"],
        },
        headers=setup.president.headers,
    )
    assert paid.status_code == 201, paid.text
    invite = await client.post(
        f"/chapters/{setup.chapter_id}/invites",
        json={"role": "member", "max_uses": 4},
        headers=setup.president.headers,
    )
    assert invite.status_code == 201, invite.text

    president_view = await client.get(
        f"/chapters/{setup.chapter_id}/overview", headers=setup.president.headers
    )
    assert president_view.status_code == 200, president_view.text
    president_payload = president_view.json()

    deputy_payload = await _deputy_overview(client, setup, setup.member.headers)

    assert deputy_payload["roster"] == president_payload["roster"]
    assert deputy_payload["dues"] == president_payload["dues"]
    assert deputy_payload["invites"] == president_payload["invites"]
