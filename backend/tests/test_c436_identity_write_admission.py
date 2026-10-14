"""Profile, campus verification, and house writes honor account deletion holds."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.config import get_settings
from app.core.windows import current_week_start
from app.db import get_session_factory
from app.services.account_deletion_hold import activate_deletion_hold
from tests.conftest import MakeCampus, MakeUser, set_campus


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    yield
    get_settings.cache_clear()


def _admission(monkeypatch: pytest.MonkeyPatch, enabled: bool = True) -> None:
    monkeypatch.setenv("ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED", str(enabled).lower())
    get_settings.cache_clear()


async def _hold(user_id: str) -> None:
    async with get_session_factory()() as session:
        request = models.AccountDataRequest(
            user_id=uuid.UUID(user_id), kind="deletion", open_key=f"social-{user_id}"
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


async def _make_chapter(campus_id: str) -> str:
    async with get_session_factory()() as session:
        chapter_id = await session.scalar(
            text("INSERT INTO chapters (campus_id, org_name) VALUES (:campus, :name) RETURNING id"),
            {"campus": campus_id, "name": f"Held House {uuid.uuid4().hex[:8]}"},
        )
        await session.commit()
    return str(chapter_id)


async def test_profile_hold_blocks_field_changes_and_avatar_provider(
    client, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _admission(monkeypatch)
    user = await make_user("Profile before hold")
    await _hold(user.id)

    provider_calls: list[tuple] = []
    analytics_calls: list[tuple] = []
    import app.routers.auth as auth_router

    monkeypatch.setattr(auth_router, "finalize_media_object", lambda *args, **kwargs: provider_calls.append((args, kwargs)))
    monkeypatch.setattr(auth_router, "emit", lambda *args, **kwargs: analytics_calls.append((args, kwargs)))
    response = await client.patch(
        "/auth/me",
        headers=user.headers,
        json={
            "display_name": "Changed while held",
            "account_type": "alumni",
            "avatar_object_name": f"tmp/{user.id}/avatar.jpg",
        },
    )
    assert response.status_code == 423, response.text
    assert provider_calls == []
    assert analytics_calls == []
    async with get_session_factory()() as session:
        stored = await session.get(models.User, uuid.UUID(user.id))
        assert stored.display_name == "Profile before hold"
        assert stored.account_type == "greek"
        assert stored.avatar_url is None

    # The owner-facing profile read stays available during a hold.
    read_response = await client.get("/auth/me", headers=user.headers)
    assert read_response.status_code == 200, read_response.text


@pytest.mark.asyncio
async def test_campus_verification_writes_stop_before_mail_and_redemption(
    client, make_user: MakeUser, make_campus: MakeCampus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _admission(monkeypatch)
    campus_id = await make_campus()
    user = await make_user("Held verification user")
    async with get_session_factory()() as session:
        await session.execute(
            text("UPDATE campuses SET email_domains = ARRAY['example.edu'] WHERE id=:id"),
            {"id": campus_id},
        )
        verification_id = uuid.uuid4()
        from app.services.campus_verification import _hash_code

        session.add(models.CampusVerification(
            id=verification_id,
            user_id=uuid.UUID(user.id),
            campus_id=uuid.UUID(campus_id),
            edu_email="student@example.edu",
            code_hash=_hash_code(verification_id, "123456"),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        ))
        await session.commit()

    email_calls: list[dict] = []
    import app.services.campus_verification as verification_service

    async def fake_send_email(**kwargs):
        email_calls.append(kwargs)

    monkeypatch.setattr(verification_service.email_service, "send_email", fake_send_email)
    await _hold(user.id)

    start = await client.post(
        "/auth/campus-verification", headers=user.headers,
        json={"edu_email": "student@example.edu"},
    )
    redeem = await client.post(
        "/auth/campus-verification/redeem", headers=user.headers,
        json={"code": "123456"},
    )
    assert start.status_code == 423, start.text
    assert redeem.status_code == 423, redeem.text
    assert email_calls == []
    async with get_session_factory()() as session:
        stored = await session.get(models.CampusVerification, verification_id)
        stored_user = await session.get(models.User, uuid.UUID(user.id))
        assert stored.consumed_at is None
        assert stored.attempts == 0
        assert stored_user.campus_id is None
        assert stored_user.campus_verified_at is None

    # Verification status is a read and remains available.
    status = await client.get("/auth/campus-verification", headers=user.headers)
    assert status.status_code == 200, status.text
    assert status.json()["verified"] is False


@pytest.mark.asyncio
async def test_held_house_voters_cannot_insert_or_replace_ballots_but_can_read(
    client, make_user: MakeUser, make_campus: MakeCampus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _admission(monkeypatch)
    campus_id = await make_campus()
    first_house = await _make_chapter(campus_id)
    second_house = await _make_chapter(campus_id)
    replace_voter = await make_user("Held replace voter")
    insert_voter = await make_user("Held insert voter")
    await set_campus(replace_voter.id, campus_id)
    await set_campus(insert_voter.id, campus_id)
    async with get_session_factory()() as session:
        existing = models.HouseBallot(
            campus_id=uuid.UUID(campus_id), week_start=current_week_start(),
            voter_id=uuid.UUID(replace_voter.id), touse_chapter_id=uuid.UUID(first_house),
        )
        session.add(existing)
        await session.commit()
    await _hold(replace_voter.id)
    await _hold(insert_voter.id)

    replace = await client.put(
        f"/campuses/{campus_id}/house-ballot", headers=replace_voter.headers,
        json={"touse_chapter_id": second_house},
    )
    insert = await client.put(
        f"/campuses/{campus_id}/house-ballot", headers=insert_voter.headers,
        json={"touse_chapter_id": first_house},
    )
    assert replace.status_code == 423, replace.text
    assert insert.status_code == 423, insert.text
    async with get_session_factory()() as session:
        ballots = list((await session.scalars(
            select(models.HouseBallot).where(models.HouseBallot.campus_id == uuid.UUID(campus_id))
        )).all())
        assert len(ballots) == 1
        assert ballots[0].voter_id == uuid.UUID(replace_voter.id)
        assert ballots[0].touse_chapter_id == uuid.UUID(first_house)

    read = await client.get(
        f"/campuses/{campus_id}/house-leaderboard", headers=replace_voter.headers,
    )
    assert read.status_code == 200, read.text


@pytest.mark.asyncio
async def test_default_off_keeps_profile_write_compatibility(
    client, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _admission(monkeypatch, enabled=False)
    user = await make_user("Legacy profile")
    await _hold(user.id)
    response = await client.patch(
        "/auth/me", headers=user.headers, json={"display_name": "Legacy update"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["display_name"] == "Legacy update"


@pytest.mark.asyncio
async def test_profile_response_survives_hold_activated_after_commit(
    client, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _admission(monkeypatch)
    user = await make_user("Before post-commit race")
    original_commit = AsyncSession.commit
    state = {"activated": False}

    async def commit_then_activate(session: AsyncSession) -> None:
        should_activate = any(
            isinstance(row, models.User)
            and row.id == uuid.UUID(user.id)
            and row.display_name == "Committed before new hold"
            for row in session.identity_map.values()
        )
        await original_commit(session)
        if should_activate and not state["activated"]:
            state["activated"] = True
            await _hold(user.id)

    monkeypatch.setattr(AsyncSession, "commit", commit_then_activate)
    response = await client.patch(
        "/auth/me", headers=user.headers,
        json={"display_name": "Committed before new hold"},
    )
    assert state["activated"] is True
    assert response.status_code == 200, response.text
    assert response.json()["display_name"] == "Committed before new hold"

    following = await client.patch(
        "/auth/me", headers=user.headers,
        json={"display_name": "Must stay unchanged"},
    )
    assert following.status_code == 423, following.text
    async with get_session_factory()() as session:
        stored = await session.get(models.User, uuid.UUID(user.id))
        assert stored.display_name == "Committed before new hold"
