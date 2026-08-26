"""Dues payment plans (board card c195): installment tracking against a dues cycle.

Money-invariant tests, same discipline as test_payments.py: installments must sum to
the cycle total, at most one active plan per member per cycle, recording an
installment appends a real ledger row and nets toward the member, and the self-serve
Stripe path (payments.py) must refuse a plan member rather than double-charge them.
"""
from __future__ import annotations

from typing import Any

from httpx import AsyncClient

from tests.conftest import ChapterSetup, MakeChapterWith


async def _create_dues_cycle(
    client: AsyncClient, setup: ChapterSetup, amount_cents: int = 30_000
) -> str:
    response = await client.post(
        f"/chapters/{setup.chapter_id}/dues-cycles",
        json={
            "name": "Fall 2027 Dues",
            "amount_cents": amount_cents,
            "due_date": "2027-11-01",
        },
        headers=setup.president.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _create_plan(
    client: AsyncClient,
    setup: ChapterSetup,
    cycle_id: str,
    user_id: str,
    installments: list[dict[str, Any]],
    note: str | None = None,
) -> Any:
    return await client.post(
        f"/chapters/{setup.chapter_id}/dues-cycles/{cycle_id}/plans",
        json={
            "user_id": user_id,
            "installment_count": len(installments),
            "note": note,
            "installments": installments,
        },
        headers=setup.president.headers,
    )


def _three_installments(total: int = 30_000) -> list[dict[str, Any]]:
    """3 installments summing to `total`: 10000/10000/10000 for the default 30000."""
    each = total // 3
    return [
        {"amount_cents": each, "due_date": "2027-09-01"},
        {"amount_cents": each, "due_date": "2027-10-01"},
        {"amount_cents": total - 2 * each, "due_date": "2027-11-01"},
    ]


async def _pay_on_ledger(
    client: AsyncClient, setup: ChapterSetup, cycle_id: str, user_id: str, cents: int
) -> str:
    response = await client.post(
        f"/chapters/{setup.chapter_id}/ledger",
        json={
            "entry_type": "dues_payment",
            "amount_cents": cents,
            "related_user_id": user_id,
            "dues_cycle_id": cycle_id,
        },
        headers=setup.president.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _record_payment(
    client: AsyncClient, setup: ChapterSetup, plan_id: str, seq: int, note: str | None = None
) -> Any:
    return await client.post(
        f"/chapters/{setup.chapter_id}/dues-plans/{plan_id}/installments/{seq}/record-payment",
        json={"note": note},
        headers=setup.president.headers,
    )


# ---------------------------------------------------------------------------
# Creating a plan
# ---------------------------------------------------------------------------


async def test_installments_must_sum_to_the_cycle_total(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """FALSIFY-FIRST: a schedule that does not add up to the cycle amount is 422,
    never silently accepted at the wrong total."""
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=30_000)

    response = await _create_plan(
        client,
        setup,
        cycle_id,
        setup.member.id,
        [
            {"amount_cents": 10_000, "due_date": "2027-09-01"},
            {"amount_cents": 10_000, "due_date": "2027-10-01"},
            # Off by 1000 — should NOT be accepted.
            {"amount_cents": 9_000, "due_date": "2027-11-01"},
        ],
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "installments_must_sum_to_cycle_amount"


async def test_installment_count_must_match_the_schedule_length(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=30_000)

    response = await client.post(
        f"/chapters/{setup.chapter_id}/dues-cycles/{cycle_id}/plans",
        json={
            "user_id": setup.member.id,
            "installment_count": 4,  # claims 4, sends 3
            "installments": _three_installments(),
        },
        headers=setup.president.headers,
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "installment_count_mismatch"


async def test_a_correctly_summed_plan_is_created_with_ordered_installments(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=30_000)

    response = await _create_plan(
        client, setup, cycle_id, setup.member.id, _three_installments()
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "active"
    assert body["total_cents"] == 30_000
    assert [i["seq"] for i in body["installments"]] == [1, 2, 3]
    assert sum(i["amount_cents"] for i in body["installments"]) == 30_000
    assert all(i["paid_at"] is None for i in body["installments"])


async def test_at_most_one_active_plan_per_member_per_cycle(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """FALSIFY-FIRST: a second plan for the same (cycle, member) while the first is
    still active is refused — uq_dues_payment_plans_active_per_member (migration
    0023) is the real guard; this exercises the app-level 409 in front of it."""
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=30_000)

    first = await _create_plan(
        client, setup, cycle_id, setup.member.id, _three_installments()
    )
    assert first.status_code == 201, first.text

    second = await _create_plan(
        client, setup, cycle_id, setup.member.id, _three_installments()
    )
    assert second.status_code == 409, second.text
    assert second.json()["detail"] == "on_payment_plan"


async def test_a_member_who_already_paid_in_full_cannot_get_a_plan(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=30_000)
    await _pay_on_ledger(client, setup, cycle_id, setup.member.id, 30_000)

    response = await _create_plan(
        client, setup, cycle_id, setup.member.id, _three_installments()
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "already_paid"


async def test_plan_creation_is_dues_admin_gated(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=30_000)

    response = await client.post(
        f"/chapters/{setup.chapter_id}/dues-cycles/{cycle_id}/plans",
        json={
            "user_id": setup.member.id,
            "installment_count": 3,
            "installments": _three_installments(),
        },
        headers=setup.member.headers,  # plain member, not treasurer/president
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Recording installments
# ---------------------------------------------------------------------------


async def test_recording_an_installment_appends_a_dues_installment_ledger_row(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=30_000)
    plan = (
        await _create_plan(client, setup, cycle_id, setup.member.id, _three_installments())
    ).json()

    response = await _record_payment(client, setup, plan["id"], 1, note="cash")
    assert response.status_code == 200, response.text
    installment = response.json()
    assert installment["paid_at"] is not None
    assert installment["ledger_entry_id"] is not None

    entries = await client.get(
        f"/chapters/{setup.chapter_id}/ledger", headers=setup.president.headers
    )
    dues_installments = [
        e for e in entries.json() if e["entry_type"] == "dues_installment"
    ]
    assert len(dues_installments) == 1
    assert dues_installments[0]["id"] == installment["ledger_entry_id"]
    assert dues_installments[0]["amount_cents"] == 10_000
    assert dues_installments[0]["related_user_id"] == setup.member.id
    assert dues_installments[0]["dues_cycle_id"] == cycle_id


async def test_recording_an_installment_nets_toward_the_member(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """FALSIFY-FIRST: dues_contributions_subquery must count 'dues_installment' rows
    alongside 'dues_payment' — verified indirectly through chapter_overview's net-
    derived paid/outstanding split before a plan is even involved on that member."""
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=30_000)
    plan = (
        await _create_plan(client, setup, cycle_id, setup.member.id, _three_installments())
    ).json()
    await _record_payment(client, setup, plan["id"], 1)
    await _record_payment(client, setup, plan["id"], 2)

    overview = await client.get(
        f"/chapters/{setup.chapter_id}/overview", headers=setup.president.headers
    )
    assert overview.status_code == 200, overview.text
    # 20,000 of 30,000 collected so far, both installments landed for this member.
    assert overview.json()["dues"]["collected_cents"] == 20_000


async def test_recording_the_last_installment_completes_the_plan(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=30_000)
    plan = (
        await _create_plan(client, setup, cycle_id, setup.member.id, _three_installments())
    ).json()

    await _record_payment(client, setup, plan["id"], 1)
    await _record_payment(client, setup, plan["id"], 2)

    mid = await client.get(
        f"/chapters/{setup.chapter_id}/dues-cycles/{cycle_id}/plans/mine",
        headers=setup.member.headers,
    )
    assert mid.json()["status"] == "active"

    await _record_payment(client, setup, plan["id"], 3)

    done = await client.get(
        f"/chapters/{setup.chapter_id}/dues-cycles/{cycle_id}/plans/mine",
        headers=setup.member.headers,
    )
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "completed"
    assert all(i["paid_at"] is not None for i in done.json()["installments"])


async def test_recording_an_already_paid_installment_is_409(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """FALSIFY-FIRST: the conditional UPDATE (paid_at IS NULL) is the guard — a
    second record-payment call for the same slot must not post a second ledger row."""
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=30_000)
    plan = (
        await _create_plan(client, setup, cycle_id, setup.member.id, _three_installments())
    ).json()

    first = await _record_payment(client, setup, plan["id"], 1)
    assert first.status_code == 200, first.text

    second = await _record_payment(client, setup, plan["id"], 1)
    assert second.status_code == 409, second.text
    assert second.json()["detail"] == "installment_already_paid"

    entries = await client.get(
        f"/chapters/{setup.chapter_id}/ledger", headers=setup.president.headers
    )
    assert len([e for e in entries.json() if e["entry_type"] == "dues_installment"]) == 1


# ---------------------------------------------------------------------------
# The self-serve guard (payments.py) — the invariant this whole card protects
# ---------------------------------------------------------------------------


async def test_a_member_on_an_active_plan_gets_on_payment_plan_from_the_charge_path(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """FALSIFY-FIRST: create_dues_payment_intent must refuse a plan member with the
    specific reason, before the generic already_paid/refunded_contact_treasurer split
    — proven by asserting the SPECIFIC detail string, not just a bare 409."""
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=30_000)
    await _create_plan(client, setup, cycle_id, setup.member.id, _three_installments())

    response = await client.post(
        f"/payments/dues/{cycle_id}/intent",
        json={"rail": "card"},
        headers=setup.member.headers,
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "on_payment_plan"


async def test_a_member_who_completed_a_plan_cannot_double_pay_through_the_charge_path(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """FALSIFY-FIRST, closes the gap a literal reading of the design leaves open: once
    a plan is COMPLETED it is no longer 'active', so the on_payment_plan check alone
    would not catch it — and the member has entry_type='dues_installment' rows, never
    a 'dues_payment' row, so the ORIGINAL existence check (filtered to 'dues_payment'
    only) would not catch it either. Without extending that filter to include
    'dues_installment', this call would reach Stripe and mint a second, full-price
    PaymentIntent for a cycle already paid off in installments — the exact double-
    charge shape c193 exists to prevent, reopened by a different door.

    No Stripe mocking needed: both guards raise before create_dues_payment_intent
    ever calls stripe_service, so the chapter is deliberately left un-onboarded here
    — proof that the block happens before any Stripe interaction, not as a side
    effect of one.
    """
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=30_000)
    plan = (
        await _create_plan(client, setup, cycle_id, setup.member.id, _three_installments())
    ).json()
    await _record_payment(client, setup, plan["id"], 1)
    await _record_payment(client, setup, plan["id"], 2)
    await _record_payment(client, setup, plan["id"], 3)

    finished = await client.get(
        f"/chapters/{setup.chapter_id}/dues-cycles/{cycle_id}/plans/mine",
        headers=setup.member.headers,
    )
    assert finished.json()["status"] == "completed"

    response = await client.post(
        f"/payments/dues/{cycle_id}/intent",
        json={"rail": "card"},
        headers=setup.member.headers,
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "already_paid"


# ---------------------------------------------------------------------------
# chapter_overview: on_plan vs paid vs outstanding
# ---------------------------------------------------------------------------


async def test_an_active_plan_member_is_on_plan_not_outstanding(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """FALSIFY-FIRST: before c195's three-way split, a plan member with $0 collected
    so far reads as net<=0 and would fall straight into outstanding — indistinguishable
    from a member who has not engaged at all."""
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=30_000)
    await _create_plan(client, setup, cycle_id, setup.member.id, _three_installments())

    overview = await client.get(
        f"/chapters/{setup.chapter_id}/overview", headers=setup.president.headers
    )
    assert overview.status_code == 200, overview.text
    dues = overview.json()["dues"]
    assert dues["on_plan_members"] == 1
    assert dues["outstanding_members"] == 1  # the president, who has no plan
    assert dues["paid_members"] == 0
    assert dues["on_plan_members"] + dues["outstanding_members"] + dues["paid_members"] == 2


async def test_an_active_plan_member_with_a_partial_payment_stays_on_plan_not_paid(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The subtle case: net > 0 (one installment landed) but the plan has not
    completed — must stay on_plan, not jump to paid the way a lump-sum partial
    refund would under the pre-c195 net>0 rule."""
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=30_000)
    plan = (
        await _create_plan(client, setup, cycle_id, setup.member.id, _three_installments())
    ).json()
    await _record_payment(client, setup, plan["id"], 1)

    overview = await client.get(
        f"/chapters/{setup.chapter_id}/overview", headers=setup.president.headers
    )
    dues = overview.json()["dues"]
    assert dues["on_plan_members"] == 1
    assert dues["paid_members"] == 0


async def test_a_completed_plan_member_counts_as_paid(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=30_000)
    plan = (
        await _create_plan(client, setup, cycle_id, setup.member.id, _three_installments())
    ).json()
    for seq in (1, 2, 3):
        await _record_payment(client, setup, plan["id"], seq)

    overview = await client.get(
        f"/chapters/{setup.chapter_id}/overview", headers=setup.president.headers
    )
    dues = overview.json()["dues"]
    assert dues["paid_members"] == 1
    assert dues["on_plan_members"] == 0
    assert dues["outstanding_members"] == 1  # the president, unaffected


async def test_roster_spined_dues_counts_still_sum_to_active_roster(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Regression guard on the invariant test_paid_plus_outstanding_always_equals_active
    already protects, extended to the new third bucket."""
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=30_000)
    await _create_plan(client, setup, cycle_id, setup.member.id, _three_installments())

    overview = await client.get(
        f"/chapters/{setup.chapter_id}/overview", headers=setup.president.headers
    )
    roster = overview.json()["roster"]
    dues = overview.json()["dues"]
    assert (
        dues["paid_members"] + dues["on_plan_members"] + dues["outstanding_members"]
        == roster["active"]
    )


# ---------------------------------------------------------------------------
# Cancel + reads
# ---------------------------------------------------------------------------


async def test_canceling_a_plan_frees_the_member_for_a_new_one(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=30_000)
    plan = (
        await _create_plan(client, setup, cycle_id, setup.member.id, _three_installments())
    ).json()

    canceled = await client.post(
        f"/chapters/{setup.chapter_id}/dues-plans/{plan['id']}/cancel",
        headers=setup.president.headers,
    )
    assert canceled.status_code == 200, canceled.text
    assert canceled.json()["status"] == "canceled"

    second = await _create_plan(
        client, setup, cycle_id, setup.member.id, _three_installments()
    )
    assert second.status_code == 201, second.text


async def test_canceling_a_non_active_plan_is_409(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=30_000)
    plan = (
        await _create_plan(client, setup, cycle_id, setup.member.id, _three_installments())
    ).json()
    await client.post(
        f"/chapters/{setup.chapter_id}/dues-plans/{plan['id']}/cancel",
        headers=setup.president.headers,
    )

    second_cancel = await client.post(
        f"/chapters/{setup.chapter_id}/dues-plans/{plan['id']}/cancel",
        headers=setup.president.headers,
    )
    assert second_cancel.status_code == 409
    assert second_cancel.json()["detail"] == "plan_not_active"


async def test_get_my_plan_404s_before_a_plan_exists(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=30_000)

    response = await client.get(
        f"/chapters/{setup.chapter_id}/dues-cycles/{cycle_id}/plans/mine",
        headers=setup.member.headers,
    )
    assert response.status_code == 404


async def test_list_plans_is_dues_admin_gated_and_get_mine_is_not(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=30_000)
    await _create_plan(client, setup, cycle_id, setup.member.id, _three_installments())

    listed = await client.get(
        f"/chapters/{setup.chapter_id}/dues-cycles/{cycle_id}/plans",
        headers=setup.member.headers,  # plain member
    )
    assert listed.status_code == 403

    mine = await client.get(
        f"/chapters/{setup.chapter_id}/dues-cycles/{cycle_id}/plans/mine",
        headers=setup.member.headers,
    )
    assert mine.status_code == 200, mine.text
    assert mine.json()["user_id"] == setup.member.id

    admin_listed = await client.get(
        f"/chapters/{setup.chapter_id}/dues-cycles/{cycle_id}/plans",
        headers=setup.president.headers,
    )
    assert admin_listed.status_code == 200
    assert len(admin_listed.json()) == 1
