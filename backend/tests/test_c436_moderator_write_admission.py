"""Moderator mutations respect deletion holds without hiding retained history."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy import event

from app import models
from app.config import get_settings
from app.db import get_session_factory
from app.services.account_deletion_hold import activate_deletion_hold
from tests.conftest import MakeChapterWith, MakeUser, verify_campus
from tests.test_moderation_suspension import _grant_platform_admin


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    yield
    get_settings.cache_clear()


def _enable_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED", "true")
    get_settings.cache_clear()


async def _hold(user_id: str) -> None:
    async with get_session_factory()() as session:
        request = models.AccountDataRequest(
            user_id=uuid.UUID(user_id), kind="deletion", open_key=f"moderation-{user_id}"
        )
        session.add(request)
        await session.flush()
        await session.commit()
        request_id = request.id
    async with get_session_factory()() as session:
        await activate_deletion_hold(session, user_id=uuid.UUID(user_id), request_id=request_id)
        await session.commit()


async def _actions(target_type: str, target_id: str) -> int:
    async with get_session_factory()() as session:
        return int(await session.scalar(text(
            "SELECT count(*) FROM moderation_actions WHERE target_type=:kind AND target_id=:id"
        ), {"kind": target_type, "id": target_id}))


@pytest.mark.asyncio
async def test_held_moderator_cannot_run_any_moderator_mutation(
    client, make_chapter_with: MakeChapterWith, make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_admission(monkeypatch)
    chapter = await make_chapter_with("president")
    await make_user("Report author")
    report_response = await client.post(
        "/moderation/reports", json={"target_type": "user", "reason": "test"},
        headers=chapter.president.headers,
    )
    assert report_response.status_code == 201, report_response.text
    report_id = report_response.json()["id"]

    async with get_session_factory()() as session:
        campus_id = await session.scalar(text("SELECT campus_id FROM chapters WHERE id=:id"),
                                         {"id": chapter.chapter_id})
        chirp = models.Chirp(author_id=uuid.UUID(chapter.president.id), campus_id=campus_id,
                             body="retained")
        post = models.Post(author_id=uuid.UUID(chapter.president.id), campus_id=campus_id,
                           chapter_id=uuid.UUID(chapter.chapter_id), body="retained")
        session.add_all([chirp, post])
        await session.commit()
        chirp_id, post_id = str(chirp.id), str(post.id)

    await _grant_platform_admin(chapter.president.id)
    await _hold(chapter.president.id)

    responses = [
        await client.patch(f"/moderation/reports/{report_id}",
                           json={"status": "actioned", "reason": "reviewed"},
                           headers=chapter.president.headers),
        await client.post(f"/moderation/chirps/{chirp_id}/remove", json={"reason": "reviewed"},
                          headers=chapter.president.headers),
        await client.post("/moderation/content/remove",
                          json={"target_type": "post", "target_id": post_id, "reason": "reviewed"},
                          headers=chapter.president.headers),
        await client.post(f"/moderation/users/{chapter.president.id}/suspend",
                          json={"reason": "reviewed"}, headers=chapter.president.headers),
        await client.post(f"/moderation/users/{chapter.president.id}/unsuspend",
                          json={"reason": "reviewed"}, headers=chapter.president.headers),
    ]
    assert [response.status_code for response in responses] == [423] * 5
    read_response = await client.get("/moderation/reports", headers=chapter.president.headers)
    assert read_response.status_code == 200
    async with get_session_factory()() as session:
        report = await session.get(models.ContentReport, uuid.UUID(report_id))
        stored_chirp = await session.get(models.Chirp, uuid.UUID(chirp_id))
        stored_post = await session.get(models.Post, uuid.UUID(post_id))
        actor = await session.get(models.User, uuid.UUID(chapter.president.id))
        assert report.status == "open"
        assert stored_chirp.removed_at is None
        assert stored_post.deleted_at is None
        assert actor.suspended_at is None
    assert await _actions("report", report_id) == 0
    assert await _actions("chirp", chirp_id) == 0
    assert await _actions("post", post_id) == 0
    assert await _actions("user", chapter.president.id) == 0


@pytest.mark.asyncio
async def test_held_target_does_not_block_moderator_removal_or_reads(
    client, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_admission(monkeypatch)
    chapter = await make_chapter_with("member")
    await verify_campus(chapter.president.id)
    await _hold(chapter.member.id)

    chirp_response = await client.post(
        f"/campuses/{(await client.get(f'/chapters/{chapter.chapter_id}', headers=chapter.president.headers)).json()['campus_id']}/chirps",
        json={"body": "held author"}, headers=chapter.member.headers,
    )
    # The held author cannot publish after the hold. Seed retained authored records
    # directly to exercise moderation against a preexisting safety/history record.
    assert chirp_response.status_code == 423
    async with get_session_factory()() as session:
        campus_id = await session.scalar(text("SELECT campus_id FROM chapters WHERE id=:id"),
                                         {"id": chapter.chapter_id})
        chirp = models.Chirp(author_id=uuid.UUID(chapter.member.id), campus_id=campus_id,
                             body="retained")
        post = models.Post(author_id=uuid.UUID(chapter.member.id), campus_id=campus_id,
                           chapter_id=uuid.UUID(chapter.chapter_id), body="retained")
        session.add_all([chirp, post])
        await session.commit()
        chirp_id, post_id = str(chirp.id), str(post.id)

    chirp_response = await client.post(
        f"/moderation/chirps/{chirp_id}/remove", json={"reason": "safety"},
        headers=chapter.president.headers,
    )
    post_response = await client.post(
        "/moderation/content/remove",
        json={"target_type": "post", "target_id": post_id, "reason": "safety"},
        headers=chapter.president.headers,
    )
    read_response = await client.get("/moderation/reports", headers=chapter.president.headers)
    assert chirp_response.status_code == 204
    assert post_response.status_code == 204
    assert read_response.status_code == 200
    assert await _actions("chirp", chirp_id) == 1
    assert await _actions("post", post_id) == 1


@pytest.mark.asyncio
async def test_unsuspend_of_held_target_is_rejected_without_audit(
    client, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_admission(monkeypatch)
    admin = await make_user("Platform admin")
    target = await make_user("Suspended held target")
    await _grant_platform_admin(admin.id)
    async with get_session_factory()() as session:
        await session.execute(text("UPDATE users SET suspended_at=now(), suspension_reason='prior' WHERE id=:id"),
                              {"id": target.id})
        await session.commit()
    await _hold(target.id)

    response = await client.post(
        f"/moderation/users/{target.id}/unsuspend", json={"reason": "appeal"},
        headers=admin.headers,
    )
    assert response.status_code == 423, response.text
    async with get_session_factory()() as session:
        row = await session.get(models.User, uuid.UUID(target.id))
        assert row.suspended_at is not None
        assert row.suspension_reason == "prior"
    assert await _actions("user", target.id) == 0

    non_admin = await make_user("Non admin held target caller")
    private_response = await client.post(
        f"/moderation/users/{target.id}/unsuspend", json={"reason": "appeal"},
        headers=non_admin.headers,
    )
    assert private_response.status_code == 403
    assert private_response.json() == {"detail": "platform_admin_required"}


@pytest.mark.asyncio
async def test_flag_off_keeps_legacy_moderator_write_without_hold_query(
    client, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED", "false")
    get_settings.cache_clear()
    chapter = await make_chapter_with("president")
    await verify_campus(chapter.president.id)
    response = await client.post(
        "/moderation/reports", json={"target_type": "user", "reason": "test"},
        headers=chapter.president.headers,
    )
    assert response.status_code == 201, response.text
    report_id = response.json()["id"]
    statements: list[str] = []

    def record_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    from app.db import get_engine
    engine = get_engine()
    event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
    try:
        resolved = await client.patch(
            f"/moderation/reports/{report_id}",
            json={"status": "dismissed", "reason": "reviewed"},
            headers=chapter.president.headers,
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_sql)
    assert resolved.status_code == 200, resolved.text
    assert not any("account_deletion_holds" in statement for statement in statements)
