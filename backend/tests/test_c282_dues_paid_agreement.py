"""The treasurer and the president must answer "who has paid" identically (board c282).

ID NOTE: the merged commit and PR that introduced this file (ce81ca3, PR #180) carry
"c281" in their titles, which was wrong - c281 is chirps-7b's ARCHITECTURE refresh, and
this work's card has always been c282. Commit titles cannot be rewritten, so this file
and the comments pointing at it are the corrected, greppable truth.

THIS TEST IS THE DELIVERABLE; the code change only makes it pass.

GET /chapters/{id}/ledger/summary and GET /chapters/{id}/overview both split a roster
into paid / on-plan / outstanding for the current dues cycle. They must use the ONE rule
in core/dues_status.py, because two surfaces answering the same money question differently
is the exact bug that module was created to end - and it came back anyway.

WHAT WENT WRONG, recorded here because the recurrence is the point: the ledger summary
shipped with a plain `net > 0`, under a comment asserting it matched chapter_overview.
It did not. The rules diverge for exactly one member - active payment plan, partly paid,
net positive but below the cycle total - and the observed disagreement was:

    ledger summary paid_members  : 1
    overview       paid_members  : 0
    overview       on_plan_members: 1

The treasurer's screen said that member had paid. The president's said they had not.
Nothing in the diff looked wrong, because the comment asserted the agreement it lacked.
A comment claiming two surfaces agree is a claim that needs a test, not a reading. This
is that test.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from httpx import AsyncClient
from sqlalchemy import text as sa_text

from tests.conftest import MakeChapterWith

CYCLE_TOTAL = 30_000
PART_PAID = 10_000


async def _seed_active_plan_partial_payer(client: AsyncClient, setup) -> uuid.UUID:
    """The one member the two rules disagree about, and nothing else.

    Deliberately the DISCRIMINATING case rather than a realistic mix: an active plan,
    one installment recorded, so net is positive (10000) but under the cycle total
    (30000). A member who is simply paid, or simply unpaid, cannot detect the drift.
    """
    from app.db import get_session_factory

    chapter_id = uuid.UUID(setup.chapter_id)
    member = uuid.UUID(setup.member.id)
    cycle_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    async with get_session_factory()() as session:
        await session.execute(
            sa_text(
                "INSERT INTO dues_cycles (id, chapter_id, name, amount_cents, due_date)"
                " VALUES (:id, :cid, 'Agreement cycle', :total, :due)"
            ),
            {"id": cycle_id, "cid": chapter_id, "total": CYCLE_TOTAL, "due": date(2027, 5, 1)},
        )
        await session.execute(
            sa_text(
                "INSERT INTO dues_payment_plans (id, chapter_id, dues_cycle_id, user_id,"
                " status, total_cents, installment_count, created_by, created_at)"
                " VALUES (:id, :cid, :cy, :u, 'active', :total, 3, :u, :now)"
            ),
            {
                "id": uuid.uuid4(), "cid": chapter_id, "cy": cycle_id, "u": member,
                "total": CYCLE_TOTAL, "now": now,
            },
        )
        await session.execute(
            sa_text(
                "INSERT INTO ledger_entries (id, chapter_id, entry_type, amount_cents,"
                " related_user_id, dues_cycle_id, created_by, created_at)"
                " VALUES (:id, :cid, 'dues_installment', :amt, :u, :cy, :u, :now)"
            ),
            {
                "id": uuid.uuid4(), "cid": chapter_id, "amt": PART_PAID,
                "u": member, "cy": cycle_id, "now": now,
            },
        )
        await session.commit()
    return cycle_id


async def test_both_surfaces_split_the_roster_identically(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member")
    await _seed_active_plan_partial_payer(client, setup)

    summary = await client.get(
        f"/chapters/{setup.chapter_id}/ledger/summary", headers=setup.president.headers
    )
    assert summary.status_code == 200, summary.text
    overview = await client.get(
        f"/chapters/{setup.chapter_id}/overview", headers=setup.president.headers
    )
    assert overview.status_code == 200, overview.text

    treasurer_view = summary.json()["dues"]
    president_view = overview.json()["dues"]
    assert treasurer_view is not None

    assert treasurer_view["paid_members"] == president_view["paid_members"], (
        "the treasurer and president screens disagree about who has PAID: "
        f"{treasurer_view['paid_members']} vs {president_view['paid_members']}"
    )
    assert treasurer_view["on_plan_members"] == president_view["on_plan_members"], (
        "the two screens disagree about who is ON A PLAN: "
        f"{treasurer_view['on_plan_members']} vs {president_view['on_plan_members']}"
    )


async def test_the_seed_actually_exercises_the_disagreeing_case(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Guard against the test above going vacuous.

    If the seed ever stops producing a member who is net-positive, under the cycle total,
    AND on an active plan, the agreement assertion still passes while proving nothing -
    the same vacuous-green shape that has bitten this repo repeatedly. So the bucket
    itself is pinned: exactly one member on plan, none counted paid.
    """
    setup = await make_chapter_with("member")
    await _seed_active_plan_partial_payer(client, setup)

    dues = (
        await client.get(
            f"/chapters/{setup.chapter_id}/ledger/summary", headers=setup.president.headers
        )
    ).json()["dues"]

    assert dues["collected_cents"] == PART_PAID, "net must be positive"
    assert dues["collected_cents"] < dues["amount_cents"], "and under the cycle total"
    assert dues["on_plan_members"] == 1, "the discriminating member must land ON PLAN"
    assert dues["paid_members"] == 0, "and must NOT be counted as paid"


async def test_the_members_own_dues_screen_agrees_with_both_officer_surfaces(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """THE THIRD SURFACE (c258 PR B). The member's own dues list carries `viewer_paid`,
    and it must land in the same bucket the officers see for that same member.

    Extended here rather than in a parallel file on purpose: three surfaces answering
    one question belong in one test, so adding a fourth has an obvious home and cannot
    quietly grow its own rule the way this one did.
    """
    setup = await make_chapter_with("member")
    cycle_id = await _seed_active_plan_partial_payer(client, setup)

    cycles = await client.get(
        f"/chapters/{setup.chapter_id}/dues-cycles", headers=setup.member.headers
    )
    assert cycles.status_code == 200, cycles.text
    mine = next(c for c in cycles.json() if c["id"] == str(cycle_id))

    overview = (
        await client.get(
            f"/chapters/{setup.chapter_id}/overview", headers=setup.president.headers
        )
    ).json()["dues"]
    summary = (
        await client.get(
            f"/chapters/{setup.chapter_id}/ledger/summary", headers=setup.president.headers
        )
    ).json()["dues"]

    # One member in the chapter has contributed, and all three must place them the same.
    assert mine["viewer_paid"] is False, "the member must not read as paid to themselves"
    assert mine["viewer_on_plan"] is True, "they are on an active plan, partway paid"
    assert overview["paid_members"] == 0 and overview["on_plan_members"] == 1
    assert summary["paid_members"] == 0 and summary["on_plan_members"] == 1


async def test_a_completed_plan_corrected_away_is_not_latched_as_paid(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The latch the member's screen used to have, now impossible to express.

    dues.tsx counted a COMPLETED plan as independent, permanent proof that a cycle was
    settled - the exact latch c195's adversarial review removed from chapter_overview,
    grown back in the client. A plan whose installments are corrected away leaves the
    member owing again, and every surface must say so.
    """
    setup = await make_chapter_with("member")
    from app.db import get_session_factory

    chapter_id = uuid.UUID(setup.chapter_id)
    member = uuid.UUID(setup.member.id)
    cycle_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    async with get_session_factory()() as session:
        await session.execute(
            sa_text(
                "INSERT INTO dues_cycles (id, chapter_id, name, amount_cents, due_date)"
                " VALUES (:id, :cid, 'Refunded cycle', :total, :due)"
            ),
            {"id": cycle_id, "cid": chapter_id, "total": CYCLE_TOTAL, "due": date(2027, 5, 1)},
        )
        await session.execute(
            sa_text(
                "INSERT INTO dues_payment_plans (id, chapter_id, dues_cycle_id, user_id,"
                " status, total_cents, installment_count, created_by, created_at)"
                " VALUES (:id, :cid, :cy, :u, 'completed', :total, 1, :u, :now)"
            ),
            {
                "id": uuid.uuid4(), "cid": chapter_id, "cy": cycle_id, "u": member,
                "total": CYCLE_TOTAL, "now": now,
            },
        )
        paid_id = uuid.uuid4()
        await session.execute(
            sa_text(
                "INSERT INTO ledger_entries (id, chapter_id, entry_type, amount_cents,"
                " related_user_id, dues_cycle_id, created_by, created_at)"
                " VALUES (:id, :cid, 'dues_installment', :amt, :u, :cy, :u, :now)"
            ),
            {
                "id": paid_id, "cid": chapter_id, "amt": CYCLE_TOTAL,
                "u": member, "cy": cycle_id, "now": now,
            },
        )
        # ...then corrected away in full. Net is zero: the member owes again.
        await session.execute(
            sa_text(
                "INSERT INTO ledger_entries (id, chapter_id, entry_type, amount_cents,"
                " dues_cycle_id, corrects_entry_id, created_by, created_at)"
                " VALUES (:id, :cid, 'correction', :amt, :cy, :target, :u, :now)"
            ),
            {
                "id": uuid.uuid4(), "cid": chapter_id, "amt": -CYCLE_TOTAL,
                "cy": cycle_id, "target": paid_id, "u": member, "now": now,
            },
        )
        await session.commit()

    mine = next(
        c
        for c in (
            await client.get(
                f"/chapters/{setup.chapter_id}/dues-cycles", headers=setup.member.headers
            )
        ).json()
        if c["id"] == str(cycle_id)
    )
    assert mine["viewer_paid"] is False, (
        "a completed plan whose installments were corrected away must NOT latch as paid"
    )
    assert mine["viewer_on_plan"] is False, "the plan is completed, not active"
