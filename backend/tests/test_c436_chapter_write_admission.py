"""Deletion-hold admission for chapter and membership mutations."""
from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import func, select

from app import models
from app.config import get_settings
from app.db import get_session_factory
from app.services.account_deletion_hold import activate_deletion_hold
from app.routers import chapters
from tests.conftest import _grant_platform_admin
from tests.conftest import MakeChapterWith, MakeCampus, MakeUser, set_campus


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
            user_id=uuid.UUID(user_id), kind="deletion", open_key="chapter-test"
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


async def _chapter_count() -> int:
    async with get_session_factory()() as session:
        return int(await session.scalar(select(func.count()).select_from(models.Chapter)) or 0)


@pytest.mark.asyncio
async def test_held_founder_is_rejected_before_chapter_rows(
    client: Any,
    make_user: MakeUser,
    make_campus: MakeCampus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    user = await make_user("held founder")
    await set_campus(user.id, await make_campus())
    before = await _chapter_count()
    await _hold(user.id)
    response = await client.post(
        "/chapters",
        json={"org_name": "Held Org", "chapter_name": "Alpha"},
        headers=user.headers,
    )
    assert response.status_code == 423, response.text
    assert await _chapter_count() == before


@pytest.mark.asyncio
async def test_held_joiner_is_rejected_before_membership_or_seat_change(
    client: Any,
    make_user: MakeUser,
    make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("president")
    invite = await client.post(
        f"/chapters/{setup.chapter_id}/invites",
        json={"role": "member", "max_uses": 1},
        headers=setup.president.headers,
    )
    assert invite.status_code == 201, invite.text
    joiner = await make_user("held joiner")
    await _hold(joiner.id)
    response = await client.post(
        "/chapters/join", json={"code": invite.json()["code"]}, headers=joiner.headers
    )
    assert response.status_code == 423, response.text
    async with get_session_factory()() as session:
        assert await session.scalar(
            select(models.Membership.id).where(
                models.Membership.chapter_id == uuid.UUID(setup.chapter_id),
                models.Membership.user_id == uuid.UUID(joiner.id),
            )
        ) is None


@pytest.mark.asyncio
async def test_target_hold_blocks_member_role_and_role_term_mutation(
    client: Any,
    make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("member")
    await _hold(setup.member.id)
    response = await client.patch(
        f"/chapters/{setup.chapter_id}/members",
        json={"user_id": setup.member.id, "role": "treasurer"},
        headers=setup.president.headers,
    )
    assert response.status_code == 423, response.text
    async with get_session_factory()() as session:
        membership = await session.scalar(
            select(models.Membership).where(
                models.Membership.chapter_id == uuid.UUID(setup.chapter_id),
                models.Membership.user_id == uuid.UUID(setup.member.id),
            )
        )
        terms = list(
            (
                await session.execute(
                    select(models.RoleTerm).where(
                        models.RoleTerm.membership_id == membership.id
                    )
                )
            ).scalars()
        )
    assert membership.role == "member"
    assert len(terms) == 1


@pytest.mark.asyncio
async def test_unauthorized_caller_does_not_learn_target_hold(
    client: Any,
    make_chapter_with: MakeChapterWith,
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("member")
    stranger = await make_user("chapter stranger")
    await _hold(setup.member.id)
    response = await client.patch(
        f"/chapters/{setup.chapter_id}/members",
        json={"user_id": setup.member.id, "role": "treasurer"},
        headers=stranger.headers,
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "not_a_member"}


@pytest.mark.asyncio
async def test_flag_off_preserves_chapter_write_behavior(
    client: Any,
    make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, False)
    setup = await make_chapter_with("member")
    await _hold(setup.president.id)
    response = await client.patch(
        f"/chapters/{setup.chapter_id}",
        json={"org_name": "Still Allowed Offline"},
        headers=setup.president.headers,
    )
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_held_actor_is_rejected_by_update_chapter_invite_and_revoke(
    client: Any,
    make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("president")
    invite = await client.post(
        f"/chapters/{setup.chapter_id}/invites",
        json={"role": "member", "max_uses": 2},
        headers=setup.president.headers,
    )
    assert invite.status_code == 201, invite.text
    code = invite.json()["code"]
    await _hold(setup.president.id)

    update = await client.patch(
        f"/chapters/{setup.chapter_id}",
        json={"org_name": "blocked update"},
        headers=setup.president.headers,
    )
    mint = await client.post(
        f"/chapters/{setup.chapter_id}/invites",
        json={"role": "member", "max_uses": 1},
        headers=setup.president.headers,
    )
    revoke = await client.post(
        f"/chapters/{setup.chapter_id}/invites/revoke",
        json={"code": code},
        headers=setup.president.headers,
    )
    assert update.status_code == mint.status_code == revoke.status_code == 423
    async with get_session_factory()() as session:
        chapter = await session.get(models.Chapter, uuid.UUID(setup.chapter_id))
        stored = await session.scalar(
            select(models.ChapterInvite).where(models.ChapterInvite.code == code)
        )
        invite_count = await session.scalar(
            select(func.count()).select_from(models.ChapterInvite).where(
                models.ChapterInvite.chapter_id == uuid.UUID(setup.chapter_id)
            )
        )
    assert chapter.org_name != "blocked update"
    assert stored.revoked_at is None
    assert invite_count == 1


@pytest.mark.asyncio
async def test_held_platform_admin_is_rejected_before_approval_audit(
    client: Any,
    make_chapter_with: MakeChapterWith,
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("member", approve_moderation=False)
    admin = await make_user("held chapter platform admin")
    await _grant_platform_admin(admin.id)
    await _hold(admin.id)
    response = await client.patch(
        f"/chapters/{setup.chapter_id}/moderation-approval",
        json={"approved": True, "reason": "blocked"},
        headers=admin.headers,
    )
    assert response.status_code == 423, response.text
    async with get_session_factory()() as session:
        chapter = await session.get(models.Chapter, uuid.UUID(setup.chapter_id))
        audits = await session.scalar(
            select(func.count()).select_from(models.ModerationAction).where(
                models.ModerationAction.target_id == uuid.UUID(setup.chapter_id),
                models.ModerationAction.action == "approve_chapter",
            )
        )
    assert chapter.moderation_approved is False
    assert audits == 0


@pytest.mark.asyncio
async def test_role_loss_after_preflight_blocks_member_update_without_mutation(
    client: Any,
    make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("member")
    async with get_session_factory()() as session:
        target_before = await session.scalar(
            select(models.Membership.role).where(
                models.Membership.chapter_id == uuid.UUID(setup.chapter_id),
                models.Membership.user_id == uuid.UUID(setup.member.id),
            )
        )
        term_count_before = await session.scalar(
            select(func.count()).select_from(models.RoleTerm).join(
                models.Membership,
                models.Membership.id == models.RoleTerm.membership_id,
            ).where(
                models.Membership.chapter_id == uuid.UUID(setup.chapter_id),
                models.Membership.user_id == uuid.UUID(setup.member.id),
            )
        )

    original = chapters.get_current_user
    calls = 0

    async def lose_role(*, uid: str, session: Any) -> models.User:
        nonlocal calls
        calls += 1
        if calls == 2:
            async with get_session_factory()() as mutator:
                await mutator.execute(
                    models.Membership.__table__.update()
                    .where(
                        models.Membership.chapter_id == uuid.UUID(setup.chapter_id),
                        models.Membership.user_id == uuid.UUID(setup.president.id),
                    )
                    .values(role="member")
                )
                await mutator.commit()
        return await original(uid=uid, session=session)

    monkeypatch.setattr(chapters, "get_current_user", lose_role)
    response = await client.patch(
        f"/chapters/{setup.chapter_id}/members",
        json={"user_id": setup.member.id, "role": "treasurer"},
        headers=setup.president.headers,
    )
    assert response.status_code == 403, response.text
    async with get_session_factory()() as session:
        target_after = await session.scalar(
            select(models.Membership.role).where(
                models.Membership.chapter_id == uuid.UUID(setup.chapter_id),
                models.Membership.user_id == uuid.UUID(setup.member.id),
            )
        )
        term_count_after = await session.scalar(
            select(func.count()).select_from(models.RoleTerm).join(
                models.Membership,
                models.Membership.id == models.RoleTerm.membership_id,
            ).where(
                models.Membership.chapter_id == uuid.UUID(setup.chapter_id),
                models.Membership.user_id == uuid.UUID(setup.member.id),
            )
        )
    assert calls == 2
    assert target_after == target_before == "member"
    assert term_count_after == term_count_before
