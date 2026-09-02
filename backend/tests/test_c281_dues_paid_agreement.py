"""The treasurer and the president must answer "who has paid" identically (c258 follow-up).

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
