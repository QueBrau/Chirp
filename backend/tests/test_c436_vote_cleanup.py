"""Deletion clears private votes without changing other voters or revealing authors."""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import models
from app.config import get_settings
from app.core.windows import current_week_start
from app.db import get_session_factory
from app.services.account_cleanup import cleanup_local_account
from tests.conftest import set_campus
from tests.test_c436_org_cleanup_review import _hold
from tests.test_house_leaderboard import _make_chapter


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setenv("ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_hold_blocks_vote_insert_and_replace_and_cleanup_cannot_be_undone(
    client, make_user, make_campus,
):
    author, voter, other = [await make_user(name) for name in ("author", "voter", "other")]
    campus = await make_campus()
    for person in (author, voter, other):
        await set_campus(person.id, campus)
    chirps = []
    for body in ("Existing vote", "No vote yet"):
        response = await client.post(f"/campuses/{campus}/chirps", headers=author.headers, json={"body": body})
        assert response.status_code == 201, response.text
        chirps.append(response.json()["id"])
    for person in (voter, other):
        response = await client.put(f"/chirps/{chirps[0]}/vote", headers=person.headers, json={"value": 1})
        assert response.status_code == 200, response.text
    await _hold(voter.id)
    for chirp in chirps:
        response = await client.put(f"/chirps/{chirp}/vote", headers=voter.headers, json={"value": -1})
        assert response.status_code == 423, response.text
    async with get_session_factory()() as session:
        original = await session.get(models.ChirpVote, (uuid.UUID(chirps[0]), uuid.UUID(voter.id)))
        assert original.value == 1
        assert await session.get(models.ChirpVote, (uuid.UUID(chirps[1]), uuid.UUID(voter.id))) is None
        await cleanup_local_account(session, uuid.UUID(voter.id))
        await session.commit()
    response = await client.put(f"/chirps/{chirps[0]}/vote", headers=voter.headers, json={"value": 1})
    assert response.status_code == 423, response.text
    async with get_session_factory()() as session:
        votes = (await session.scalars(select(models.ChirpVote).where(models.ChirpVote.chirp_id == uuid.UUID(chirps[0])))).all()
        assert [(vote.user_id, vote.value) for vote in votes] == [(uuid.UUID(other.id), 1)]
        assert (await session.get(models.Chirp, uuid.UUID(chirps[0]))).score == 1


@pytest.mark.asyncio
async def test_visible_chirp_author_hold_is_not_exposed_by_voting(client, make_user, make_campus):
    author, voter = [await make_user(name) for name in ("hidden author", "unheld voter")]
    campus = await make_campus()
    for person in (author, voter):
        await set_campus(person.id, campus)
    response = await client.post(f"/campuses/{campus}/chirps", headers=author.headers, json={"body": "Visible until removed"})
    assert response.status_code == 201
    chirp = response.json()["id"]
    await _hold(author.id)
    vote = await client.put(f"/chirps/{chirp}/vote", headers=voter.headers, json={"value": 1})
    assert vote.status_code == 200, vote.text
    assert set(vote.json()) == {"chirp_id", "value"}
    assert author.id not in vote.text
    async with get_session_factory()() as session:
        await cleanup_local_account(session, uuid.UUID(author.id))
        await session.commit()
    removed = await client.put(f"/chirps/{chirp}/vote", headers=voter.headers, json={"value": -1})
    assert removed.status_code == 404 and removed.json()["detail"] == "chirp_not_found"


@pytest.mark.asyncio
async def test_disabled_flag_preserves_vote_path(client, make_user, make_campus, monkeypatch):
    voter = await make_user("legacy voter")
    campus = await make_campus()
    await set_campus(voter.id, campus)
    response = await client.post(f"/campuses/{campus}/chirps", headers=voter.headers, json={"body": "Legacy"})
    assert response.status_code == 201
    await _hold(voter.id)
    monkeypatch.setenv("ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED", "false")
    get_settings.cache_clear()
    vote = await client.put(f"/chirps/{response.json()['id']}/vote", headers=voter.headers, json={"value": 1})
    assert vote.status_code == 200, vote.text


@pytest.mark.asyncio
async def test_cleanup_removes_all_owned_house_ballots_only_and_repeat_is_safe(client, make_user, make_campus):
    owner, other = [await make_user(name) for name in ("ballot owner", "other voter")]
    campus = await make_campus()
    chapter = await _make_chapter(campus, "Retained house")
    for person in (owner, other):
        await set_campus(person.id, campus)
    async with get_session_factory()() as session:
        for person in (owner, other):
            for week in (current_week_start(), current_week_start() - timedelta(days=7)):
                session.add(models.HouseBallot(campus_id=uuid.UUID(campus), voter_id=uuid.UUID(person.id),
                    week_start=week, touse_chapter_id=uuid.UUID(chapter)))
        await session.commit()
    await _hold(owner.id)
    for _ in range(2):
        async with get_session_factory()() as session:
            result = await cleanup_local_account(session, uuid.UUID(owner.id))
            await session.commit()
            assert "house_ballots" in result["coverage"]["private"]
    async with get_session_factory()() as session:
        remaining = (await session.scalars(select(models.HouseBallot).where(models.HouseBallot.campus_id == uuid.UUID(campus)))).all()
        assert len(remaining) == 2
        assert all(ballot.voter_id == uuid.UUID(other.id) for ballot in remaining)
        assert await session.get(models.Chapter, uuid.UUID(chapter)) is not None
    denied = await client.put(f"/campuses/{campus}/house-ballot", headers=owner.headers,
        json={"touse_chapter_id": chapter})
    assert denied.status_code == 423, denied.text
    board = await client.get(f"/campuses/{campus}/house-leaderboard", headers=other.headers)
    assert board.status_code == 200 and board.json()["ballots_cast"] == 1
