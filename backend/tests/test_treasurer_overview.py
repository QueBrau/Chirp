"""GET /chapters/{chapter_id}/treasurer-overview (board card c278).

Jose's product ruling (board decisions log, Sep 4): the treasurer gets a SCOPED read
endpoint carrying the dues section only, rather than a widened chapter_overview gate —
the same capability logic as the c163 deputy-overview ruling: chapter_overview's
payload mixes attendance (Secretary's domain) and lineage (Historian's/e-board's),
and dues_admin grants a claim to neither.

These tests hold the lines a convenient-looking rewrite would cross without failing
anything else:

  * the gate is dues_admin (treasurer + president), not deputy_overview and not "any
    e-board" (test_a_vice_president_cannot_read_it — the exact mirror of
    test_deputy_overview.py's test_a_treasurer_cannot_read_it — plus secretary and
    historian refusals)
  * the response carries ONLY the dues section — roster/invites/attendance/lineage
    are ABSENT keys, not empty ones (test_the_response_is_dues_only)
  * the numbers agree exactly with chapter_overview's dues, since both read the same
    _dues_overview helper (test_treasurer_and_president_report_the_same_dues) — the
    c258 lesson, where a client-side recomputation missed installment payers and
    displayed refunded money as collected
"""

from __future__ import annotations

from datetime import date

from httpx import AsyncClient

from tests.conftest import ChapterSetup, MakeChapterWith


async def _treasurer_overview(
    client: AsyncClient, setup: ChapterSetup, headers: dict
) -> dict:
    response = await client.get(
        f"/chapters/{setup.chapter_id}/treasurer-overview", headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---- the gate ----


async def test_a_plain_member_cannot_read_it(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member")
    response = await client.get(
        f"/chapters/{setup.chapter_id}/treasurer-overview", headers=setup.member.headers
    )
    assert response.status_code == 403, response.text


async def test_a_vice_president_cannot_read_it(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """deputy_overview is not dues_admin — the VP already has their own read at
    /deputy-overview and must not get this one for free, exactly as the treasurer
    does not get theirs."""
    setup = await make_chapter_with("vice_president")
    response = await client.get(
        f"/chapters/{setup.chapter_id}/treasurer-overview", headers=setup.member.headers
    )
    assert response.status_code == 403, response.text


async def test_a_secretary_cannot_read_it(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("secretary")
    response = await client.get(
        f"/chapters/{setup.chapter_id}/treasurer-overview", headers=setup.member.headers
    )
    assert response.status_code == 403, response.text


async def test_a_historian_cannot_read_it(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Historian is EBOARD (moderation, lineage_admin) but not dues_admin."""
    setup = await make_chapter_with("historian")
    response = await client.get(
        f"/chapters/{setup.chapter_id}/treasurer-overview", headers=setup.member.headers
    )
    assert response.status_code == 403, response.text


async def test_the_treasurer_can_read_it(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The card itself: the officer who owns the dues number can finally read the
    authoritative computation of it."""
    setup = await make_chapter_with("treasurer")
    payload = await _treasurer_overview(client, setup, setup.member.headers)
    assert payload["dues"]["cycle_id"] is None  # new chapter, no cycle yet — a real state


async def test_the_president_can_also_read_it(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """President holds every capability, dues_admin included — same invariant
    test_role_capabilities.py's test_president_holds_every_capability asserts."""
    setup = await make_chapter_with("treasurer")
    payload = await _treasurer_overview(client, setup, setup.president.headers)
    assert payload["dues"]["cycle_id"] is None


# ---- the shape ----


async def test_the_response_is_dues_only(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Not merely empty — ABSENT. dues_admin grants no claim to roster, invites,
    attendance, or lineage, so this endpoint must never carry those keys."""
    setup = await make_chapter_with("treasurer")
    payload = await _treasurer_overview(client, setup, setup.member.headers)
    assert set(payload.keys()) == {"chapter_id", "generated_at", "dues"}


# ---- agreement with the president's overview ----


async def test_treasurer_and_president_report_the_same_dues(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Both read _dues_overview, so a chapter with a live cycle, a lump-sum payment,
    and real activity must not be able to show the treasurer and the president two
    different dues pictures."""
    setup = await make_chapter_with("treasurer")

    cycle = await client.post(
        f"/chapters/{setup.chapter_id}/dues-cycles",
        json={
            "name": "Fall 2026",
            "amount_cents": 10_000,
            "due_date": date(2026, 12, 1).isoformat(),
        },
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

    president_view = await client.get(
        f"/chapters/{setup.chapter_id}/overview", headers=setup.president.headers
    )
    assert president_view.status_code == 200, president_view.text

    treasurer_payload = await _treasurer_overview(client, setup, setup.member.headers)

    assert treasurer_payload["dues"] == president_view.json()["dues"]
