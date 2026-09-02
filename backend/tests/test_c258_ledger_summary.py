"""c258 PR A: the treasurer's numbers move server-side, BEFORE the list is paginated.

The dashboard used to reduce the full ledger list in the client for its balance, trend,
category donut and dues meter. Paginating the list first would have turned page one into
"the" balance - treasurer.tsx's own chart comment warned about exactly that shape before
any of this work existed. So the totals move first and the list becomes render-only.

THE EQUALITY GATE IS THE ACCEPTANCE TEST. On a deliberately ugly seeded ledger, the
server's balance and per-category totals must EXACTLY equal what the client's current
reduce produces over the same rows - the client formula is today's ground truth, because
it is what treasurers have been looking at. Those formulas are transcribed here from
treasurer.tsx and src/lib/treasury.ts and compared row for row.

DUES ARE THE ONE DELIBERATE DIVERGENCE, and it is asserted rather than glossed: the
server uses core/dues_status.py's netting (dues_payment + dues_installment + corrections)
while the old client formula read only dues_payment with Math.abs. On this seed those two
disagree, and the test pins WHY, so the correction can never be mistaken for a bug.

The seed is ugly on purpose (manager's requirement): an installment payer, a refund of a
dues payment, a correction against a NON-dues spend, an uncategorised entry, a
whitespace-only category, and a member paying across MULTIPLE entry types. An equality
gate over a tidy ledger proves nothing, because the two formulas only disagree on the
branches a tidy ledger does not contain.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy import text as sa_text

from tests.conftest import MakeChapterWith, MakeUser

UNCATEGORISED = "Uncategorised"  # the client's label for a blank category


def _client_balance(rows: list[dict]) -> int:
    """treasurer.tsx:223 — entries.reduce((sum, e) => sum + e.amount_cents, 0)."""
    return sum(r["amount_cents"] for r in rows)


def _client_categories(rows: list[dict]) -> dict[str, int]:
    """src/lib/treasury.ts spendByCategory, before its ranking/folding step."""
    totals: dict[str, int] = {}
    for r in rows:
        if r["amount_cents"] >= 0:
            continue
        raw = r.get("category")
        label = raw.strip() if raw and raw.strip() else UNCATEGORISED
        totals[label] = totals.get(label, 0) + abs(r["amount_cents"])
    return totals


def _server_categories(payload: dict) -> dict[str, int]:
    """The server returns category=None for blank; the client supplies the label."""
    out: dict[str, int] = {}
    for row in payload["categories"]:
        raw = row["category"]
        label = raw.strip() if raw and raw.strip() else UNCATEGORISED
        out[label] = out.get(label, 0) + row["cents"]
    return out


async def _seed_ugly_ledger(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
):
    setup = await make_chapter_with("member")
    from app.db import get_session_factory

    chapter_id = uuid.UUID(setup.chapter_id)
    payer = uuid.UUID(setup.member.id)
    other = uuid.UUID(setup.president.id)
    cycle_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    async def insert(**kw):
        kw.setdefault("id", uuid.uuid4())
        kw.setdefault("category", None)
        kw.setdefault("related_user_id", None)
        kw.setdefault("dues_cycle_id", None)
        kw.setdefault("corrects_entry_id", None)
        await session.execute(
            sa_text(
                "INSERT INTO ledger_entries (id, chapter_id, entry_type, amount_cents,"
                " category, related_user_id, dues_cycle_id, corrects_entry_id,"
                " created_by, created_at)"
                " VALUES (:id, :chapter_id, :entry_type, :amount_cents, :category,"
                " :related_user_id, :dues_cycle_id, :corrects_entry_id, :created_by,"
                " :created_at)"
            ),
            {"chapter_id": chapter_id, "created_by": other, **kw},
        )
        return kw["id"]

    async with get_session_factory()() as session:
        await session.execute(
            sa_text(
                "INSERT INTO dues_cycles (id, chapter_id, name, amount_cents, due_date)"
                " VALUES (:id, :chapter_id, 'Spring 2027 Dues', 30000, :due)"
            ),
            {"id": cycle_id, "chapter_id": chapter_id, "due": date(2027, 5, 1)},
        )

        # A lump-sum dues payment, later PARTIALLY REFUNDED by a correction.
        paid = await insert(
            entry_type="dues_payment", amount_cents=30000, related_user_id=other,
            dues_cycle_id=cycle_id, created_at=now - timedelta(days=95),
        )
        await insert(
            entry_type="correction", amount_cents=-10000, dues_cycle_id=cycle_id,
            corrects_entry_id=paid, created_at=now - timedelta(days=60),
        )
        # A PAYMENT-PLAN member: two installments, invisible to the old client formula.
        for offset in (70, 40):
            await insert(
                entry_type="dues_installment", amount_cents=10000, related_user_id=payer,
                dues_cycle_id=cycle_id, created_at=now - timedelta(days=offset),
            )
        # ...who ALSO has a dues_payment row, so one member spans multiple entry types.
        await insert(
            entry_type="dues_payment", amount_cents=5000, related_user_id=payer,
            dues_cycle_id=cycle_id, created_at=now - timedelta(days=35),
        )
        # Spending: two categories, one blank, one whitespace-only.
        await insert(entry_type="expense", amount_cents=-4500, category="formal",
                     created_at=now - timedelta(days=50))
        await insert(entry_type="expense", amount_cents=-1500, category="formal",
                     created_at=now - timedelta(days=20))
        await insert(entry_type="expense", amount_cents=-2000, category="rush",
                     created_at=now - timedelta(days=10))
        await insert(entry_type="expense", amount_cents=-750, category=None,
                     created_at=now - timedelta(days=8))
        await insert(entry_type="expense", amount_cents=-250, category="   ",
                     created_at=now - timedelta(days=7))
        # A correction against a NON-DUES spend: nets the balance, must not touch dues.
        spend = await insert(entry_type="expense", amount_cents=-9000, category="rush",
                             created_at=now - timedelta(days=6))
        # Anchored to NOW, not to a day offset. An offset seed makes the "current month
        # is partial" assertion depend on the date the suite runs - five days ago is last
        # month on the 1st and this month on the 20th - which is a flaky test dressed as
        # a passing one. This row guarantees the newest bucket is the current month.
        await insert(entry_type="correction", amount_cents=3000, category="rush",
                     corrects_entry_id=spend, created_at=now)
        await session.commit()

    return setup, cycle_id


async def test_server_totals_equal_the_client_formula_exactly(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """THE ACCEPTANCE GATE. Same rows, both formulas, exact equality."""
    setup, _ = await _seed_ugly_ledger(client, make_chapter_with, make_user)

    listed = await client.get(
        f"/chapters/{setup.chapter_id}/ledger", headers=setup.president.headers
    )
    assert listed.status_code == 200, listed.text
    rows = listed.json()
    assert len(rows) == 12, "the seed must actually be ugly"

    summary = await client.get(
        f"/chapters/{setup.chapter_id}/ledger/summary", headers=setup.president.headers
    )
    assert summary.status_code == 200, summary.text
    payload = summary.json()

    assert payload["balance_cents"] == _client_balance(rows), (
        "server balance must equal the client reduce over the same rows"
    )
    assert payload["entry_count"] == len(rows)
    assert _server_categories(payload) == _client_categories(rows), (
        "per-category spend totals must match the client's spendByCategory exactly"
    )


async def test_the_dues_meter_is_corrected_and_the_old_formula_was_wrong(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """The one deliberate divergence, pinned so it cannot be mistaken for a regression.

    Old client formula: only entry_type == "dues_payment", summed with Math.abs.
    House logic: dues_payment + dues_installment, netted against their corrections.
    """
    setup, _ = await _seed_ugly_ledger(client, make_chapter_with, make_user)
    rows = (
        await client.get(
            f"/chapters/{setup.chapter_id}/ledger", headers=setup.president.headers
        )
    ).json()

    old_formula = sum(
        abs(r["amount_cents"]) for r in rows if r["entry_type"] == "dues_payment"
    )
    summary = (
        await client.get(
            f"/chapters/{setup.chapter_id}/ledger/summary", headers=setup.president.headers
        )
    ).json()
    dues = summary["dues"]
    assert dues is not None

    # 30000 lump sum - 10000 refund + 10000 + 10000 installments + 5000 = 45000
    assert dues["collected_cents"] == 45000
    # The old formula saw 35000: both dues_payment rows, no installments, no refund.
    assert old_formula == 35000
    assert dues["collected_cents"] != old_formula, (
        "this seed must exercise the disagreement, or the correction is untested"
    )
    # Both members are net-positive for the cycle.
    assert dues["paid_members"] == 2
    assert dues["amount_cents"] == 30000


async def test_the_trend_is_monthly_running_balance_ending_at_the_balance(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """Buckets are months, values are the RUNNING balance, and the series must land
    exactly on the reported balance - a trend whose last point disagrees with the hero
    stat is the two-surfaces-one-number bug in a single screen."""
    setup, _ = await _seed_ugly_ledger(client, make_chapter_with, make_user)
    payload = (
        await client.get(
            f"/chapters/{setup.chapter_id}/ledger/summary", headers=setup.president.headers
        )
    ).json()

    trend = payload["trend"]
    assert trend, "a seeded ledger must produce points"
    assert [p["period_start"] for p in trend] == sorted(p["period_start"] for p in trend)
    assert trend[-1]["balance_cents"] == payload["balance_cents"], (
        "the last running-balance point IS the balance"
    )
    assert sum(1 for p in trend if p["partial"]) <= 1, "only the current month is partial"
    assert trend[-1]["partial"] is True, "the seed writes into the current month"


async def test_a_chapter_with_no_ledger_and_no_cycle_is_a_real_state(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """A brand-new chapter is not an error: zeros, empty lists, and no dues meter."""
    setup = await make_chapter_with("member")
    payload = (
        await client.get(
            f"/chapters/{setup.chapter_id}/ledger/summary", headers=setup.president.headers
        )
    ).json()
    assert payload["balance_cents"] == 0
    assert payload["entry_count"] == 0
    assert payload["categories"] == []
    assert payload["trend"] == []
    assert payload["dues"] is None


async def test_the_summary_is_treasurer_gated(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Same DUES_ADMIN gate as the list it summarises - a member must not read it."""
    setup = await make_chapter_with("member")
    denied = await client.get(
        f"/chapters/{setup.chapter_id}/ledger/summary", headers=setup.member.headers
    )
    assert denied.status_code == 403, denied.text
