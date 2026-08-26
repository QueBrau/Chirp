"""c194 falsify-first: a correction must not target another correction.

c191 audit finding 3 (moderate, manager-verified): finance.py's correction
validation checked corrects_entry_id exists + same-chapter, but never checked
the TARGET's entry_type. So C2 (correction) -> C1 (correction) -> P (dues
payment) was accepted with 201. dues_status.py's netting (dues_contributions_
subquery) joins corrections only directly against dues_payment rows, so a
correction-of-a-correction never matches that join and contributes nothing to
the sum: C2 had zero effect on the number it was meant to move, and the
member stayed on the outstanding list with no error surfaced anywhere.

This test builds exactly that chain against the real dues-standing read path
(GET /chapters/{id}/overview -> dues.paid_members/outstanding_members, the
same netting chapter_overview and payments.py's double-charge guard both
read) rather than asserting only the write-time status code, so a fix that
returns 422 but leaves the netting silently wrong would still fail this.
"""
from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import MakeChapterWith


async def test_correction_cannot_target_another_correction(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("treasurer")

    cycle = await client.post(
        f"/chapters/{setup.chapter_id}/dues-cycles",
        json={"name": "Fall Dues", "amount_cents": 10000, "due_date": "2026-12-01"},
        headers=setup.member.headers,
    )
    assert cycle.status_code == 201, cycle.text
    cycle_id = cycle.json()["id"]

    # P: the originating dues payment.
    payment = await client.post(
        f"/chapters/{setup.chapter_id}/ledger",
        json={
            "entry_type": "dues_payment",
            "amount_cents": 10000,
            "related_user_id": setup.member.id,
            "dues_cycle_id": cycle_id,
        },
        headers=setup.member.headers,
    )
    assert payment.status_code == 201, payment.text
    payment_id = payment.json()["id"]

    overview_paid = await client.get(
        f"/chapters/{setup.chapter_id}/overview", headers=setup.president.headers
    )
    assert overview_paid.status_code == 200, overview_paid.text
    # Roster is 2 (president + treasurer): the treasurer paid, the president
    # never has, so paid=1/outstanding=1 -- not paid=1/outstanding=0.
    assert overview_paid.json()["dues"]["paid_members"] == 1
    assert overview_paid.json()["dues"]["outstanding_members"] == 1

    # C1: a legitimate correction targeting the PAYMENT directly (full refund).
    # This must stay 201 and must net correctly: 10000 + (-10000) == 0.
    c1 = await client.post(
        f"/chapters/{setup.chapter_id}/ledger",
        json={
            "entry_type": "correction",
            "amount_cents": -10000,
            "description": "Full refund",
            "corrects_entry_id": payment_id,
        },
        headers=setup.member.headers,
    )
    assert c1.status_code == 201, c1.text
    c1_id = c1.json()["id"]

    overview_refunded = await client.get(
        f"/chapters/{setup.chapter_id}/overview", headers=setup.president.headers
    )
    assert overview_refunded.status_code == 200, overview_refunded.text
    assert overview_refunded.json()["dues"]["paid_members"] == 0, (
        "C1 -> P is a legitimate direct correction and must net: the member "
        "should now read as unpaid (refunded in full)."
    )
    assert overview_refunded.json()["dues"]["outstanding_members"] == 2

    # C2: a correction targeting C1 -- ANOTHER CORRECTION, not the payment.
    # THE BUG: this used to be accepted (201) and then silently never netted,
    # because dues_contributions_subquery's join only matches a correction
    # against a dues_payment row, never against another correction's id.
    c2 = await client.post(
        f"/chapters/{setup.chapter_id}/ledger",
        json={
            "entry_type": "correction",
            "amount_cents": 10000,
            "description": "Reinstating the refund by correcting the correction",
            "corrects_entry_id": c1_id,
        },
        headers=setup.member.headers,
    )
    assert c2.status_code == 422, (
        f"correction-of-a-correction must be rejected at write time, got "
        f"{c2.status_code}: {c2.text}"
    )
    assert c2.json()["detail"] == "correction_target_is_correction"

    # The rejected C2 must never have been persisted, so standing is exactly
    # what C1 alone produced -- no silent drift, no error left un-surfaced.
    overview_after_rejected_c2 = await client.get(
        f"/chapters/{setup.chapter_id}/overview", headers=setup.president.headers
    )
    assert overview_after_rejected_c2.status_code == 200
    assert overview_after_rejected_c2.json()["dues"]["paid_members"] == 0
    assert overview_after_rejected_c2.json()["dues"]["outstanding_members"] == 2

    ledger = await client.get(
        f"/chapters/{setup.chapter_id}/ledger", headers=setup.member.headers
    )
    assert ledger.status_code == 200
    assert {entry["id"] for entry in ledger.json()} == {payment_id, c1_id}, (
        "the rejected C2 must not appear in the ledger at all"
    )


async def test_correction_can_still_target_a_non_payment_entry(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Guardrail against over-fixing: SPEC.md 5 says corrections are new offsetting
    entries for ANY ledger row (not "any dues_payment row"), and
    test_ledger_append_only.py::test_correction_entry_references_original already
    exercises correcting an *expense* entry. The fix must reject a correction
    whose TARGET is itself a correction, without narrowing corrections to only
    ever target dues_payment rows -- that would be a real regression on an
    existing, tested, unrelated feature.
    """
    setup = await make_chapter_with("treasurer")

    expense = await client.post(
        f"/chapters/{setup.chapter_id}/ledger",
        json={"entry_type": "expense", "amount_cents": -8000, "category": "formal"},
        headers=setup.member.headers,
    )
    assert expense.status_code == 201, expense.text

    correction = await client.post(
        f"/chapters/{setup.chapter_id}/ledger",
        json={
            "entry_type": "correction",
            "amount_cents": 8000,
            "description": "Reversing duplicate expense",
            "corrects_entry_id": expense.json()["id"],
        },
        headers=setup.member.headers,
    )
    assert correction.status_code == 201, correction.text
