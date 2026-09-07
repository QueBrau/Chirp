"""Barrier-controlled poll lifecycle races and publication after successful commit."""
from __future__ import annotations

import asyncio
import contextvars
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.routers import polls
from tests.conftest import MakeChapterWith
from tests.test_c345_poll_delivery import _open, _write


@pytest.mark.parametrize("first_action,second_action,expected", [
    ("vote", "close", (200, 200)),
    ("close", "vote", (200, 409)),
    ("vote", "delete", (200, 409)),
    ("delete", "vote", (204, 404)),
])
async def test_parent_lock_orders_lifecycle_decisions(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
    first_action: str, second_action: str, expected: tuple[int, int],
) -> None:
    setup = await make_chapter_with("secretary")
    poll = await _open(client, setup)
    first_has_poll = asyncio.Event()
    second_entered = asyncio.Event()
    release_first = asyncio.Event()
    request_order = contextvars.ContextVar("poll_request_order", default="")
    original = polls._get_chapter_poll
    published: list[dict[str, Any]] = []

    async def controlled_lookup(*args: Any, **kwargs: Any) -> models.Poll:
        order = request_order.get()
        if order == "second":
            second_entered.set()
        result = await original(*args, **kwargs)
        if order == "first":
            first_has_poll.set()
            await release_first.wait()
        return result

    async def capture(user_id: str, event: dict[str, Any]) -> None:
        published.append(event)

    async def request(action: str, order: str) -> Response:
        token = request_order.set(order)
        try:
            return await _write(client, setup, poll, action)
        finally:
            request_order.reset(token)

    monkeypatch.setattr(polls, "_get_chapter_poll", controlled_lookup)
    monkeypatch.setattr(polls, "publish_to_user", capture)
    first = asyncio.create_task(request(first_action, "first"))
    second: asyncio.Task[Response] | None = None
    try:
        await asyncio.wait_for(first_has_poll.wait(), timeout=2.0)
        second = asyncio.create_task(request(second_action, "second"))
        await asyncio.wait_for(second_entered.wait(), timeout=2.0)
        # The second request has reached the actual lookup but must wait for the
        # first writer. Without the row lock it finishes against the old state.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(second), timeout=0.2)
    finally:
        release_first.set()
        tasks = [first] + ([second] if second is not None else [])
        responses = await asyncio.wait_for(asyncio.gather(*tasks), timeout=3.0)
    assert tuple(response.status_code for response in responses) == expected
    if second_action == "delete":
        assert responses[1].json() == {"detail": "poll_has_ballots"}
        assert not any(event["action"] == "deleted" for event in published)
    elif first_action == "close":
        assert responses[1].json() == {"detail": "poll_closed"}
    reread = await client.get(f"/chapters/{setup.chapter_id}/polls/{poll['id']}",
                              headers=setup.member.headers)
    if first_action == "delete":
        assert reread.status_code == 404
        assert all(event["action"] == "deleted" for event in published)
    else:
        assert reread.status_code == 200, reread.text
        assert reread.json()["total_votes"] == (1 if first_action == "vote" else 0)
        if "close" in (first_action, second_action):
            assert reread.json()["status"] == "closed"


async def test_failed_delete_commit_publishes_nothing_and_retains_poll(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await make_chapter_with("secretary")
    poll = await _open(client, setup)
    original_commit = AsyncSession.commit
    attempted = False
    published: list[dict[str, Any]] = []

    async def failed_delete_commit(session: AsyncSession) -> None:
        nonlocal attempted
        if any(isinstance(row, models.Poll) for row in session.deleted):
            attempted = True
            raise RuntimeError("forced deletion commit failure")
        await original_commit(session)

    async def capture(user_id: str, event: dict[str, Any]) -> None:
        published.append(event)

    monkeypatch.setattr(AsyncSession, "commit", failed_delete_commit)
    monkeypatch.setattr(polls, "publish_to_user", capture)
    with pytest.raises(RuntimeError, match="forced deletion commit failure"):
        await _write(client, setup, poll, "delete")
    assert attempted, "the injected fault must reach the delete commit"
    assert published == []
    reread = await client.get(f"/chapters/{setup.chapter_id}/polls/{poll['id']}",
                              headers=setup.member.headers)
    assert reread.status_code == 200, reread.text
    assert len(reread.json()["options"]) == 2
