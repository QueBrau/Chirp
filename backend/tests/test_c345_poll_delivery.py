"""Poll writes release database capacity before bounded best-effort broker delivery."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.db import get_engine, get_session, get_session_factory
from app.routers import polls
from app.ws import pubsub
from tests.conftest import MakeChapterWith


async def _open(client: AsyncClient, setup: Any) -> dict[str, Any]:
    response = await client.post(f"/chapters/{setup.chapter_id}/polls", headers=setup.member.headers,
                                 json={"question": "Secret question", "options": ["Yes", "No"]})
    assert response.status_code == 201, response.text
    return response.json()


async def _write(client: AsyncClient, setup: Any, poll: dict[str, Any], action: str) -> Response:
    path = f"/chapters/{setup.chapter_id}/polls/{poll['id']}"
    if action == "create":
        return await client.post(f"/chapters/{setup.chapter_id}/polls", headers=setup.member.headers,
                                 json={"question": "Secret question", "options": ["Yes", "No"]})
    if action == "delete":
        return await client.delete(path, headers=setup.member.headers)
    return await client.post(f"{path}/{action}", headers=setup.member.headers,
                             json={"option_id": poll["options"][0]["id"]} if action == "vote" else None)


@pytest.mark.parametrize("character", ["x", "界", "\U0001D11E"])
async def test_option_text_bound_accepts_limit_and_rejects_overflow_without_truncation(
    client: AsyncClient, make_chapter_with: MakeChapterWith, character: str,
) -> None:
    setup = await make_chapter_with("secretary")
    path = f"/chapters/{setup.chapter_id}/polls"
    accepted = await client.post(path, headers=setup.member.headers, json={
        "question": "Bounded option", "options": [character * 200, "Other"],
    })
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["options"][0]["text"] == character * 200
    refused = await client.post(path, headers=setup.member.headers, json={
        "question": "Oversized option", "options": [character * 201, "Other"],
    })
    assert refused.status_code == 422, refused.text
    listing = await client.get(path, headers=setup.member.headers)
    assert [poll["question"] for poll in listing.json()] == ["Bounded option"]


@pytest.mark.parametrize("action,expected", [("create", 201), ("vote", 200), ("close", 200), ("delete", 204)])
async def test_invoked_publishers_have_no_request_transaction_or_checked_out_connection(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
    action: str, expected: int,
) -> None:
    setup = await make_chapter_with("secretary")
    poll = await _open(client, setup)
    sessions: list[AsyncSession] = []
    observed: list[tuple[list[bool], int]] = []

    async def tracked_session() -> AsyncIterator[AsyncSession]:
        async with get_session_factory()() as session:
            sessions.append(session)
            yield session

    async def observe_publish(user_id: str, event: dict[str, Any]) -> None:
        # Record rather than assert here: delivery deliberately catches exceptions,
        # so an assertion inside the publisher could otherwise become a false green.
        observed.append(([session.in_transaction() for session in sessions],
                         get_engine().pool.checkedout()))

    app = client._transport.app
    app.dependency_overrides[get_session] = tracked_session
    monkeypatch.setattr(polls, "publish_to_user", observe_publish)
    try:
        response = await _write(client, setup, poll, action)
    finally:
        app.dependency_overrides.pop(get_session, None)
    assert response.status_code == expected, response.text
    assert len(sessions) == 1
    assert len(observed) == 2, "the real delivery helper must invoke both active recipients"
    assert observed == [([False], 0), ([False], 0)]


@pytest.mark.parametrize("failure", ["hang", "raise"])
@pytest.mark.parametrize("action,expected", [("create", 201), ("vote", 200), ("close", 200), ("delete", 204)])
async def test_actual_pubsub_path_is_bounded_and_committed_write_survives(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, failure: str, action: str, expected: int,
) -> None:
    setup = await make_chapter_with("secretary")
    poll = await _open(client, setup)
    calls: list[str] = []
    cancelled = asyncio.Event()

    class BrokenRedis:
        async def publish(self, channel: str, message: str) -> None:
            calls.append(message)
            if failure == "raise":
                raise ConnectionError("redis://secret-credential@example.invalid Secret question")
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    monkeypatch.setattr(pubsub, "get_redis", lambda: BrokenRedis())
    monkeypatch.setattr(polls, "POLL_BROADCAST_TIMEOUT_SECONDS", 0.05)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=polls.__name__):
        response = await asyncio.wait_for(_write(client, setup, poll, action), timeout=1.5)
    assert response.status_code == expected, response.text
    assert calls, "exercise publish_to_user JSON encoding and its actual Redis await"
    if failure == "hang":
        assert cancelled.is_set(), "the timed-out publish must not survive in a background task"
    diagnostics = [json.loads(record.getMessage()) for record in caplog.records
                   if record.name == polls.__name__ and record.levelno == logging.WARNING]
    assert len(diagnostics) == 1, "one batch failure must not create one warning per member"
    diagnostic = diagnostics[0]
    assert diagnostic["event"] == "poll_broadcast_incomplete"
    assert diagnostic["timed_out"] is (failure == "hang")
    assert diagnostic["recipients"] == 2
    assert "Secret question" not in repr(diagnostic)
    assert "secret-credential" not in repr(diagnostic)
    assert setup.member.id not in repr(diagnostic)
    assert "my_option_id" not in calls[0]
    if action == "create":
        poll = response.json()
    reread = await client.get(f"/chapters/{setup.chapter_id}/polls/{poll['id']}",
                              headers=setup.member.headers)
    if action == "delete":
        assert reread.status_code == 404
    else:
        assert reread.status_code == 200, reread.text
        assert reread.json()["total_votes"] == (1 if action == "vote" else 0)
        assert reread.json()["status"] == ("closed" if action == "close" else "open")


async def test_one_total_deadline_bounds_slow_delivery_across_a_large_roster(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await make_chapter_with("secretary")
    poll = await _open(client, setup)
    # This fixture controls the already-snapshotted roster, preserving the actual
    # Redis helper and deadline. It isolates whole-batch vs per-recipient timeouts.
    original_prepare = polls._prepare_broadcast

    async def large_roster(*args: Any, **kwargs: Any) -> polls._PollBroadcast:
        batch = await original_prepare(*args, **kwargs)
        return polls._PollBroadcast(tuple(str(uuid.uuid4()) for _ in range(100)), batch.event)

    calls = 0

    class SlowRedis:
        async def publish(self, channel: str, message: str) -> None:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.02)

    monkeypatch.setattr(polls, "_prepare_broadcast", large_roster)
    monkeypatch.setattr(pubsub, "get_redis", lambda: SlowRedis())
    monkeypatch.setattr(polls, "POLL_BROADCAST_TIMEOUT_SECONDS", 0.06)
    response = await asyncio.wait_for(_write(client, setup, poll, "vote"), timeout=0.8)
    assert response.status_code == 200, response.text
    assert 1 <= calls < 100


async def test_vote_budget_nplus1_and_independent_user_and_poll_scopes(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await make_chapter_with("secretary")
    poll = await _open(client, setup)
    other_poll = await _open(client, setup)

    async def publish(user_id: str, event: dict[str, Any]) -> None:
        pass

    monkeypatch.setattr(polls, "publish_to_user", publish)
    maximum, window = polls.POLL_VOTE_LIMIT
    assert maximum >= 2 * 5 and window <= 60, "five changes of mind need comfortable headroom"
    for _ in range(maximum):
        response = await _write(client, setup, poll, "vote")
        assert response.status_code == 200, response.text
    refused = await _write(client, setup, poll, "vote")
    assert refused.status_code == 429, refused.text
    assert refused.json() == {"detail": "poll_vote_rate_limited"}
    response = await _write(client, setup, other_poll, "vote")
    assert response.status_code == 200, response.text
    response = await client.post(f"/chapters/{setup.chapter_id}/polls/{poll['id']}/vote",
                                 headers=setup.president.headers,
                                 json={"option_id": poll["options"][1]["id"]})
    assert response.status_code == 200, response.text
    assert response.json()["total_votes"] == 2


async def test_rate_limit_redis_budget_runs_before_database_checkout(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await make_chapter_with("secretary")
    poll = await _open(client, setup)
    original = polls.enforce_limit
    observed: list[int] = []

    async def observe_limit(*args: Any, **kwargs: Any) -> None:
        observed.append(get_engine().pool.checkedout())
        await original(*args, **kwargs)

    monkeypatch.setattr(polls, "enforce_limit", observe_limit)
    response = await _write(client, setup, poll, "vote")
    assert response.status_code == 200, response.text
    assert observed == [0]


async def test_synthetic_chapter_vote_burst_releases_pool_while_delivery_waits(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await make_chapter_with("secretary")
    poll = await _open(client, setup)
    uids = [f"poll-burst-{uuid.uuid4().hex}" for _ in range(98)]
    async with get_session_factory()() as session:
        members = [models.User(firebase_uid=uid, email=f"{uid}@example.test",
                               display_name="Burst voter", account_type="greek") for uid in uids]
        session.add_all(members)
        await session.flush()
        session.add_all(models.Membership(user_id=user.id, chapter_id=uuid.UUID(setup.chapter_id),
                                           role="member", status="active") for user in members)
        await session.commit()
    gate = asyncio.Event()
    all_started = asyncio.Event()
    started: set[int] = set()
    final_recipients: set[str] = set()

    class DelayedRedis:
        async def publish(self, channel: str, message: str) -> None:
            total = json.loads(message)["poll"]["total_votes"]
            started.add(total)
            if len(started) == 12:
                all_started.set()
            await gate.wait()
            if total == 12:
                final_recipients.add(channel)

    monkeypatch.setattr(pubsub, "get_redis", lambda: DelayedRedis())
    monkeypatch.setattr(polls, "POLL_BROADCAST_TIMEOUT_SECONDS", 10.0)
    requests = [asyncio.create_task(client.post(
        f"/chapters/{setup.chapter_id}/polls/{poll['id']}/vote",
        headers={"X-Debug-Firebase-Uid": uid}, json={"option_id": poll["options"][0]["id"]},
    )) for uid in uids[:12]]
    try:
        # Twelve requests exceed the local five-connection pool. All must reach
        # their blocked broker call; retaining connections would stall this barrier.
        await asyncio.wait_for(all_started.wait(), timeout=4.0)
        assert get_engine().pool.checkedout() == 0
    finally:
        gate.set()
        responses = await asyncio.wait_for(asyncio.gather(*requests), timeout=4.0)
    assert all(response.status_code == 200 for response in responses)
    assert started == set(range(1, 13))
    assert len(final_recipients) == 100
    reread = await client.get(f"/chapters/{setup.chapter_id}/polls/{poll['id']}",
                              headers=setup.member.headers)
    assert reread.json()["total_votes"] == 12
