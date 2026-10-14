"""Account-deletion admission for actor-owned poll mutations."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app import models
from app.config import get_settings
from app.db import get_session_factory
from app.services.account_cleanup import cleanup_local_account
from app.services.account_deletion_hold import activate_deletion_hold


def _set_admission(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setenv("ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED", str(enabled).lower())
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clear_settings():
    yield
    get_settings.cache_clear()


@pytest.fixture
def poll_delivery(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    """Keep request-path dispatch local and record only its delivered snapshots."""
    from app.routers import polls

    delivered: list[tuple[str, dict]] = []

    async def record(user_id: str, event: dict) -> None:
        delivered.append((user_id, event))

    monkeypatch.setattr(polls, "publish_to_user", record)
    return delivered


async def _activate_hold(user_id: str) -> uuid.UUID:
    async with get_session_factory()() as session:
        request = models.AccountDataRequest(
            user_id=uuid.UUID(user_id), kind="deletion", open_key="poll-test"
        )
        session.add(request)
        await session.flush()
        await session.commit()
        request_id = request.id
    async with get_session_factory()() as session:
        await activate_deletion_hold(
            session, user_id=uuid.UUID(user_id), request_id=request_id
        )
        await session.commit()
    return request_id


async def _create_poll(client, setup, *, actor=None) -> dict:
    user = actor or setup.member
    response = await client.post(
        f"/chapters/{setup.chapter_id}/polls",
        headers=user.headers,
        json={"question": "Approve?", "options": ["Yes", "No"]},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _poll_state(chapter_id: str, poll_id: str) -> dict[str, object]:
    async with get_session_factory()() as session:
        poll_count = await session.scalar(
            select(func.count()).select_from(models.Poll).where(
                models.Poll.chapter_id == uuid.UUID(chapter_id)
            )
        )
        poll = await session.get(models.Poll, uuid.UUID(poll_id))
        assert poll is not None
        options = await session.execute(
            select(models.PollOption.id, models.PollOption.text_, models.PollOption.position)
            .where(models.PollOption.poll_id == uuid.UUID(poll_id))
            .order_by(models.PollOption.position, models.PollOption.id)
        )
        vote_count = await session.scalar(
            select(func.count()).select_from(models.PollVote).where(
                models.PollVote.poll_id == uuid.UUID(poll_id)
            )
        )
        votes = await session.execute(
            select(models.PollVote.user_id, models.PollVote.option_id)
            .where(models.PollVote.poll_id == uuid.UUID(poll_id))
            .order_by(models.PollVote.user_id)
        )
        outbox_count = await session.scalar(
            select(func.count()).select_from(models.DeliveryOutbox).where(
                models.DeliveryOutbox.kind == "poll",
                models.DeliveryOutbox.payload["poll_id"].astext == poll_id,
            )
        )
        outbox_rows = await session.execute(
            select(
                models.DeliveryOutbox.id,
                models.DeliveryOutbox.recipient_ids,
                models.DeliveryOutbox.payload,
            )
            .where(
                models.DeliveryOutbox.kind == "poll",
                models.DeliveryOutbox.payload["poll_id"].astext == poll_id,
            )
            .order_by(models.DeliveryOutbox.id)
        )
        return {
            "chapter_poll_count": int(poll_count),
            "poll": (poll.id, poll.question, poll.status, poll.created_by, poll.closed_at),
            "options": tuple(options.all()),
            "vote_count": int(vote_count),
            "votes": tuple(votes.all()),
            "outbox_count": int(outbox_count),
            "outbox": tuple(outbox_rows.all()),
        }


@pytest.mark.asyncio
async def test_held_actor_cannot_run_any_poll_mutation_and_reads_stay_open(
    client, make_chapter_with, monkeypatch: pytest.MonkeyPatch, poll_delivery
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("secretary")
    poll = await _create_poll(client, setup)
    voted = await client.post(
        f"/chapters/{setup.chapter_id}/polls/{poll['id']}/vote",
        headers=setup.president.headers,
        json={"option_id": poll["options"][0]["id"]},
    )
    assert voted.status_code == 200, voted.text
    before = await _poll_state(setup.chapter_id, poll["id"])
    poll_delivery.clear()
    await _activate_hold(setup.president.id)

    create = await client.post(
        f"/chapters/{setup.chapter_id}/polls",
        headers=setup.president.headers,
        json={"question": "Must not persist", "options": ["Yes", "No"]},
    )
    vote = await client.post(
        f"/chapters/{setup.chapter_id}/polls/{poll['id']}/vote",
        headers=setup.president.headers,
        json={"option_id": poll["options"][1]["id"]},
    )
    close = await client.post(
        f"/chapters/{setup.chapter_id}/polls/{poll['id']}/close",
        headers=setup.president.headers,
    )
    delete = await client.delete(
        f"/chapters/{setup.chapter_id}/polls/{poll['id']}",
        headers=setup.president.headers,
    )
    assert [r.status_code for r in (create, vote, close, delete)] == [423] * 4
    assert await _poll_state(setup.chapter_id, poll["id"]) == before
    assert poll_delivery == []

    listed = await client.get(
        f"/chapters/{setup.chapter_id}/polls", headers=setup.president.headers
    )
    read_one = await client.get(
        f"/chapters/{setup.chapter_id}/polls/{poll['id']}",
        headers=setup.president.headers,
    )
    assert listed.status_code == 200, listed.text
    assert read_one.status_code == 200, read_one.text


@pytest.mark.asyncio
async def test_held_poll_creator_does_not_freeze_active_officer_lifecycle(
    client, make_chapter_with, monkeypatch: pytest.MonkeyPatch, poll_delivery
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("secretary")
    poll = await _create_poll(client, setup)
    await _activate_hold(setup.member.id)

    closed = await client.post(
        f"/chapters/{setup.chapter_id}/polls/{poll['id']}/close",
        headers=setup.president.headers,
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == "closed"
    async with get_session_factory()() as session:
        stored = await session.get(models.Poll, uuid.UUID(poll["id"]))
        assert stored is not None and stored.status == "closed"
        assert stored.created_by == uuid.UUID(setup.member.id)


@pytest.mark.asyncio
async def test_cleanup_removes_ballot_and_hold_blocks_its_recreation(
    client, make_chapter_with, monkeypatch: pytest.MonkeyPatch, poll_delivery
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("secretary")
    poll = await _create_poll(client, setup)
    voted = await client.post(
        f"/chapters/{setup.chapter_id}/polls/{poll['id']}/vote",
        headers=setup.president.headers,
        json={"option_id": poll["options"][0]["id"]},
    )
    assert voted.status_code == 200, voted.text
    await _activate_hold(setup.president.id)
    async with get_session_factory()() as session:
        await cleanup_local_account(session, uuid.UUID(setup.president.id))
        await session.commit()
    state = await _poll_state(setup.chapter_id, poll["id"])
    assert state["chapter_poll_count"] == 1
    assert state["vote_count"] == 0
    assert state["outbox_count"] == 0

    retry = await client.post(
        f"/chapters/{setup.chapter_id}/polls/{poll['id']}/vote",
        headers=setup.president.headers,
        json={"option_id": poll["options"][1]["id"]},
    )
    assert retry.status_code == 423, retry.text
    state = await _poll_state(setup.chapter_id, poll["id"])
    assert state["chapter_poll_count"] == 1
    assert state["vote_count"] == 0
    assert state["outbox_count"] == 0


@pytest.mark.asyncio
async def test_flag_off_preserves_legacy_poll_write_for_held_actor(
    client, make_chapter_with, monkeypatch: pytest.MonkeyPatch, poll_delivery
) -> None:
    _set_admission(monkeypatch, False)
    setup = await make_chapter_with("secretary")
    await _activate_hold(setup.president.id)
    created = await _create_poll(client, setup, actor=setup.president)
    assert created["status"] == "open"
