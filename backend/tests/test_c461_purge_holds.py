"""c461 maintenance purge fencing tests with non-empty cross-account rows."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

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

    async with get_session_factory()() as session:
        result = await purge.purge_batch(session, cutoff=cutoff, batch_size=10)
        await session.commit()
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
async def test_purge_first_shared_lock_makes_hold_wait_for_commit(
    make_chapter_with: MakeChapterWith, make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await make_chapter_with("president")
    owner = await make_user("c461 purge first")
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)
    post_id = await _post(setup.chapter_id, owner.id, now - timedelta(days=31))
    request_id = await _activate_request_only(owner.id)
    locked = asyncio.Event()
    release = asyncio.Event()
    original = purge._fence_users

    async def fence_then_pause(session, owners):
        result = await original(session, owners)
        locked.set()
        await release.wait()
        return result

    monkeypatch.setattr(purge, "_fence_users", fence_then_pause)

    async def run_purge() -> purge.PurgeResult:
        async with get_session_factory()() as session:
            result = await purge.purge_batch(session, cutoff=cutoff, batch_size=1)
            await session.commit()
            return result

    async def activate() -> None:
        async with get_session_factory()() as session:
            await activate_deletion_hold(
                session, user_id=uuid.UUID(owner.id), request_id=request_id,
            )
            await session.commit()

    purge_task = asyncio.create_task(run_purge())
    await asyncio.wait_for(locked.wait(), timeout=5)
    hold_task = asyncio.create_task(activate())
    await asyncio.sleep(0.05)
    assert not hold_task.done()
    release.set()
    result = await asyncio.wait_for(purge_task, timeout=5)
    await asyncio.wait_for(hold_task, timeout=5)
    assert result.posts == 1
    assert await _counts([post_id]) == (0, 0)


@pytest.mark.asyncio
async def test_parent_lock_fences_late_child_insert(
    make_chapter_with: MakeChapterWith, make_user: MakeUser,
) -> None:
    setup = await make_chapter_with("president")
    owner = await make_user("c461 late child")
    now = datetime.now(timezone.utc)
    post_id = await _post(setup.chapter_id, owner.id, now - timedelta(days=31))
    async with get_session_factory()() as purger:
        result = await purge.purge_batch(
            purger, cutoff=now - timedelta(days=30), batch_size=1,
        )
        assert result.posts == 1
        async with get_session_factory()() as writer:
            await writer.execute(text("SET LOCAL lock_timeout = 100"))
            with pytest.raises(DBAPIError) as error:
                await writer.execute(text(
                    "INSERT INTO post_comments(post_id, author_id, body) "
                    "VALUES (:post, :author, 'late c461 child')"
                ), {"post": post_id, "author": owner.id})
            assert error.value.orig.sqlstate == "55P03"
            await writer.rollback()
        await purger.commit()


@pytest.mark.asyncio
async def test_completed_tombstone_hold_does_not_block_purge(
    make_chapter_with: MakeChapterWith, make_user: MakeUser,
) -> None:
    setup = await make_chapter_with("president")
    owner = await make_user("c461 completed tombstone")
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)
    post_id = await _post(setup.chapter_id, owner.id, now - timedelta(days=31))
    request_id = await _activate_hold(owner.id)
    async with get_session_factory()() as session:
        await session.execute(text(
            "UPDATE users SET is_ghost=true, firebase_uid=:uid, email=:email "
            "WHERE id=:user"
        ), {"uid": f"deleted:{owner.id}", "email": f"deleted+{owner.id}@invalid.chirp", "user": owner.id})
        await session.execute(text(
            "UPDATE account_data_requests SET status='completed', completed_at=now() "
            "WHERE id=:request"
        ), {"request": str(request_id)})
        await session.commit()
    async with get_session_factory()() as session:
        result = await purge.purge_batch(session, cutoff=cutoff, batch_size=1)
        await session.commit()
    assert result.posts == 1
    assert await _counts([post_id]) == (0, 0)


async def _activate_request_only(user_id: str) -> uuid.UUID:
    request_id = uuid.uuid4()
    async with get_session_factory()() as session:
        session.add(models.AccountDataRequest(
            id=request_id, user_id=uuid.UUID(user_id), kind="deletion",
            open_key=f"c461-wait-{request_id}",
        ))
        await session.commit()
    return request_id


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


@pytest.mark.asyncio
async def test_held_backlog_does_not_starve_later_eligible_parent(
    make_chapter_with: MakeChapterWith, make_user: MakeUser,
) -> None:
    setup = await make_chapter_with("president")
    held = await make_user("c461 held backlog")
    eligible = await make_user("c461 later eligible")
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)
    expired = now - timedelta(days=31)
    held_posts = [
        await _post(setup.chapter_id, held.id, now - timedelta(days=32))
        for _ in range(4)
    ]
    eligible_post = await _post(setup.chapter_id, eligible.id, expired)
    await _activate_hold(held.id)

    async with get_session_factory()() as session:
        result = await purge.purge_batch(session, cutoff=cutoff, batch_size=1)
        await session.commit()

    assert result.posts == 1
    assert await _counts(held_posts) == (4, 0)
    assert await _counts([eligible_post]) == (0, 0)


@pytest.mark.asyncio
async def test_held_children_do_not_starve_later_unrelated_parents(
    make_chapter_with: MakeChapterWith, make_campus: MakeCampus, make_user: MakeUser,
) -> None:
    setup = await make_chapter_with("president")
    held = await make_user("c461 held children backlog")
    owner = await make_user("c461 unheld parents")
    now = datetime.now(timezone.utc)
    campus = await make_campus()
    retained_posts = []
    for _ in range(4):
        post_id = await _post(setup.chapter_id, owner.id, now - timedelta(days=32))
        retained_posts.append(post_id)
        await _comment(post_id, held.id)
        chirp_id = await _insert_chirp(campus, owner.id, removed_at=now - timedelta(days=32))
        await _insert_chirp_vote(chirp_id, held.id)
    eligible_post = await _post(setup.chapter_id, owner.id, now - timedelta(days=31))
    await _insert_chirp(campus, owner.id, removed_at=now - timedelta(days=31))
    await _activate_hold(held.id)

    async with get_session_factory()() as session:
        result = await purge.purge_batch(session, cutoff=now - timedelta(days=30), batch_size=1)
        await session.commit()

    assert result.posts == result.chirps == 1
    assert await _counts(retained_posts) == (4, 4)
    assert await _counts([eligible_post]) == (0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_completion", ["request_owner", "email"])
async def test_invalid_completed_hold_remains_blocking(
    make_chapter_with: MakeChapterWith, make_user: MakeUser, invalid_completion: str,
) -> None:
    setup = await make_chapter_with("president")
    owner = await make_user("c461 invalid completion")
    other = await make_user("c461 different request owner")
    now = datetime.now(timezone.utc)
    post_id = await _post(setup.chapter_id, owner.id, now - timedelta(days=31))
    request_id = await _activate_hold(owner.id)
    async with get_session_factory()() as session:
        await session.execute(text(
            "UPDATE users SET is_ghost=true, firebase_uid=:uid, email=:email WHERE id=:user"
        ), {"uid": f"deleted:{owner.id}", "email": (
            f"deleted+{owner.id}@invalid.chirp" if invalid_completion != "email"
            else f"{owner.id}@example.invalid"
        ), "user": owner.id})
        await session.execute(text(
            "UPDATE account_data_requests SET status='completed', completed_at=now(), "
            "user_id=:user WHERE id=:request"
        ), {"user": other.id if invalid_completion == "request_owner" else owner.id,
            "request": str(request_id)})
        await session.commit()
    async with get_session_factory()() as session:
        result = await purge.purge_batch(session, cutoff=now - timedelta(days=30), batch_size=1)
        await session.commit()
    assert result.posts == 0
    assert await _counts([post_id]) == (1, 0)
