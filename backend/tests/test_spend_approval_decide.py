"""Spend-approval decisions are claimed once, by exactly one officer (board c114)."""
from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import MakeChapterWith


async def _open_approval(client: AsyncClient, setup) -> str:
    """Create a pending spend approval and return its id."""
    response = await client.post(
        f"/chapters/{setup.chapter_id}/spend-approvals",
        headers=setup.member.headers,
        json={"amount_cents": 12_000, "description": "banner for rush"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def test_decide_returns_the_decided_row_not_a_stale_pending_one(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The 200 body reports the decision that was just written.

    Guards the specific way the guarded-UPDATE fix could regress: the write goes
    through Core with synchronize_session=False, so the identity-mapped object still
    holds the pre-UPDATE values until it is refreshed. Drop that refresh and this
    route happily answers "pending" to the officer who just approved it.
    """
    setup = await make_chapter_with("treasurer")
    approval_id = await _open_approval(client, setup)

    response = await client.post(
        f"/chapters/{setup.chapter_id}/spend-approvals/{approval_id}/decide",
        headers=setup.member.headers,
        json={"status": "approved"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "approved"
    assert body["decided_by"] is not None
    assert body["decided_at"] is not None


async def test_second_decision_is_a_conflict_and_does_not_overwrite_the_first(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """A second officer deciding an already-decided approval gets 409 and changes nothing.

    The overwrite is the half that matters. Before the guarded UPDATE, the treasurer's
    approval and the president's rejection were both accepted, so the surviving record
    was whichever committed last while BOTH officers believed their decision stood. The
    409 is the visible symptom; the assertion that the row is still "approved", by the
    original decider, is the one that proves nothing was silently replaced.
    """
    setup = await make_chapter_with("treasurer")
    approval_id = await _open_approval(client, setup)

    first = await client.post(
        f"/chapters/{setup.chapter_id}/spend-approvals/{approval_id}/decide",
        headers=setup.member.headers,
        json={"status": "approved"},
    )
    assert first.status_code == 200, first.text
    decided_by = first.json()["decided_by"]

    second = await client.post(
        f"/chapters/{setup.chapter_id}/spend-approvals/{approval_id}/decide",
        headers=setup.president.headers,
        json={"status": "rejected"},
    )
    assert second.status_code == 409, second.text
    assert second.json() == {"detail": "already_decided"}

    listed = await client.get(
        f"/chapters/{setup.chapter_id}/spend-approvals", headers=setup.member.headers
    )
    assert listed.status_code == 200, listed.text
    row = next(item for item in listed.json() if item["id"] == approval_id)
    assert row["status"] == "approved", "the rejection overwrote the approval"
    assert row["decided_by"] == decided_by, "decided_by was replaced by the loser"


async def test_decide_is_scoped_to_the_chapter_in_the_path(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """An approval id from chapter A cannot be decided through chapter B's path.

    The chapter_id predicate moved from a Python comparison into the UPDATE's WHERE
    clause with the fix, so it is worth an explicit test that it is still enforced —
    a where-clause is easier to drop silently than an `if` with a raise under it.
    """
    chapter_a = await make_chapter_with("treasurer")
    chapter_b = await make_chapter_with("treasurer")
    approval_id = await _open_approval(client, chapter_a)

    response = await client.post(
        f"/chapters/{chapter_b.chapter_id}/spend-approvals/{approval_id}/decide",
        headers=chapter_b.member.headers,
        json={"status": "approved"},
    )

    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "spend_approval_not_found"}


# WHAT THESE TESTS DO AND DO NOT PROVE — measured, not assumed.
#
# All three PASS against the pre-fix read-check-then-write code as well. That is not a
# defect in them, it is the honest shape of the change: the SEQUENTIAL path was already
# correct before this commit, because a second decider re-read the row and saw a status
# that was no longer "pending". So these are not evidence that the race is fixed, and
# nobody reading a green run should think otherwise.
#
# What they do catch, verified by deleting the line and re-running: remove the
# `await session.refresh(approval)` from the decide route and the first two fail. The
# session factory is built with expire_on_commit=False (app/db.py), so a Core UPDATE
# with synchronize_session=False leaves the identity-mapped object holding pre-UPDATE
# values indefinitely — the route would answer "pending" to the officer who just
# approved it. That failure mode is NEW, introduced by this fix, and these tests exist
# mainly to pin it down.
#
# NO CONCURRENCY TEST HERE, DELIBERATELY, for the reasons test_moderation_resolve.py
# already recorded against the identical fix on resolve_report:
#   - asyncio.gather over two POSTs through the ASGI test client PASSES even against a
#     sabotaged read-check-then-write build, because the client does not interleave the
#     two handlers inside the critical section. A test that cannot fail reads as
#     coverage and is worse than none.
#   - Two live sessions each running the guarded UPDATE deadlock instead: the second
#     blocks on the row lock the first holds and neither has committed.
# The sequential double-decide above is what is actually covered. The concurrent case
# rests on `UPDATE ... WHERE status = 'pending'` being correct by construction, which is
# the same guarantee c51, c105 and c91 already depend on. Real coverage would need two
# processes and a lock-wait timeout, not two coroutines on one event loop.
