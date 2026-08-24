"""Poll changes fan out over the WS gateway (board card c162).

Patched at publish_to_user rather than driven through a real Redis: what needs
proving here is WHO receives an event and WHAT it contains, and the socket path
itself is already covered end to end by test_ws_fanout.py (c21). Those tests need
a reachable Redis and skip without one; these must run everywhere, because the
thing most likely to regress is the payload quietly gaining a field.

The assertion that matters most is the negative one: a broadcast reaches every
member of the chapter, so if a voter's identity ever leaked into it, it would leak
to the entire chapter at once.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from tests.conftest import MakeChapterWith


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Collect fan-out instead of publishing it.

    Both modules are patched: polls.py binds the name at import time with a
    from-import, so patching only app.ws.pubsub would leave the router calling the
    real thing (the same trap test_group_membership_leave.py documents).
    """
    published: list[tuple[str, dict[str, Any]]] = []

    async def _fake(user_id: str, event: dict[str, Any]) -> None:
        published.append((user_id, event))

    import app.routers.polls as polls_router
    import app.ws.pubsub as pubsub

    monkeypatch.setattr(pubsub, "publish_to_user", _fake)
    monkeypatch.setattr(polls_router, "publish_to_user", _fake)
    return published


async def _open_poll(client: AsyncClient, setup) -> dict:
    created = await client.post(
        f"/chapters/{setup.chapter_id}/polls",
        json={"question": "Approve the budget?", "options": ["Yes", "No"]},
        headers=setup.member.headers,
    )
    assert created.status_code == 201, created.text
    return created.json()


async def test_opening_a_poll_reaches_every_active_member(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = await make_chapter_with("secretary")
    published = _capture(monkeypatch)

    poll = await _open_poll(client, setup)

    recipients = {user_id for user_id, _ in published}
    assert recipients == {setup.member.id, setup.president.id}
    for _user_id, event in published:
        assert event["type"] == "poll"
        assert event["action"] == "opened"
        assert event["poll_id"] == poll["id"]
        assert event["poll"]["total_votes"] == 0


async def test_a_vote_broadcasts_the_new_tally_and_names_nobody(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = await make_chapter_with("secretary")
    poll = await _open_poll(client, setup)
    published = _capture(monkeypatch)

    yes = poll["options"][0]["id"]
    voted = await client.post(
        f"/chapters/{setup.chapter_id}/polls/{poll['id']}/vote",
        json={"option_id": yes},
        headers=setup.president.headers,
    )
    assert voted.status_code == 200, voted.text

    assert {user_id for user_id, _ in published} == {setup.member.id, setup.president.id}
    for _user_id, event in published:
        assert event["action"] == "updated"
        assert event["poll"]["total_votes"] == 1
        assert {o["text"]: o["votes"] for o in event["poll"]["options"]} == {"Yes": 1, "No": 0}
        # The voter's id must appear nowhere, and my_option_id must not be in a
        # payload that every member receives -- it means something different for
        # each of them.
        assert "my_option_id" not in event["poll"]
        assert setup.president.id not in repr(event)


async def test_closing_broadcasts_once_and_a_second_close_is_silent(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = await make_chapter_with("secretary")
    poll = await _open_poll(client, setup)
    published = _capture(monkeypatch)

    first = await client.post(
        f"/chapters/{setup.chapter_id}/polls/{poll['id']}/close",
        headers=setup.member.headers,
    )
    assert first.status_code == 200, first.text
    after_first = len(published)
    assert after_first > 0
    assert all(e["poll"]["status"] == "closed" for _u, e in published)

    second = await client.post(
        f"/chapters/{setup.chapter_id}/polls/{poll['id']}/close",
        headers=setup.member.headers,
    )
    assert second.status_code == 200, second.text
    assert len(published) == after_first, "a no-op close must not re-push an event"


async def test_deleting_broadcasts_without_a_poll_body(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = await make_chapter_with("secretary")
    poll = await _open_poll(client, setup)
    published = _capture(monkeypatch)

    deleted = await client.delete(
        f"/chapters/{setup.chapter_id}/polls/{poll['id']}", headers=setup.member.headers
    )
    assert deleted.status_code == 204, deleted.text

    assert {user_id for user_id, _ in published} == {setup.member.id, setup.president.id}
    for _user_id, event in published:
        assert event["action"] == "deleted"
        assert event["poll_id"] == poll["id"]
        assert "poll" not in event, "there is no poll left to describe"


async def test_a_dead_redis_does_not_lose_the_vote(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fan-out is fire-and-forget. A ballot that was recorded but not broadcast is
    a stale screen; a broadcast that fails the write loses the vote outright."""
    setup = await make_chapter_with("secretary")
    poll = await _open_poll(client, setup)

    async def _explode(user_id: str, event: dict[str, Any]) -> None:
        raise ConnectionError("redis is down")

    import app.routers.polls as polls_router
    import app.ws.pubsub as pubsub

    monkeypatch.setattr(pubsub, "publish_to_user", _explode)
    monkeypatch.setattr(polls_router, "publish_to_user", _explode)

    voted = await client.post(
        f"/chapters/{setup.chapter_id}/polls/{poll['id']}/vote",
        json={"option_id": poll["options"][0]["id"]},
        headers=setup.president.headers,
    )
    assert voted.status_code == 200, voted.text
    assert voted.json()["total_votes"] == 1

    # And it is genuinely persisted, not just echoed back.
    reread = await client.get(
        f"/chapters/{setup.chapter_id}/polls/{poll['id']}", headers=setup.member.headers
    )
    assert reread.json()["total_votes"] == 1
