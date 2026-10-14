"""Deletion-hold admission for feed posts and actor-owned interactions."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.config import get_settings
from app.db import get_engine, get_session_factory
from app.services.account_cleanup import cleanup_local_account
from app.services.account_deletion_hold import activate_deletion_hold
from tests.conftest import MakeChapterWith, MakeUser, verify_campus


def _set_admission(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setenv("ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED", str(enabled).lower())
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clear_settings() -> None:
    yield
    get_settings.cache_clear()


async def _hold(user_id: str) -> None:
    async with get_session_factory()() as session:
        request = models.AccountDataRequest(
            user_id=uuid.UUID(user_id),
            kind="deletion",
            open_key=f"feed-{uuid.uuid4()}",
        )
        session.add(request)
        await session.flush()
        request_id = request.id
        await session.commit()
    async with get_session_factory()() as session:
        await activate_deletion_hold(
            session, user_id=uuid.UUID(user_id), request_id=request_id
        )
        await session.commit()


async def _create_post(client, chapter_id: str, user, body: str = "feed body") -> dict:
    response = await client.post(
        f"/chapters/{chapter_id}/posts", json={"body": body}, headers=user.headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _counts(user_id: str) -> tuple[int, int, int]:
    async with get_session_factory()() as session:
        posts = await session.scalar(
            select(func.count()).select_from(models.Post).where(
                models.Post.author_id == uuid.UUID(user_id)
            )
        )
        likes = await session.scalar(
            select(func.count()).select_from(models.PostLike).where(
                models.PostLike.user_id == uuid.UUID(user_id)
            )
        )
        comments = await session.scalar(
            select(func.count()).select_from(models.PostComment).where(
                models.PostComment.author_id == uuid.UUID(user_id)
            )
        )
    return int(posts or 0), int(likes or 0), int(comments or 0)


@pytest.mark.asyncio
async def test_held_actor_cannot_create_or_change_feed_rows_but_reads_remain(
    client, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("member")
    own_post = await _create_post(client, setup.chapter_id, setup.member)
    other_post = await _create_post(client, setup.chapter_id, setup.president)
    prior_like = await client.put(
        f"/posts/{other_post['id']}/likes", headers=setup.member.headers
    )
    assert prior_like.status_code == 200, prior_like.text
    await verify_campus(setup.member.id)
    chapter = await client.get(
        f"/chapters/{setup.chapter_id}", headers=setup.member.headers
    )
    assert chapter.status_code == 200, chapter.text
    campus_id = chapter.json()["campus_id"]
    await _hold(setup.member.id)

    create_org = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "must not persist"},
        headers=setup.member.headers,
    )
    create_campus = await client.post(
        f"/campuses/{campus_id}/posts",
        json={"body": "must not persist"},
        headers=setup.member.headers,
    )
    update = await client.patch(
        f"/chapters/{setup.chapter_id}/posts/{own_post['id']}",
        json={"body": "overwrite"},
        headers=setup.member.headers,
    )
    own_delete = await client.delete(
        f"/posts/{own_post['id']}", headers=setup.member.headers
    )
    chapter_delete = await client.delete(
        f"/chapters/{setup.chapter_id}/posts/{own_post['id']}",
        headers=setup.member.headers,
    )
    like = await client.put(
        f"/posts/{other_post['id']}/likes", headers=setup.member.headers
    )
    unlike = await client.delete(
        f"/posts/{other_post['id']}/likes", headers=setup.member.headers
    )
    comment = await client.post(
        f"/posts/{other_post['id']}/comments",
        json={"body": "must not persist"},
        headers=setup.member.headers,
    )
    assert [response.status_code for response in (
        create_org, create_campus, update, own_delete, chapter_delete, like, unlike, comment
    )] == [423] * 8
    assert await _counts(setup.member.id) == (1, 1, 0)

    async with get_session_factory()() as session:
        stored = await session.get(models.Post, uuid.UUID(own_post["id"]))
        assert stored.body == "feed body"
        assert stored.deleted_at is None
    read = await client.get(
        f"/chapters/{setup.chapter_id}/posts", headers=setup.member.headers
    )
    assert read.status_code == 200, read.text
    assert own_post["id"] in {row["id"] for row in read.json()}


@pytest.mark.asyncio
@pytest.mark.parametrize("write_kind", ["chapter_post", "campus_post", "comment", "like"])
async def test_write_response_is_ready_before_hold_activates_after_commit(
    write_kind: str,
    client,
    make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("member")
    target_post = None
    if write_kind in {"comment", "like"}:
        target_post = await _create_post(
            client, setup.chapter_id, setup.president, "interaction target"
        )
    if write_kind == "campus_post":
        await verify_campus(setup.member.id)
        chapter = await client.get(
            f"/chapters/{setup.chapter_id}", headers=setup.member.headers
        )
        assert chapter.status_code == 200, chapter.text
        campus_id = chapter.json()["campus_id"]

    row_type = {
        "chapter_post": models.Post,
        "campus_post": models.Post,
        "comment": models.PostComment,
        "like": models.PostLike,
    }[write_kind]
    original_commit = AsyncSession.commit
    activated = False

    async def commit_then_activate_hold(session: AsyncSession, *args, **kwargs):
        nonlocal activated
        has_target_row = any(
            isinstance(row, row_type)
            for row in (*session.new, *session.identity_map.values())
        )
        result = await original_commit(session, *args, **kwargs)
        if has_target_row and not activated:
            # The SQL row is durable and the shared gate has been released. Activate
            # the real hold before returning from commit to the route handler.
            activated = True
            await _hold(setup.member.id)
        return result

    monkeypatch.setattr(AsyncSession, "commit", commit_then_activate_hold)
    if write_kind == "chapter_post":
        response = await client.post(
            f"/chapters/{setup.chapter_id}/posts",
            json={"body": "commit response is already materialized"},
            headers=setup.member.headers,
        )
        expected_status = 201
    elif write_kind == "campus_post":
        response = await client.post(
            f"/campuses/{campus_id}/posts",
            json={"body": "commit response is already materialized"},
            headers=setup.member.headers,
        )
        expected_status = 201
    elif write_kind == "comment":
        response = await client.post(
            f"/posts/{target_post['id']}/comments",
            json={"body": "commit response is already materialized"},
            headers=setup.member.headers,
        )
        expected_status = 201
    else:
        response = await client.put(
            f"/posts/{target_post['id']}/likes", headers=setup.member.headers
        )
        expected_status = 200

    assert activated
    assert response.status_code == expected_status, response.text
    async with get_session_factory()() as session:
        if write_kind in {"chapter_post", "campus_post"}:
            row_id = uuid.UUID(response.json()["id"])
            persisted = await session.get(models.Post, row_id)
            assert persisted is not None and persisted.author_id == uuid.UUID(setup.member.id)
        elif write_kind == "comment":
            row_id = uuid.UUID(response.json()["id"])
            persisted = await session.get(models.PostComment, row_id)
            assert persisted is not None and persisted.author_id == uuid.UUID(setup.member.id)
        else:
            like = await session.get(
                models.PostLike,
                (uuid.UUID(target_post["id"]), uuid.UUID(setup.member.id)),
            )
            assert like is not None


@pytest.mark.asyncio
async def test_unheld_users_can_interact_with_visible_held_author_content_and_cleanup_retains_their_comment(
    client, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("member")
    held_post = await _create_post(client, setup.chapter_id, setup.president, "retained")
    await _hold(setup.president.id)

    like = await client.put(
        f"/posts/{held_post['id']}/likes", headers=setup.member.headers
    )
    comment = await client.post(
        f"/posts/{held_post['id']}/comments",
        json={"body": "other member history"},
        headers=setup.member.headers,
    )
    assert like.status_code == 200, like.text
    assert comment.status_code == 201, comment.text

    async with get_session_factory()() as session:
        await cleanup_local_account(session, uuid.UUID(setup.president.id))
        await session.commit()
    async with get_session_factory()() as session:
        post = await session.get(models.Post, uuid.UUID(held_post["id"]))
        like_row = await session.get(
            models.PostLike, (uuid.UUID(held_post["id"]), uuid.UUID(setup.member.id))
        )
        comment_row = await session.get(models.PostComment, uuid.UUID(comment.json()["id"]))
    assert post.body == "[deleted]"
    assert post.removed_reason == "account_deleted"
    assert post.media_urls == []
    assert like_row is not None
    assert comment_row.body == "other member history"


@pytest.mark.asyncio
async def test_author_edit_after_cleanup_cannot_overwrite_redaction(
    client, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("member")
    post = await _create_post(client, setup.chapter_id, setup.member, "before deletion")
    await _hold(setup.member.id)
    async with get_session_factory()() as session:
        await cleanup_local_account(session, uuid.UUID(setup.member.id))
        await session.commit()

    edited = await client.patch(
        f"/chapters/{setup.chapter_id}/posts/{post['id']}",
        json={"body": "resurrected"},
        headers=setup.member.headers,
    )
    # Cleanup revokes membership; ordinary authorization still runs before the
    # hidden post tombstone check, so this must not disclose target hold state.
    assert edited.status_code == 403, edited.text
    assert edited.json() == {"detail": "not_a_member"}
    async with get_session_factory()() as session:
        stored = await session.get(models.Post, uuid.UUID(post["id"]))
    assert stored.body == "[deleted]"
    assert stored.removed_reason == "account_deleted"
    assert stored.media_urls == []


@pytest.mark.asyncio
async def test_officer_can_remove_held_authors_post_without_overwriting_cleanup_fields(
    client, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("member")
    post = await _create_post(client, setup.chapter_id, setup.member, "retained history")
    await _hold(setup.member.id)

    removed = await client.delete(
        f"/chapters/{setup.chapter_id}/posts/{post['id']}",
        headers=setup.president.headers,
    )
    assert removed.status_code == 204, removed.text
    async with get_session_factory()() as session:
        stored = await session.get(models.Post, uuid.UUID(post["id"]))
        removed_at = stored.deleted_at
        assert removed_at is not None
        assert stored.body == "retained history"
        assert stored.removed_reason is None
        await cleanup_local_account(session, uuid.UUID(setup.member.id))
        await session.commit()

    async with get_session_factory()() as session:
        stored = await session.get(models.Post, uuid.UUID(post["id"]))
    assert stored.deleted_at is not None
    assert stored.body == "[deleted]"
    assert stored.removed_reason == "account_deleted"
    assert stored.media_urls == []


@pytest.mark.asyncio
async def test_officer_delete_racing_cleanup_keeps_account_deleted_redaction(
    client, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("member")
    post = await _create_post(client, setup.chapter_id, setup.member, "before cleanup")
    original_execute = AsyncSession.execute
    raced = False

    async def cleanup_before_post_cas(session, statement, *args, **kwargs):
        nonlocal raced
        if (
            not raced
            and statement.__class__.__name__ == "Update"
            and getattr(getattr(statement, "table", None), "name", None) == "posts"
        ):
            raced = True
            # The route has already authorized the unheld officer and read the
            # live row. Cleanup now wins before its conditional deleted_at update.
            await _hold(setup.member.id)
            async with get_session_factory()() as cleanup_session:
                await cleanup_local_account(
                    cleanup_session, uuid.UUID(setup.member.id)
                )
                await cleanup_session.commit()
        return await original_execute(session, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", cleanup_before_post_cas)
    removed = await client.delete(
        f"/chapters/{setup.chapter_id}/posts/{post['id']}",
        headers=setup.president.headers,
    )
    assert raced
    assert removed.status_code == 404, removed.text
    async with get_session_factory()() as session:
        stored = await session.get(models.Post, uuid.UUID(post["id"]))
    assert stored.body == "[deleted]"
    assert stored.media_urls == []
    assert stored.removed_reason == "account_deleted"


@pytest.mark.asyncio
async def test_flag_off_keeps_feed_writes_without_hold_table_query(
    client, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, False)
    setup = await make_chapter_with("member")
    await _hold(setup.member.id)
    statements: list[str] = []

    def record_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    engine = get_engine()
    event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
    try:
        response = await client.post(
            f"/chapters/{setup.chapter_id}/posts",
            json={"body": "flag off"},
            headers=setup.member.headers,
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_sql)
    assert response.status_code == 201, response.text
    assert not any("account_deletion_holds" in sql for sql in statements)
    assert (await _counts(setup.member.id))[0] == 1


@pytest.mark.asyncio
async def test_post_update_authorization_precedes_hold_and_preserves_not_author(
    client, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("member")
    post = await _create_post(client, setup.chapter_id, setup.member)
    response = await client.patch(
        f"/chapters/{setup.chapter_id}/posts/{post['id']}",
        json={"body": "wrong author"},
        headers=setup.president.headers,
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "not_author"}

