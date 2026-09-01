"""Dues payment plans (board card c195): installment tracking against a dues cycle.

Money-invariant tests, same discipline as test_payments.py: installments must sum to
the cycle total, at most one active plan per member per cycle, recording an
installment appends a real ledger row and nets toward the member, and the self-serve
Stripe path (payments.py) must refuse a plan member rather than double-charge them.
"""
from __future__ import annotations

from typing import Any

from httpx import AsyncClient
from sqlalchemy import text as sa_text

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


async def _correct(
    client: AsyncClient, setup: ChapterSetup, entry_id: str, cents: int
) -> None:
    """Append a correction against a prior ledger entry (SPEC 8.2) — a refund is a
    NEW row, never an update to the original. Same helper shape as test_payments.py's."""
    response = await client.post(
        f"/chapters/{setup.chapter_id}/ledger",
        json={
            "entry_type": "correction",
            "amount_cents": cents,
            "corrects_entry_id": entry_id,
        },
        headers=setup.president.headers,
    )
    assert response.status_code == 201, response.text


async def _open_reservation(
    chapter_id: str, cycle_id: str, user_id: str, rail: str = "card"
) -> None:
    """Insert an OPEN dues_payment_intents row directly — the c51 reservation a
    real in-flight Stripe payment leaves behind, WITHOUT going through Stripe. No
    API constructs this state on its own (same reasoning as conftest's
    _grant_platform_admin/set_campus: some fixtures only exist as direct writes)."""
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        await session.execute(
            sa_text(
                "INSERT INTO dues_payment_intents "
                "(chapter_id, dues_cycle_id, user_id, rail, status) "
                "VALUES (:chapter_id, :cycle_id, :user_id, :rail, 'open')"
            ),
            {
                "chapter_id": chapter_id,
                "cycle_id": cycle_id,
                "user_id": user_id,
                "rail": rail,
            },
        )
        await session.commit()


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


# ---------------------------------------------------------------------------
# Adversarial pre-merge review gaps (see board thread) — falsify-first
# ---------------------------------------------------------------------------


async def test_recording_an_installment_against_a_canceled_plan_is_409(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """GAP 1 (HIGH). Canceling a plan must stop its installments from being
    recordable. Before this guard, a canceled plan's installments are still
    paid_at NULL and fully recordable: recording them posts dues_installment
    ledger rows the cancellation was meant to stop, and once the last one is
    recorded the plan's status is flipped back to 'completed' — resurrecting a
    plan the treasurer explicitly killed.
    """
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

    response = await _record_payment(client, setup, plan["id"], 1)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "plan_not_active"

    entries = await client.get(
        f"/chapters/{setup.chapter_id}/ledger", headers=setup.president.headers
    )
    assert not [e for e in entries.json() if e["entry_type"] == "dues_installment"]

    still_canceled = await client.get(
        f"/chapters/{setup.chapter_id}/dues-cycles/{cycle_id}/plans/mine",
        headers=setup.member.headers,
    )
    assert still_canceled.json()["status"] == "canceled"


async def test_recording_an_installment_against_a_completed_plan_is_409_plan_not_active(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """GAP 1, continued: recording against an already-COMPLETED plan must also be
    refused by the same status guard, with the specific plan_not_active reason —
    not merely fall through to the generic installment_already_paid 409 that the
    conditional-UPDATE guard happens to also produce for a fully-paid plan.
    """
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=30_000)
    plan = (
        await _create_plan(client, setup, cycle_id, setup.member.id, _three_installments())
    ).json()
    for seq in (1, 2, 3):
        await _record_payment(client, setup, plan["id"], seq)

    response = await _record_payment(client, setup, plan["id"], 1)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "plan_not_active"


async def test_a_member_who_completed_a_plan_cannot_get_a_second_plan(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """GAP 2 (MODERATE). create_dues_payment_plan's already-paid guard was filtered
    to entry_type=='dues_payment' only. A member who COMPLETED a plan has only
    entry_type='dues_installment' rows on the ledger, so that guard never saw them
    and a SECOND plan could be created for someone already fully paid.
    """
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=30_000)
    plan = (
        await _create_plan(client, setup, cycle_id, setup.member.id, _three_installments())
    ).json()
    for seq in (1, 2, 3):
        await _record_payment(client, setup, plan["id"], seq)

    second = await _create_plan(
        client, setup, cycle_id, setup.member.id, _three_installments()
    )
    assert second.status_code == 409, second.text
    assert second.json()["detail"] == "already_paid"


async def test_a_live_self_serve_reservation_blocks_plan_creation(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """GAP 3 (MODERATE). A member with an in-flight self-serve payment — an OPEN
    DuesPaymentIntent reservation (e.g. an ACH debit still processing) — has NO
    ledger row yet, so none of create_dues_payment_plan's existing guards (paid
    existence, active-plan existence) can see them. Without this check, a plan
    gets created underneath the in-flight payment; the ACH later settles into a
    full dues_payment AND the treasurer keeps recording installments on top of
    it, over-collecting the cycle.
    """
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=30_000)
    await _open_reservation(setup.chapter_id, cycle_id, setup.member.id)

    response = await _create_plan(
        client, setup, cycle_id, setup.member.id, _three_installments()
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "payment_in_progress"


async def test_a_succeeded_reservation_also_blocks_plan_creation(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The reservation guard must match payments.py's own status set — 'open' AND
    'succeeded', not just 'open' — mirroring uq_dues_intent_live's own coverage."""
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=30_000)
    await _open_reservation(setup.chapter_id, cycle_id, setup.member.id)
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        await session.execute(
            sa_text(
                "UPDATE dues_payment_intents SET status = 'succeeded' "
                "WHERE dues_cycle_id = :cycle_id AND user_id = :user_id"
            ),
            {"cycle_id": cycle_id, "user_id": setup.member.id},
        )
        await session.commit()

    response = await _create_plan(
        client, setup, cycle_id, setup.member.id, _three_installments()
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "payment_in_progress"


async def test_a_completed_then_fully_refunded_plan_member_is_not_reported_paid(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """GAP 4 (MODERATE). The old classification OR'd in plan_status=='completed' as
    an INDEPENDENT path to 'paid' alongside net>=total. That is a LATCH: once a
    plan completes, the member reads as paid forever, even after every
    installment's ledger row is corrected away to zero — while collected_cents and
    the self-serve pay-guard both correctly say the member owes again. Paid must
    be decided on net alone (net >= total, or the plain net>0 partial-refund-still-
    paid rule for everyone else), never latched by a stale plan status.
    """
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=30_000)
    plan = (
        await _create_plan(client, setup, cycle_id, setup.member.id, _three_installments())
    ).json()
    for seq in (1, 2, 3):
        await _record_payment(client, setup, plan["id"], seq)

    entries = await client.get(
        f"/chapters/{setup.chapter_id}/ledger", headers=setup.president.headers
    )
    installment_entries = [
        e
        for e in entries.json()
        if e["entry_type"] == "dues_installment"
        and e["related_user_id"] == setup.member.id
    ]
    assert len(installment_entries) == 3
    for entry in installment_entries:
        await _correct(client, setup, entry["id"], -entry["amount_cents"])

    overview = await client.get(
        f"/chapters/{setup.chapter_id}/overview", headers=setup.president.headers
    )
    assert overview.status_code == 200, overview.text
    dues = overview.json()["dues"]
    assert dues["paid_members"] == 0
    assert dues["on_plan_members"] == 0  # the plan is 'completed', not 'active'
    assert dues["outstanding_members"] == 2  # the refunded member + the president


# ---------------------------------------------------------------------------
# c224 adversarial sweep follow-ups (board cards c232, c233) — falsify-first
# ---------------------------------------------------------------------------


async def test_a_generic_dues_payment_against_an_active_plan_member_is_409(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """FIX 1 (c232, HIGH). create_ledger_entry accepted entry_type='dues_payment'
    for a member on an ACTIVE payment plan with no plan-awareness at all — a
    treasurer hand-entering a check against dues would double-collect a plan
    member silently, since the plan's own installments already account for the
    full cycle amount. Mirrors payments.py's create_dues_payment_intent guard
    (c195) from the manual-entry side.
    """
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=30_000)
    await _create_plan(client, setup, cycle_id, setup.member.id, _three_installments())

    response = await client.post(
        f"/chapters/{setup.chapter_id}/ledger",
        json={
            "entry_type": "dues_payment",
            "amount_cents": 30_000,
            "related_user_id": setup.member.id,
            "dues_cycle_id": cycle_id,
        },
        headers=setup.president.headers,
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "member_on_payment_plan"

    entries = await client.get(
        f"/chapters/{setup.chapter_id}/ledger", headers=setup.president.headers
    )
    assert not [
        e
        for e in entries.json()
        if e["entry_type"] == "dues_payment" and e["related_user_id"] == setup.member.id
    ]


async def test_a_manual_dues_installment_ledger_entry_is_always_422(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """FIX 1 (c232, HIGH), policy decision: entry_type='dues_installment' is
    rejected at this generic route UNCONDITIONALLY -- with or without an active
    plan in play -- not merely when it collides with one. The only coherent way
    an installment ledger row comes into existence is
    record_dues_installment_payment, which stamps ONE specific plan installment's
    paid_at/ledger_entry_id in the same transaction; a dues_installment row
    created here would have no seq or plan_id for FIX 3's read-path netting to
    attach it to -- exactly the plan/ledger incoherence c233 fights. Checked with
    no plan in existence at all, and again once an active one does, to pin that
    this is a structural rejection, not a plan-conflict one.
    """
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=30_000)

    no_plan_yet = await client.post(
        f"/chapters/{setup.chapter_id}/ledger",
        json={
            "entry_type": "dues_installment",
            "amount_cents": 10_000,
            "related_user_id": setup.member.id,
            "dues_cycle_id": cycle_id,
        },
        headers=setup.president.headers,
    )
    assert no_plan_yet.status_code == 422, no_plan_yet.text
    assert no_plan_yet.json()["detail"] == "dues_installment_requires_plan_route"

    await _create_plan(client, setup, cycle_id, setup.member.id, _three_installments())

    with_active_plan = await client.post(
        f"/chapters/{setup.chapter_id}/ledger",
        json={
            "entry_type": "dues_installment",
            "amount_cents": 10_000,
            "related_user_id": setup.member.id,
            "dues_cycle_id": cycle_id,
        },
        headers=setup.president.headers,
    )
    assert with_active_plan.status_code == 422, with_active_plan.text
    assert with_active_plan.json()["detail"] == "dues_installment_requires_plan_route"


async def test_a_completed_then_fully_refunded_plan_member_can_get_a_fresh_plan(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """FIX 2 (c233a, MED-HIGH). create_dues_payment_plan's already-paid pre-check
    was raw row EXISTENCE, so a fully-refunded member could never get a new plan
    (409 already_paid forever) even though dues_status.py's own netting
    definition -- and the President overview -- both already agree they hold no
    money and owe again. Same corrected-then-refunded sequence as GAP 4 above;
    net reaches exactly 0, so a fresh plan must succeed with 201.
    """
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=30_000)
    plan = (
        await _create_plan(client, setup, cycle_id, setup.member.id, _three_installments())
    ).json()
    for seq in (1, 2, 3):
        await _record_payment(client, setup, plan["id"], seq)

    entries = await client.get(
        f"/chapters/{setup.chapter_id}/ledger", headers=setup.president.headers
    )
    installment_entries = [
        e
        for e in entries.json()
        if e["entry_type"] == "dues_installment"
        and e["related_user_id"] == setup.member.id
    ]
    assert len(installment_entries) == 3
    for entry in installment_entries:
        await _correct(client, setup, entry["id"], -entry["amount_cents"])

    fresh = await _create_plan(
        client, setup, cycle_id, setup.member.id, _three_installments()
    )
    assert fresh.status_code == 201, fresh.text


async def test_a_net_positive_partial_refund_still_blocks_a_new_plan(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """FIX 2 (c233a) pin: netting is not the same as never blocking. A member
    refunded only PART of a lump-sum dues_payment still has a positive net and
    must still 409 already_paid -- the fix only reopens the FULLY-refunded (net
    <= 0) case, the same net > 0 threshold payments.py's own already_paid vs.
    refunded_contact_treasurer split hinges on.
    """
    setup = await make_chapter_with(role="member")
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=30_000)
    entry_id = await _pay_on_ledger(client, setup, cycle_id, setup.member.id, 30_000)
    await _correct(client, setup, entry_id, -5_000)  # net = 25,000 > 0

    response = await _create_plan(
        client, setup, cycle_id, setup.member.id, _three_installments()
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "already_paid"


async def test_plans_mine_and_treasurer_list_reflect_corrections_via_effective_paid(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """FIX 3 (c233b, HIGH). _plan_out/_load_installments mirrored the write-once
    paid_at/ledger_entry_id columns verbatim, so after GAP 4's corrections reverse
    the money, plans/mine and the treasurer's plan list both kept showing every
    installment paid even though the overview (and dues_contributions_subquery)
    say outstanding. effective_paid must flip to False for a corrected
    installment while paid_at (write-once, historical) stays exactly as it was.
    A second, UNTOUCHED plan for the same member on a different cycle is the
    control: it must still read effective_paid=True throughout, proving the
    derived field isn't just globally false.
    """
    setup = await make_chapter_with(role="member")

    # Plan A: paid in full, then every installment corrected away to zero.
    cycle_a = await _create_dues_cycle(client, setup, amount_cents=30_000)
    plan_a = (
        await _create_plan(client, setup, cycle_a, setup.member.id, _three_installments())
    ).json()
    for seq in (1, 2, 3):
        await _record_payment(client, setup, plan_a["id"], seq)
    entries = await client.get(
        f"/chapters/{setup.chapter_id}/ledger", headers=setup.president.headers
    )
    plan_a_entries = [
        e
        for e in entries.json()
        if e["entry_type"] == "dues_installment" and e["dues_cycle_id"] == cycle_a
    ]
    assert len(plan_a_entries) == 3
    for entry in plan_a_entries:
        await _correct(client, setup, entry["id"], -entry["amount_cents"])

    # Plan B: paid in full, untouched -- the "still reads paid" control.
    cycle_b = await _create_dues_cycle(client, setup, amount_cents=30_000)
    plan_b = (
        await _create_plan(client, setup, cycle_b, setup.member.id, _three_installments())
    ).json()
    for seq in (1, 2, 3):
        await _record_payment(client, setup, plan_b["id"], seq)

    mine_a = await client.get(
        f"/chapters/{setup.chapter_id}/dues-cycles/{cycle_a}/plans/mine",
        headers=setup.member.headers,
    )
    assert mine_a.status_code == 200, mine_a.text
    assert len(mine_a.json()["installments"]) == 3
    for installment in mine_a.json()["installments"]:
        assert installment["paid_at"] is not None
        assert installment["effective_paid"] is False

    mine_b = await client.get(
        f"/chapters/{setup.chapter_id}/dues-cycles/{cycle_b}/plans/mine",
        headers=setup.member.headers,
    )
    assert mine_b.status_code == 200, mine_b.text
    assert len(mine_b.json()["installments"]) == 3
    for installment in mine_b.json()["installments"]:
        assert installment["paid_at"] is not None
        assert installment["effective_paid"] is True

    list_a = await client.get(
        f"/chapters/{setup.chapter_id}/dues-cycles/{cycle_a}/plans",
        headers=setup.president.headers,
    )
    assert list_a.status_code == 200, list_a.text
    [treasurer_plan_a] = list_a.json()
    assert len(treasurer_plan_a["installments"]) == 3
    for installment in treasurer_plan_a["installments"]:
        assert installment["paid_at"] is not None
        assert installment["effective_paid"] is False

    list_b = await client.get(
        f"/chapters/{setup.chapter_id}/dues-cycles/{cycle_b}/plans",
        headers=setup.president.headers,
    )
    assert list_b.status_code == 200, list_b.text
    [treasurer_plan_b] = list_b.json()
    assert len(treasurer_plan_b["installments"]) == 3
    for installment in treasurer_plan_b["installments"]:
        assert installment["paid_at"] is not None
        assert installment["effective_paid"] is True


# ---- c265: the schedule length is capped (from the c263 abuse sweep) ----


def _n_installments(n: int, total: int = 30_000) -> list[dict[str, Any]]:
    """n installments summing exactly to `total`, so the ONLY possible 422 source in
    the over-limit test is the length cap - not the sum rule, not the count rule.
    (c264's lesson: an over-limit payload that also trips a route's own 422 proves
    nothing about the cap.)"""
    each = total // n
    rows = [{"amount_cents": each, "due_date": "2027-09-01"} for _ in range(n - 1)]
    rows.append({"amount_cents": total - each * (n - 1), "due_date": "2027-11-01"})
    return rows


async def test_an_oversized_installment_schedule_is_refused_by_the_cap(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    from app.schemas.finance import MAX_PLAN_INSTALLMENTS

    setup = await make_chapter_with("treasurer")
    cycle_id = await _create_dues_cycle(client, setup)
    resp = await _create_plan(
        client, setup, cycle_id, setup.member.id,
        _n_installments(MAX_PLAN_INSTALLMENTS + 1),
    )
    assert resp.status_code == 422, resp.text
    # Assert the SHAPE, not just the status: pydantic's too_long is the one error
    # only the cap can produce (sum and count are both deliberately valid above).
    assert any(e.get("type") == "too_long" for e in resp.json()["detail"]), resp.text


async def test_a_three_year_monthly_schedule_still_fits(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Boundary is inclusive and generous: exactly MAX_PLAN_INSTALLMENTS creates fine,
    and a realistic 12-payment plan is nowhere near it."""
    from app.schemas.finance import MAX_PLAN_INSTALLMENTS

    setup = await make_chapter_with("treasurer")
    cycle_id = await _create_dues_cycle(client, setup)
    resp = await _create_plan(
        client, setup, cycle_id, setup.member.id,
        _n_installments(MAX_PLAN_INSTALLMENTS),
    )
    assert resp.status_code == 201, resp.text
    assert len(resp.json()["installments"]) == MAX_PLAN_INSTALLMENTS
