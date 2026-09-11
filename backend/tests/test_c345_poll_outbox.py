"""Polls wired onto the c356 delivery outbox (board card c345): durable retry via
the sweeper, at-most-one-pending-row-per-poll coalescing, and no push/no delivery
ordering guarantee added. See DELIVERY-OUTBOX.md and docs/poll-delivery-and-lifecycle.md.

tests/test_c345_poll_delivery.py is NOT edited by this card and must stay green
unchanged; it is re-run as-is (not duplicated here) as part of this card's own
falsification/verification, not as a new test in this file.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text

from app import models
from app.db import get_session_factory
from app.routers import polls
from app.services import outbox
from tests.conftest import MakeChapterWith, MakeUser
from tests.test_c345_poll_delivery import _open, _write


async def _poll_outbox_rows(poll_id: str) -> list[models.DeliveryOutbox]:
    async with get_session_factory()() as session:
        result = await session.execute(
            select(models.DeliveryOutbox).where(
                models.DeliveryOutbox.kind == "poll",
                models.DeliveryOutbox.payload["poll_id"].astext == poll_id,
            )
        )
        return list(result.scalars().all())


async def _bump_next_attempt_to_past(row_id: uuid.UUID) -> None:
    async with get_session_factory()() as session:
        await session.execute(
            text(
                "UPDATE delivery_outbox SET next_attempt_at = now() - interval '1 second' "
                "WHERE id = :id"
            ),
            {"id": row_id},
        )
        await session.commit()


async def _noop_dispatch_now(*args: Any, **kwargs: Any) -> None:
    return None


async def test_vote_enqueues_one_pending_row_scoped_to_active_recipients_only(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await make_chapter_with("secretary")
    poll = await _open(client, setup)

    invite = await client.post(
        f"/chapters/{setup.chapter_id}/invites", json={"role": "member"},
        headers=setup.president.headers,
    )
    assert invite.status_code == 201, invite.text
    inactive_user = await make_user("Soon Inactive")
    joined = await client.post(
        "/chapters/join", json={"code": invite.json()["code"]}, headers=inactive_user.headers,
    )
    assert joined.status_code == 201, joined.text

    async with get_session_factory()() as session:
        await session.execute(
            text("UPDATE memberships SET status='inactive' WHERE user_id=:u AND chapter_id=:c"),
            {"u": uuid.UUID(inactive_user.id), "c": uuid.UUID(setup.chapter_id)},
        )
        await session.commit()

    async with get_session_factory()() as session:
        status = (
            await session.execute(
                text("SELECT status FROM memberships WHERE user_id=:u AND chapter_id=:c"),
                {"u": uuid.UUID(inactive_user.id), "c": uuid.UUID(setup.chapter_id)},
            )
        ).scalar_one()
    assert status == "inactive", (
        "construct and confirm the excluded membership exists before voting -- otherwise "
        "the recipient-set assertion below would pass vacuously even with no active filter"
    )

    monkeypatch.setattr(outbox, "dispatch_now", _noop_dispatch_now)

    voted = await _write(client, setup, poll, "vote")
    assert voted.status_code == 200, voted.text

    rows = await _poll_outbox_rows(poll["id"])
    assert len(rows) == 1
    row = rows[0]
    assert row.payload["poll_id"] == poll["id"]
    assert row.payload["poll"]["total_votes"] == 1
    recipient_ids = {str(rid) for rid in row.recipient_ids}
    assert recipient_ids == {setup.member.id, setup.president.id}
    assert inactive_user.id not in recipient_ids


async def test_sweeper_delivers_the_pending_poll_row_to_every_active_recipient(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await make_chapter_with("secretary")
    poll = await _open(client, setup)

    monkeypatch.setattr(outbox, "dispatch_now", _noop_dispatch_now)
    voted = await _write(client, setup, poll, "vote")
    assert voted.status_code == 200, voted.text

    rows = await _poll_outbox_rows(poll["id"])
    assert len(rows) == 1
    assert rows[0].attempts == 0, "left pending untouched -- isolates the sweeper's own path"

    received: list[tuple[str, dict]] = []

    async def recording_publish(user_id: str, event: dict) -> None:
        received.append((user_id, event))

    monkeypatch.setattr(polls, "publish_to_user", recording_publish)

    stats = await outbox.dispatch_pending(limit=10)
    assert stats.claimed == 1
    assert stats.delivered == 1

    assert len(received) == 2, "the real 'poll' dispatcher must fire for both active members"
    recipients = {user_id for user_id, _ in received}
    assert recipients == {setup.member.id, setup.president.id}
    assert all(event["poll_id"] == poll["id"] for _, event in received)
    assert all(event["poll"]["total_votes"] == 1 for _, event in received)

    assert await _poll_outbox_rows(poll["id"]) == [], "row deleted on full success"


async def test_live_publisher_down_then_a_later_sweep_delivers_the_same_counts(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await make_chapter_with("secretary")
    poll = await _open(client, setup)

    async def always_fails(user_id: str, event: dict) -> None:
        raise ConnectionError("redis down (c345 test)")

    monkeypatch.setattr(polls, "publish_to_user", always_fails)

    voted = await _write(client, setup, poll, "vote")
    assert voted.status_code == 200, voted.text

    reread = await client.get(
        f"/chapters/{setup.chapter_id}/polls/{poll['id']}", headers=setup.member.headers,
    )
    assert reread.status_code == 200, reread.text
    assert reread.json()["total_votes"] == 1, "the write survives a dead publisher"

    rows = await _poll_outbox_rows(poll["id"])
    assert len(rows) == 1
    row = rows[0]
    assert row.attempts == 1, "dispatch_now's failure branch bumped it synchronously"
    recipients = {str(rid) for rid in row.recipient_ids}
    assert recipients == {setup.member.id, setup.president.id}, "both failed, nothing narrowed"

    received: list[tuple[str, dict]] = []

    async def recording_publish(user_id: str, event: dict) -> None:
        received.append((user_id, event))

    monkeypatch.setattr(polls, "publish_to_user", recording_publish)
    await _bump_next_attempt_to_past(row.id)

    stats = await outbox.dispatch_pending(limit=10)
    assert stats.delivered == 1
    assert len(received) == 2
    assert all(event["poll"]["total_votes"] == 1 for _, event in received), (
        "the sweep must deliver the SAME tally the original vote produced"
    )
    assert await _poll_outbox_rows(poll["id"]) == []


async def test_two_failed_votes_coalesce_into_one_pending_row_with_the_final_tally(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await make_chapter_with("secretary")
    poll = await _open(client, setup)

    async def always_fails(user_id: str, event: dict) -> None:
        raise ConnectionError("redis down (c345 test)")

    monkeypatch.setattr(polls, "publish_to_user", always_fails)

    first_vote = await _write(client, setup, poll, "vote")
    assert first_vote.status_code == 200, first_vote.text

    rows_after_first = await _poll_outbox_rows(poll["id"])
    assert len(rows_after_first) == 1
    assert rows_after_first[0].payload["poll"]["total_votes"] == 1, (
        "construct and confirm the first pending row exists before the second vote coalesces it"
    )

    second_vote = await client.post(
        f"/chapters/{setup.chapter_id}/polls/{poll['id']}/vote",
        headers=setup.president.headers,
        json={"option_id": poll["options"][0]["id"]},
    )
    assert second_vote.status_code == 200, second_vote.text

    rows_after_second = await _poll_outbox_rows(poll["id"])
    assert len(rows_after_second) == 1, (
        "two failed votes must coalesce into one pending row, not two"
    )
    assert rows_after_second[0].payload["poll"]["total_votes"] == 2, (
        "the surviving row must carry the FINAL tally, never the superseded first vote's"
    )

    received: list[tuple[str, dict]] = []

    async def recording_publish(user_id: str, event: dict) -> None:
        received.append((user_id, event))

    monkeypatch.setattr(polls, "publish_to_user", recording_publish)
    await _bump_next_attempt_to_past(rows_after_second[0].id)

    stats = await outbox.dispatch_pending(limit=10)
    assert stats.delivered == 1
    assert len(received) == 2
    assert all(event["poll"]["total_votes"] == 2 for _, event in received)
    assert await _poll_outbox_rows(poll["id"]) == []


async def test_partial_failure_narrows_the_row_to_only_the_failed_recipient(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await make_chapter_with("secretary")
    poll = await _open(client, setup)

    async def fails_for_president(user_id: str, event: dict) -> None:
        if user_id == setup.president.id:
            raise ConnectionError("redis down for president (c345 test)")

    monkeypatch.setattr(polls, "publish_to_user", fails_for_president)

    voted = await _write(client, setup, poll, "vote")
    assert voted.status_code == 200, voted.text

    rows = await _poll_outbox_rows(poll["id"])
    assert len(rows) == 1
    row = rows[0]
    recipient_ids = {str(rid) for rid in row.recipient_ids}
    assert recipient_ids == {setup.president.id}, (
        "narrowed to exactly the failed recipient; member's publish already succeeded"
    )

    received: list[str] = []

    async def recording_publish(user_id: str, event: dict) -> None:
        received.append(user_id)

    monkeypatch.setattr(polls, "publish_to_user", recording_publish)
    await _bump_next_attempt_to_past(row.id)

    stats = await outbox.dispatch_pending(limit=10)
    assert stats.delivered == 1
    assert received == [setup.president.id], "member must never be re-delivered to"
    assert await _poll_outbox_rows(poll["id"]) == []


async def test_delete_event_enqueues_and_delivers_a_bodyless_snapshot(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await make_chapter_with("secretary")
    poll = await _open(client, setup)

    async def always_fails(user_id: str, event: dict) -> None:
        raise ConnectionError("redis down (c345 test)")

    monkeypatch.setattr(polls, "publish_to_user", always_fails)

    deleted = await client.delete(
        f"/chapters/{setup.chapter_id}/polls/{poll['id']}", headers=setup.member.headers,
    )
    assert deleted.status_code == 204, deleted.text

    rows = await _poll_outbox_rows(poll["id"])
    assert len(rows) == 1
    row = rows[0]
    assert row.payload["action"] == "deleted"
    assert "poll" not in row.payload, "there is no poll left to describe"

    received: list[dict] = []

    async def recording_publish(user_id: str, event: dict) -> None:
        received.append(event)

    monkeypatch.setattr(polls, "publish_to_user", recording_publish)
    await _bump_next_attempt_to_past(row.id)

    stats = await outbox.dispatch_pending(limit=10)
    assert stats.delivered == 1
    assert len(received) == 2
    assert all(event["action"] == "deleted" and "poll" not in event for event in received)
    assert await _poll_outbox_rows(poll["id"]) == []


def test_poll_dispatcher_registered_at_import_time() -> None:
    assert "poll" in outbox._dispatchers
    assert outbox._dispatchers["poll"] is polls._broadcast
