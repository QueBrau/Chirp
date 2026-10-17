"""c461 maintenance purge fencing tests with non-empty cross-account rows."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app import models
from app.db import get_session_factory
from app.jobs import purge
from app.services.account_deletion_hold import activate_deletion_hold
from tests.conftest import MakeCampus, MakeChapterWith, MakeUser
from tests.test_c347_key_material_retirement import _device, _otk
from tests.test_purge import _insert_chirp, _insert_chirp_vote, _insert_post_like


async def _post(chapter_id: str, author_id: str, deleted_at: datetime) -> str:
    async with get_session_factory()() as session:
        result = await session.execute(text(
            "INSERT INTO posts (chapter_id, campus_id, author_id, body, deleted_at) "
            "VALUES (:chapter, (SELECT campus_id FROM chapters WHERE id=:chapter), "
            ":author, 'c461 purge row', :deleted) RETURNING id"
        ), {"chapter": chapter_id, "author": author_id, "deleted": deleted_at})
        row_id = str(result.scalar_one())
        await session.commit()
        return row_id


async def _comment(post_id: str, author_id: str) -> str:
    async with get_session_factory()() as session:
        result = await session.execute(text(
            "INSERT INTO post_comments (post_id, author_id, body, deleted_at) "
            "VALUES (:post, :author, 'c461 comment', NULL) RETURNING id"
        ), {"post": post_id, "author": author_id})
        row_id = str(result.scalar_one())
        await session.commit()
        return row_id


async def _activate_hold(user_id: str) -> uuid.UUID:
    request_id = uuid.uuid4()
    async with get_session_factory()() as session:
        session.add(models.AccountDataRequest(
            id=request_id, user_id=uuid.UUID(user_id), kind="deletion",
            open_key=f"c461-{request_id}",
        ))
        await session.commit()
    async with get_session_factory()() as session:
        await activate_deletion_hold(session, user_id=uuid.UUID(user_id), request_id=request_id)
        await session.commit()
    return request_id


async def _counts(post_ids: list[str]) -> tuple[int, int]:
    async with get_session_factory()() as session:
        posts = await session.scalar(text(
            "SELECT count(*) FROM posts WHERE id = ANY(:ids)"
        ), {"ids": post_ids})
        comments = await session.scalar(text(
            "SELECT count(*) FROM post_comments WHERE post_id = ANY(:ids)"
        ), {"ids": post_ids})
        return int(posts or 0), int(comments or 0)


@pytest.mark.asyncio
async def test_purge_skips_held_child_owner_but_deletes_unrelated_owner(
    make_chapter_with: MakeChapterWith, make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await make_chapter_with("president")
    held = await make_user("c461 held child")
    eligible = await make_user("c461 eligible")
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)
    expired = now - timedelta(days=31)
    held_parent = await _post(setup.chapter_id, held.id, expired)
    await _comment(held_parent, held.id)
    mixed_parent = await _post(setup.chapter_id, eligible.id, expired)
    await _comment(mixed_parent, held.id)
    eligible_parent = await _post(setup.chapter_id, eligible.id, expired)
    assert await _counts([held_parent, mixed_parent, eligible_parent]) == (3, 2)
    await _activate_hold(held.id)

    seen_held: list[tuple[set[uuid.UUID], list[uuid.UUID]]] = []
    original_fence = purge._fence_users

    async def record_fence(session, owners):
        result = await original_fence(session, owners)
        seen_held.append((result, list(owners)))
        return result

    # This assertion also proves the held child owner was part of the lock set.
    # It is deliberately installed only around this real purge transaction.
    monkeypatch.setattr(purge, "_fence_users", record_fence)
    async with get_session_factory()() as session:
        result = await purge.purge_batch(session, cutoff=cutoff, batch_size=10)
        await session.commit()
    assert seen_held and uuid.UUID(held.id) in seen_held[0][1]
    assert seen_held[0][0] == {uuid.UUID(held.id)}
    # The held comment owner is included in the advisory-lock set, so the
    # parent is retained rather than deleting a child owned by a held account.
    assert result.posts == 1
    assert await _counts([held_parent]) == (1, 1)
    assert await _counts([mixed_parent]) == (1, 1)
    assert await _counts([eligible_parent]) == (0, 0)


@pytest.mark.asyncio
async def test_hold_activated_after_candidate_capture_wins_before_delete(
    make_chapter_with: MakeChapterWith, make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await make_chapter_with("president")
    owner = await make_user("c461 race owner")
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)
    post_id = await _post(setup.chapter_id, owner.id, now - timedelta(days=31))
    request_id = uuid.uuid4()
    async with get_session_factory()() as session:
        session.add(models.AccountDataRequest(
            id=request_id, user_id=uuid.UUID(owner.id), kind="deletion",
            open_key=f"c461-race-{request_id}",
        ))
        await session.commit()

    captured = asyncio.Event()
    release = asyncio.Event()
    original = purge._fence_users

    async def fence_after_capture(session, owners):
        captured.set()
        await release.wait()
        return await original(session, owners)

    monkeypatch.setattr(purge, "_fence_users", fence_after_capture)

    async def run() -> purge.PurgeResult:
        async with get_session_factory()() as session:
            result = await purge.purge_batch(session, cutoff=cutoff, batch_size=10)
            await session.commit()
            return result

    task = asyncio.create_task(run())
    await asyncio.wait_for(captured.wait(), timeout=5)
    async with get_session_factory()() as activator:
        await activate_deletion_hold(
            activator, user_id=uuid.UUID(owner.id), request_id=request_id,
        )
        await activator.commit()
    release.set()
    result = await asyncio.wait_for(task, timeout=5)

    assert result.posts == 0
    assert await _counts([post_id]) == (1, 0)


@pytest.mark.asyncio
async def test_held_like_and_vote_owners_are_fenced(
    make_chapter_with: MakeChapterWith, make_campus: MakeCampus, make_user: MakeUser,
) -> None:
    setup = await make_chapter_with("president")
    held = await make_user("c461 held interaction")
    eligible = await make_user("c461 interaction owner")
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)
    expired = now - timedelta(days=31)
    post_id = await _post(setup.chapter_id, eligible.id, expired)
    await _insert_post_like(post_id, held.id)
    chirp_id = await _insert_chirp(await make_campus(), eligible.id, removed_at=expired)
    await _insert_chirp_vote(chirp_id, held.id)
    device_id = await _device(held.id)
    prekey_id = await _otk(device_id, consumed_at=expired)
    await _activate_hold(held.id)

    async with get_session_factory()() as session:
        result = await purge.purge_batch(session, cutoff=cutoff, batch_size=10)
        await session.commit()

    assert result.posts == result.chirps == 0
    async with get_session_factory()() as session:
        assert await session.scalar(text("SELECT count(*) FROM post_likes WHERE post_id=:id"), {"id": post_id}) == 1
        assert await session.scalar(text("SELECT count(*) FROM chirp_votes WHERE chirp_id=:id"), {"id": chirp_id}) == 1
    async with get_session_factory()() as session:
        key_result = await purge.retire_key_material_batch(session, cutoff=cutoff, batch_size=10)
        await session.commit()
    assert key_result.retired_one_time_prekeys == 0
    async with get_session_factory()() as session:
        assert await session.get(models.OneTimePrekey, prekey_id) is not None
