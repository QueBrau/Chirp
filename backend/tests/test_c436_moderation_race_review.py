"""Real cleanup and concurrent moderation must not overwrite removal history."""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import func, select

from app import models
from app.config import get_settings
from app.db import get_session_factory
from app.routers import moderation
from app.services.account_cleanup import cleanup_local_account
from tests.conftest import verify_campus


@pytest.fixture(autouse=True)
def admission(monkeypatch):
    monkeypatch.setenv("ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _content(setup, kind):
    await verify_campus(setup.president.id)
    async with get_session_factory()() as session:
        chapter = await session.get(models.Chapter, uuid.UUID(setup.chapter_id))
        if kind == "chirp":
            row = models.Chirp(
                author_id=uuid.UUID(setup.member.id), campus_id=chapter.campus_id,
                body="Reported content",
            )
        else:
            post = models.Post(
                author_id=uuid.UUID(setup.member.id), campus_id=chapter.campus_id,
                chapter_id=chapter.id, body="Reported post",
            )
            session.add(post)
            await session.flush()
            row = post if kind == "post" else models.PostComment(
                post_id=post.id, author_id=uuid.UUID(setup.member.id), body="Reported comment",
            )
        session.add(row)
        await session.commit()
        return type(row), row.id


async def _remove(client, setup, kind, target_id):
    if kind == "chirp":
        path = f"/moderation/chirps/{target_id}/remove"
        body = {"reason": "Moderator decision"}
    else:
        path = "/moderation/content/remove"
        body = {"target_type": kind, "target_id": str(target_id), "reason": "Moderator decision"}
    return await client.post(path, headers=setup.president.headers, json=body)


async def _read_result(model, target_id):
    async with get_session_factory()() as session:
        row = await session.get(model, target_id)
        audits = await session.scalar(
            select(func.count()).select_from(models.ModerationAction).where(
                models.ModerationAction.action == "remove_content",
                models.ModerationAction.target_id == target_id,
            )
        )
        return row.body, row.removed_reason, audits


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["chirp", "post", "comment"])
async def test_cleanup_wins_after_moderator_read_without_reason_overwrite(
    client, make_chapter_with, monkeypatch, kind,
):
    setup = await make_chapter_with("member")
    model, target_id = await _content(setup, kind)
    authorize = moderation._require_moderator_for_campus

    async def cleanup_after_authorization(*args, **kwargs):
        await authorize(*args, **kwargs)
        # The request already read the old content. A distinct real transaction
        # now performs the actual cleanup before its conditional removal write.
        async with get_session_factory()() as cleanup:
            await cleanup_local_account(cleanup, uuid.UUID(setup.member.id))
            await cleanup.commit()

    monkeypatch.setattr(moderation, "_require_moderator_for_campus", cleanup_after_authorization)
    result = await _remove(client, setup, kind, target_id)
    assert result.status_code == 409, result.text
    assert result.json() == {"detail": "already_removed"}
    assert await _read_result(model, target_id) == ("[deleted]", "account_deleted", 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["chirp", "post", "comment"])
async def test_two_moderators_have_one_success_and_one_audit(
    client, make_chapter_with, monkeypatch, kind,
):
    setup = await make_chapter_with("member")
    model, target_id = await _content(setup, kind)
    authorize = moderation._require_moderator_for_campus
    both_read = asyncio.Event()
    arrivals = 0

    async def meet_after_authorization(*args, **kwargs):
        nonlocal arrivals
        await authorize(*args, **kwargs)
        arrivals += 1
        if arrivals == 2:
            both_read.set()
        await asyncio.wait_for(both_read.wait(), timeout=5)

    monkeypatch.setattr(moderation, "_require_moderator_for_campus", meet_after_authorization)
    tasks = [asyncio.create_task(_remove(client, setup, kind, target_id)) for _ in range(2)]
    try:
        results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert sorted(result.status_code for result in results) == [204, 409]
    assert arrivals == 2
    _body, reason, audits = await _read_result(model, target_id)
    assert reason == "Moderator decision"
    assert audits == 1
