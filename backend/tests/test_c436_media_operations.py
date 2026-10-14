"""Real-PostgreSQL admission and drain tests for GCS media worker calls."""
from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.config import get_settings
from app.db import get_engine, get_session_factory
from app.services.account_deletion_hold import activate_deletion_hold
from app.services.account_media_operations import finalize_account_media
from app.services.storage_service import AVATAR_PREFIX
from tests.conftest import MakeChapterWith, MakeUser, verify_campus

TEST_BUCKET = "chirps-prod-media"


@pytest.fixture(autouse=True)
def _clear_settings() -> None:
    yield
    get_settings.cache_clear()


def _set_admission(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setenv("ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED", str(enabled).lower())
    get_settings.cache_clear()


def _configure_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "media_bucket_name", TEST_BUCKET)


def _tmp_name(user_id: str, suffix: str = "image.jpg") -> str:
    return f"tmp/{user_id}/{suffix}"


def _final_url(user_id: str, object_name: str, **kwargs) -> str:
    prefix = kwargs.get("destination_prefix", "posts")
    basename = object_name.rsplit("/", 1)[-1]
    return f"https://storage.googleapis.com/{TEST_BUCKET}/{prefix}/{user_id}/{basename}"


async def _hold(user_id: str) -> uuid.UUID:
    async with get_session_factory()() as session:
        request = models.AccountDataRequest(
            user_id=uuid.UUID(user_id),
            kind="deletion",
            open_key=f"media-operation-{uuid.uuid4()}",
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
    return request_id


async def _operations(user_id: str) -> list[models.AccountExternalOperation]:
    async with get_session_factory()() as session:
        return list((await session.scalars(
            select(models.AccountExternalOperation)
            .where(models.AccountExternalOperation.actor_user_id == uuid.UUID(user_id))
            .order_by(models.AccountExternalOperation.created_at)
        )).all())


async def _avatar(client, user, object_name: str):
    return await client.patch(
        "/auth/me", headers=user.headers,
        json={"avatar_object_name": object_name},
    )


@pytest.mark.asyncio
async def test_held_actor_is_rejected_before_media_worker_registration(
    client, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    _configure_bucket(monkeypatch)
    user = await make_user("Held media actor")
    await _hold(user.id)
    calls: list[str] = []

    import app.routers.auth as auth_router

    monkeypatch.setattr(
        auth_router, "finalize_media_object",
        lambda user_id, name, **kwargs: calls.append(name) or _final_url(user_id, name, **kwargs),
    )
    response = await _avatar(client, user, _tmp_name(user.id, "held.jpg"))

    assert response.status_code == 423, response.text
    assert calls == []
    assert await _operations(user.id) == []


@pytest.mark.asyncio
async def test_hold_during_worker_drains_it_then_blocks_followup_profile_write(
    client, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    _configure_bucket(monkeypatch)
    user = await make_user("Media race actor")
    started, release = threading.Event(), threading.Event()

    def finalize(user_id: str, name: str, **kwargs) -> str:
        started.set()
        if not release.wait(8):
            raise AssertionError("test did not release media worker")
        return _final_url(user_id, name, **kwargs)

    import app.routers.auth as auth_router

    monkeypatch.setattr(auth_router, "finalize_media_object", finalize)
    request = asyncio.create_task(_avatar(client, user, _tmp_name(user.id, "race.jpg")))
    try:
        assert await asyncio.to_thread(started.wait, 5), "media worker did not start"
        assert get_engine().sync_engine.pool.checkedout() == 0
        request_id = await _hold(user.id)
        operations = await _operations(user.id)
        assert len(operations) == 1
        assert operations[0].kind == "media_finalize_avatar"
        assert operations[0].state == "running"
        release.set()
        response = await request
        assert response.status_code == 423, response.text
        operations = await _operations(user.id)
        assert len(operations) == 1 and operations[0].state == "finished"
        assert operations[0].completed_at is not None
        async with get_session_factory()() as session:
            stored = await session.get(models.User, uuid.UUID(user.id))
            assert stored.avatar_url is None
        # Activation is a barrier only for registered work; after the worker is
        # drained, a fresh hold-scoped inspection sees no registered operation.
        from app.services.account_external_operations import inspect_registered_operation_drain

        drain = await inspect_registered_operation_drain(
            user_id=uuid.UUID(user.id), request_id=request_id
        )
        assert drain.registered_operations_drained
    finally:
        release.set()
        if not request.done():
            await asyncio.gather(request, return_exceptions=True)


@pytest.mark.asyncio
async def test_repeated_cancellation_drains_thread_and_records_completion(
    make_user: MakeUser, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    _configure_bucket(monkeypatch)
    user = await make_user("Canceled media actor")
    started, release, worker_finished = threading.Event(), threading.Event(), threading.Event()

    def finalize(user_id: str, name: str) -> str:
        started.set()
        try:
            if not release.wait(8):
                raise AssertionError("test did not release media worker")
            return _final_url(user_id, name)
        finally:
            worker_finished.set()

    request = asyncio.create_task(finalize_account_media(
        uuid.UUID(user.id), user.firebase_uid, _tmp_name(user.id, "cancel.jpg"),
        finalize=finalize,
    ))
    try:
        assert await asyncio.to_thread(started.wait, 5), "media worker did not start"
        request.cancel()
        await asyncio.sleep(.05)
        request.cancel()
        await asyncio.sleep(.05)
        assert not request.done(), "wrapper abandoned worker after repeated cancellation"
        operations = await _operations(user.id)
        assert len(operations) == 1 and operations[0].state == "running"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert worker_finished.is_set(), "wrapper cancellation returned before the real thread exited"
        operations = await _operations(user.id)
        assert len(operations) == 1 and operations[0].state == "finished"
        assert operations[0].completed_at is not None
    finally:
        release.set()
        if not request.done():
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["profile", "org_post", "campus_post", "edit_post"])
async def test_media_routes_preserve_final_urls_and_record_completion(
    route: str,
    client,
    make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    _configure_bucket(monkeypatch)
    setup = await make_chapter_with("member")
    user = setup.member
    name = _tmp_name(user.id, f"{route}.jpg")

    import app.routers.auth as auth_router
    import app.routers.feed as feed_router

    monkeypatch.setattr(auth_router, "finalize_media_object", _final_url)
    monkeypatch.setattr(feed_router, "finalize_media_object", _final_url)
    expected_prefix = "avatars" if route == "profile" else "posts"
    expected_url = f"https://storage.googleapis.com/{TEST_BUCKET}/{expected_prefix}/{user.id}/{route}.jpg"

    if route == "profile":
        response = await _avatar(client, user, name)
        assert response.status_code == 200, response.text
        assert response.json()["avatar_url"] == expected_url
        kind = "media_finalize_avatar"
    elif route == "org_post":
        response = await client.post(
            f"/chapters/{setup.chapter_id}/posts",
            headers=user.headers,
            json={"body": "media op", "media_object_names": [name], "post_type": "photo"},
        )
        assert response.status_code == 201, response.text
        assert response.json()["media_urls"] == [expected_url]
        kind = "media_finalize_post"
    elif route == "campus_post":
        await verify_campus(user.id)
        chapter = await client.get(
            f"/chapters/{setup.chapter_id}", headers=user.headers
        )
        campus_id = chapter.json()["campus_id"]
        response = await client.post(
            f"/campuses/{campus_id}/posts", headers=user.headers,
            json={"body": "media op", "media_object_names": [name]},
        )
        assert response.status_code == 201, response.text
        assert response.json()["media_urls"] == [expected_url]
        kind = "media_finalize_post"
    else:
        created = await client.post(
            f"/chapters/{setup.chapter_id}/posts", headers=user.headers,
            json={"body": "before edit"},
        )
        assert created.status_code == 201, created.text
        response = await client.patch(
            f"/chapters/{setup.chapter_id}/posts/{created.json()['id']}",
            headers=user.headers, json={"media_object_names": [name]},
        )
        assert response.status_code == 200, response.text
        assert response.json()["media_urls"] == [expected_url]
        kind = "media_finalize_post"

    operations = await _operations(user.id)
    assert len(operations) == 1
    assert operations[0].kind == kind and operations[0].state == "finished"
    assert operations[0].completed_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["org_post", "campus_post", "edit_post"])
async def test_hold_during_feed_media_worker_blocks_followup_rows(
    route: str,
    client,
    make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    _configure_bucket(monkeypatch)
    setup = await make_chapter_with("member")
    user = setup.member
    post = None
    if route == "edit_post":
        created = await client.post(
            f"/chapters/{setup.chapter_id}/posts", headers=user.headers,
            json={"body": "before media edit"},
        )
        assert created.status_code == 201, created.text
        post = created.json()
    elif route == "campus_post":
        await verify_campus(user.id)
        chapter = await client.get(
            f"/chapters/{setup.chapter_id}", headers=user.headers
        )
        campus_id = chapter.json()["campus_id"]

    started, release = threading.Event(), threading.Event()

    def finalize(user_id: str, name: str, **kwargs) -> str:
        started.set()
        if not release.wait(8):
            raise AssertionError("test did not release media worker")
        return _final_url(user_id, name, **kwargs)

    import app.routers.feed as feed_router

    monkeypatch.setattr(feed_router, "finalize_media_object", finalize)
    name = _tmp_name(user.id, f"held-{route}.jpg")
    if route == "org_post":
        request = asyncio.create_task(client.post(
            f"/chapters/{setup.chapter_id}/posts", headers=user.headers,
            json={"body": "must not persist", "media_object_names": [name], "post_type": "photo"},
        ))
    elif route == "campus_post":
        request = asyncio.create_task(client.post(
            f"/campuses/{campus_id}/posts", headers=user.headers,
            json={"body": "must not persist", "media_object_names": [name]},
        ))
    else:
        request = asyncio.create_task(client.patch(
            f"/chapters/{setup.chapter_id}/posts/{post['id']}", headers=user.headers,
            json={"body": "must not persist", "media_object_names": [name]},
        ))
    try:
        assert await asyncio.to_thread(started.wait, 5), "media worker did not start"
        assert get_engine().sync_engine.pool.checkedout() == 0
        await _hold(user.id)
        operations = await _operations(user.id)
        assert len(operations) == 1 and operations[0].state == "running"
        release.set()
        response = await request
        assert response.status_code == 423, response.text
        operations = await _operations(user.id)
        assert len(operations) == 1 and operations[0].state == "finished"
        async with get_session_factory()() as session:
            if route == "edit_post":
                stored = await session.get(models.Post, uuid.UUID(post["id"]))
                assert stored.body == "before media edit"
                assert stored.media_urls is None
            else:
                count = await session.scalar(
                    select(models.Post.id).where(models.Post.author_id == uuid.UUID(user.id))
                )
                assert count is None
    finally:
        release.set()
        if not request.done():
            await asyncio.gather(request, return_exceptions=True)


@pytest.mark.asyncio
async def test_duplicate_effect_is_rejected_without_second_provider_call(
    client, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    _configure_bucket(monkeypatch)
    user = await make_user("Duplicate media actor")
    calls: list[str] = []

    import app.routers.auth as auth_router

    def finalize(user_id: str, name: str, **kwargs) -> str:
        calls.append(name)
        return _final_url(user_id, name, **kwargs)

    monkeypatch.setattr(auth_router, "finalize_media_object", finalize)
    name = _tmp_name(user.id, "repeat.jpg")
    first = await _avatar(client, user, name)
    second = await _avatar(client, user, name)

    assert first.status_code == 200, first.text
    assert second.status_code == 409, second.text
    assert second.json() == {"detail": "media_operation_already_started"}
    assert calls == [name]
    operations = await _operations(user.id)
    assert len(operations) == 1 and operations[0].state == "finished"


@pytest.mark.asyncio
async def test_provider_failure_stays_uncertain_and_cannot_be_retried_by_time(
    client, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    _configure_bucket(monkeypatch)
    user = await make_user("Uncertain media actor")
    calls: list[str] = []

    import app.routers.auth as auth_router

    def fail_finalize(user_id: str, name: str, **kwargs) -> str:
        calls.append(name)
        raise RuntimeError("provider result was ambiguous")

    monkeypatch.setattr(auth_router, "finalize_media_object", fail_finalize)
    name = _tmp_name(user.id, "uncertain.jpg")
    with pytest.raises(RuntimeError, match="provider result was ambiguous"):
        await _avatar(client, user, name)
    operations = await _operations(user.id)
    assert len(operations) == 1 and operations[0].state == "uncertain"
    assert operations[0].completed_at is not None

    # An old timestamp does not clear an ambiguous provider outcome or
    # authorize a second external attempt with the same logical effect key.
    async with get_session_factory()() as session:
        await session.execute(
            update(models.AccountExternalOperation)
            .where(models.AccountExternalOperation.id == operations[0].id)
            .values(created_at=datetime.now(timezone.utc) - timedelta(days=3650))
        )
        await session.commit()
    duplicate = await _avatar(client, user, name)
    assert duplicate.status_code == 409, duplicate.text
    assert calls == [name]
    assert len(await _operations(user.id)) == 1


@pytest.mark.asyncio
async def test_completion_write_failure_leaves_running_operation_blocking(
    make_user: MakeUser, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    _configure_bucket(monkeypatch)
    user = await make_user("Media completion write failure")

    import app.services.account_media_operations as media_operations
    from app.services.account_external_operations import inspect_registered_operation_drain

    async def fail_completion(*_args, **_kwargs):
        raise RuntimeError("completion database write failed")

    monkeypatch.setattr(media_operations, "finish_external_operation", fail_completion)
    with pytest.raises(RuntimeError, match="completion database write failed"):
        await finalize_account_media(
            uuid.UUID(user.id), user.firebase_uid, _tmp_name(user.id, "completion.jpg"),
            finalize=_final_url,
        )
    operations = await _operations(user.id)
    assert len(operations) == 1 and operations[0].state == "running"
    request_id = await _hold(user.id)
    drain = await inspect_registered_operation_drain(
        user_id=uuid.UUID(user.id), request_id=request_id
    )
    assert drain.running_count == 1 and drain.uncertain_count == 0
    assert not drain.registered_operations_drained


@pytest.mark.asyncio
async def test_flag_off_keeps_unjournaled_media_behavior_without_registry_sql(
    client, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, False)
    _configure_bucket(monkeypatch)
    user = await make_user("Legacy media actor")
    await _hold(user.id)
    calls: list[str] = []

    import app.routers.auth as auth_router
    import app.services.account_media_operations as media_operations

    monkeypatch.setattr(
        auth_router, "finalize_media_object",
        lambda user_id, name, **kwargs: calls.append(name) or _final_url(user_id, name, **kwargs),
    )
    monkeypatch.setattr(
        media_operations, "register_external_operation",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("registry must stay off")),
    )
    statements: list[str] = []

    def record_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    engine = get_engine()
    event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
    try:
        name = _tmp_name(user.id, "legacy.jpg")
        response = await _avatar(client, user, name)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_sql)

    expected = f"https://storage.googleapis.com/{TEST_BUCKET}/avatars/{user.id}/legacy.jpg"
    assert response.status_code == 200, response.text
    assert response.json()["avatar_url"] == expected
    assert calls == [name]
    assert not any("account_external_operation" in statement for statement in statements)


@pytest.mark.asyncio
async def test_cleanup_retains_operation_control_rows_but_export_excludes_them(
    make_user: MakeUser, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    _configure_bucket(monkeypatch)
    user = await make_user("Retained media operation actor")
    name = _tmp_name(user.id, "retained.jpg")
    await finalize_account_media(
        uuid.UUID(user.id), user.firebase_uid, name,
        finalize=_final_url,
    )
    before = await _operations(user.id)
    assert len(before) == 1 and before[0].state == "finished"

    from app.services.account_cleanup import cleanup_local_account
    from app.services.account_data import EXPORT_EXCLUDED, build_export

    async with get_session_factory()() as session:
        stored_user = await session.get(models.User, uuid.UUID(user.id))
        result = await cleanup_local_account(session, uuid.UUID(user.id))
        await session.commit()
        payload = await build_export(session, stored_user)

    assert "account_external_operations" in result["coverage"]["retained"]
    assert "internal operation ownership tokens and coordination records" in EXPORT_EXCLUDED
    after = await _operations(user.id)
    assert len(after) == 1
    assert after[0].id == before[0].id
    assert after[0].owner_token == before[0].owner_token
    assert after[0].effect_key == before[0].effect_key
    serialized = str(payload)
    assert str(before[0].owner_token) not in serialized
    assert str(before[0].id) not in serialized
    assert before[0].effect_key not in serialized

