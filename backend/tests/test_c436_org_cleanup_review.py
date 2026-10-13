"""Cleanup boundary: retained organization rows stay readable and immutable for held targets."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app import models
from app.config import get_settings
from app.db import get_session_factory
from app.services.account_cleanup import cleanup_local_account
from app.services.account_deletion_hold import activate_deletion_hold


def _set_admission(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setenv("ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED", str(enabled).lower())
    get_settings.cache_clear()


async def _hold(user_id: str) -> None:
    async with get_session_factory()() as session:
        request = models.AccountDataRequest(
            user_id=uuid.UUID(user_id),
            kind="deletion",
            open_key=f"cleanup-review-{uuid.uuid4()}",
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


@pytest.fixture(autouse=True)
def _clear_settings():
    yield
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_cleanup_retains_org_history_and_rejects_reactivation(
    client, make_chapter_with, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("member")
    meeting = await client.post(
        f"/chapters/{setup.chapter_id}/meetings",
        headers=setup.president.headers,
        json={
            "title": "Retained minutes",
            "meeting_date": datetime.now(timezone.utc).isoformat(),
        },
    )
    assert meeting.status_code == 201, meeting.text
    meeting_id = meeting.json()["id"]
    target_id = uuid.UUID(setup.member.id)
    chapter_id = uuid.UUID(setup.chapter_id)
    await _hold(setup.member.id)

    async with get_session_factory()() as session:
        await cleanup_local_account(session, target_id)
        await session.commit()

    async with get_session_factory()() as session:
        membership = await session.scalar(
            select(models.Membership).where(
                models.Membership.chapter_id == chapter_id,
                models.Membership.user_id == target_id,
            )
        )
        assert membership is not None and membership.status == "removed"
        terms = list(
            (
                await session.scalars(
                    select(models.RoleTerm).where(
                        models.RoleTerm.membership_id == membership.id
                    )
                )
            ).all()
        )
        assert len(terms) == 1 and terms[0].ended_at is not None

    reactivation = await client.patch(
        f"/chapters/{setup.chapter_id}/members",
        headers=setup.president.headers,
        json={"user_id": setup.member.id, "status": "active", "role": "treasurer"},
    )
    assert reactivation.status_code == 423, reactivation.text
    async with get_session_factory()() as session:
        membership = await session.scalar(
            select(models.Membership).where(
                models.Membership.chapter_id == chapter_id,
                models.Membership.user_id == target_id,
            )
        )
        terms = list(
            (
                await session.scalars(
                    select(models.RoleTerm).where(
                        models.RoleTerm.membership_id == membership.id
                    )
                )
            ).all()
        )
        assert membership.status == "removed"
        assert membership.role == "member"
        assert len(terms) == 1 and terms[0].ended_at is not None

    retained = await client.get(
        f"/chapters/{setup.chapter_id}/meetings/{meeting_id}",
        headers=setup.president.headers,
    )
    assert retained.status_code == 200, retained.text
    assert retained.json()["title"] == "Retained minutes"


@pytest.mark.asyncio
async def test_cleanup_does_not_allow_held_actor_to_recreate_named_block(
    client, make_chapter_with, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("member")
    target_id = uuid.UUID(setup.member.id)
    created = await client.post(
        "/moderation/blocks",
        headers=setup.member.headers,
        json={"blocked_id": setup.president.id},
    )
    assert created.status_code == 201, created.text
    await _hold(setup.member.id)
    async with get_session_factory()() as session:
        await cleanup_local_account(session, target_id)
        await session.commit()
    async with get_session_factory()() as session:
        assert await session.get(
            models.UserBlock, (target_id, uuid.UUID(setup.president.id))
        ) is None

    response = await client.post(
        "/moderation/blocks",
        headers=setup.member.headers,
        json={"blocked_id": setup.president.id},
    )
    assert response.status_code == 423, response.text
    async with get_session_factory()() as session:
        block = await session.get(
            models.UserBlock, (target_id, uuid.UUID(setup.president.id))
        )
        assert block is None
