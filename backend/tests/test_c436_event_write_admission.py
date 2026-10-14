"""Deletion-hold admission for retained event writes."""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from fastapi import Depends, Path
from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.config import get_settings
from app.db import get_engine, get_session, get_session_factory
from app.middleware.auth import get_current_user_for_write
from app.middleware.org_scope import get_current_membership_for_write
from app.routers import events
from app.services.account_cleanup import cleanup_local_account
from app.services.account_deletion_hold import activate_deletion_hold
from tests.conftest import MakeChapterWith, MakeUser
from tests.test_c436_deletion_hold import _observe_blocked


def _set_admission(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setenv("ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED", str(enabled).lower())
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clear_settings() -> None:
    yield
    get_settings.cache_clear()


async def _deletion_request(user_id: str) -> uuid.UUID:
    async with get_session_factory()() as session:
        request = models.AccountDataRequest(
            user_id=uuid.UUID(user_id), kind="deletion", open_key="event-test"
        )
        session.add(request)
        await session.flush()
        await session.commit()
        return request.id


async def _hold(user_id: str) -> None:
    request_id = await _deletion_request(user_id)
    async with get_session_factory()() as session:
        await activate_deletion_hold(
            session, user_id=uuid.UUID(user_id), request_id=request_id
        )
        await session.commit()


def _event_body(title: str = "Held mixer") -> dict[str, str]:
    return {
        "title": title,
        "starts_at": "2026-11-01T19:00:00Z",
        "location": "Chapter House",
        "cover_url": "https://example.com/event.jpg",
        "visibility": "public",
    }


@pytest.mark.asyncio
async def test_held_actor_cannot_create_edit_cancel_or_rsvp_but_can_read(
    client: Any,
    make_chapter_with: MakeChapterWith,
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("member")
    invitee = await make_user("held event inviter target")
    created = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        headers=setup.member.headers,
        json={**_event_body(), "visibility": "chapter"},
    )
    assert created.status_code == 201, created.text
    event_id = created.json()["id"]
    await _hold(setup.member.id)

    create = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        headers=setup.member.headers,
        json=_event_body("Must not be created"),
    )
    edit = await client.patch(
        f"/events/{event_id}", headers=setup.member.headers,
        json={"title": "Must not change"},
    )
    cancel = await client.post(f"/events/{event_id}/cancel", headers=setup.member.headers)
    rsvp = await client.put(
        f"/events/{event_id}/rsvps", headers=setup.member.headers,
        json={"status": "going"},
    )
    invite = await client.post(
        f"/events/{event_id}/invites", headers=setup.member.headers,
        json={"user_ids": [invitee.id]},
    )
    assert [response.status_code for response in (create, edit, cancel, rsvp, invite)] == [
        423,
    ] * 5

    # Admission is write-only: ordinary visibility and event history remain available.
    read = await client.get(f"/events/{event_id}", headers=setup.member.headers)
    listed = await client.get(
        f"/chapters/{setup.chapter_id}/events", headers=setup.member.headers
    )
    assert read.status_code == 200, read.text
    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json()] == [event_id]
    async with get_session_factory()() as session:
        count = await session.scalar(
            select(func.count()).select_from(models.Event).where(
                models.Event.chapter_id == uuid.UUID(setup.chapter_id)
            )
        )
        event = await session.get(models.Event, uuid.UUID(event_id))
        rsvp_count = await session.scalar(
            select(func.count()).select_from(models.EventRsvp).where(
                models.EventRsvp.event_id == uuid.UUID(event_id)
            )
        )
        invite_count = await session.scalar(
            select(func.count()).select_from(models.EventInvite).where(
                models.EventInvite.event_id == uuid.UUID(event_id)
            )
        )
    assert count == 1
    assert event is not None and event.title == "Held mixer" and event.canceled_at is None
    assert rsvp_count == 0
    assert invite_count == 0


@pytest.mark.asyncio
async def test_held_host_does_not_freeze_officer_event_history(
    client: Any,
    make_chapter_with: MakeChapterWith,
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("member")
    invitee = await make_user("retained event invitee")
    created = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        headers=setup.member.headers,
        json={**_event_body(), "visibility": "chapter"},
    )
    assert created.status_code == 201, created.text
    event_id = uuid.UUID(created.json()["id"])
    rsvp = await client.put(
        f"/events/{event_id}/rsvps",
        headers=setup.member.headers,
        json={"status": "going"},
    )
    assert rsvp.status_code == 200, rsvp.text
    invited = await client.post(
        f"/events/{event_id}/invites",
        headers=setup.member.headers,
        json={"user_ids": [invitee.id]},
    )
    assert invited.status_code == 201, invited.text
    assert invited.json()["created_count"] == 1
    await _hold(setup.member.id)
    async with get_session_factory()() as session:
        await cleanup_local_account(session, uuid.UUID(setup.member.id))
        await session.commit()

    canceled = await client.post(
        f"/events/{event_id}/cancel", headers=setup.president.headers
    )
    assert canceled.status_code == 200, canceled.text
    assert canceled.json()["canceled_at"] is not None
    async with get_session_factory()() as session:
        retained = await session.get(models.Event, event_id)
        retained_rsvp = await session.get(
            models.EventRsvp, (event_id, uuid.UUID(setup.member.id))
        )
        retained_invite = await session.get(
            models.EventInvite, (event_id, uuid.UUID(invitee.id))
        )
    assert retained is not None
    assert retained.host_id == uuid.UUID(setup.member.id)
    assert retained.canceled_at is not None
    assert retained_rsvp is not None and retained_rsvp.status == "going"
    assert retained_invite is not None


@pytest.mark.asyncio
async def test_held_invitee_is_silently_filtered_from_atomic_batch(
    client: Any,
    make_chapter_with: MakeChapterWith,
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("member")
    invited = await make_user("eligible event invitee")
    held = await make_user("held event invitee")
    tombstoned = await make_user("completed deletion invitee")
    created = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        headers=setup.member.headers,
        json={**_event_body(), "visibility": "chapter"},
    )
    assert created.status_code == 201, created.text
    event_id = created.json()["id"]
    async with get_session_factory()() as session:
        await session.execute(
            text(
                "UPDATE users SET firebase_uid = :uid, email = :email, "
                "display_name = 'Deleted account', avatar_url = NULL, campus_id = NULL, "
                "campus_verified_at = NULL, is_ghost = true, is_platform_admin = false "
                "WHERE id = :id"
            ),
            {
                "uid": f"deleted:{tombstoned.id}",
                "email": f"deleted+{tombstoned.id}@invalid.chirp",
                "id": tombstoned.id,
            },
        )
        await session.commit()
    # Pause between separate-session preflight and request-session guard acquisition.
    # A target entering a hold in this window must be silently filtered under the
    # subsequently acquired candidate locks, without rejecting the eligible target.
    entered, release = asyncio.Event(), asyncio.Event()
    original_get_user = events.get_current_user
    request_session_calls = 0

    async def pause_before_guard_revalidation(*, uid: str, session: AsyncSession):
        nonlocal request_session_calls
        request_session_calls += 1
        if request_session_calls == 2:
            entered.set()
            await release.wait()
        return await original_get_user(uid=uid, session=session)

    monkeypatch.setattr(events, "get_current_user", pause_before_guard_revalidation)
    request_task = asyncio.create_task(client.post(
        f"/events/{event_id}/invites",
        headers=setup.member.headers,
        json={"user_ids": [invited.id, held.id, tombstoned.id]},
    ))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        await _hold(held.id)
        release.set()
        response = await asyncio.wait_for(request_task, timeout=5)
    finally:
        release.set()
        if not request_task.done():
            request_task.cancel()
        await asyncio.gather(request_task, return_exceptions=True)

    assert response.status_code == 201, response.text
    assert response.json()["created_count"] == 1
    assert [row["invited_user_id"] for row in response.json()["created"]] == [invited.id]
    assert held.id not in response.text
    assert tombstoned.id not in response.text
    invited_read = await client.get(f"/events/{event_id}", headers=invited.headers)
    held_read = await client.get(f"/events/{event_id}", headers=held.headers)
    assert invited_read.status_code == 200, invited_read.text
    assert held_read.status_code == 403
    async with get_session_factory()() as session:
        invites = list(
            (
                await session.scalars(
                    select(models.EventInvite).where(
                        models.EventInvite.event_id == uuid.UUID(event_id)
                    )
                )
            ).all()
        )
    assert len(invites) == 1 and invites[0].invited_user_id == uuid.UUID(invited.id)


@pytest.mark.asyncio
async def test_invite_permission_failure_precedes_target_hold_admission(
    client: Any,
    make_chapter_with: MakeChapterWith,
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("member")
    stranger = await make_user("event invite stranger")
    held = await make_user("held target for unauthorized invite")
    created = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        headers=setup.member.headers,
        json={**_event_body(), "visibility": "chapter"},
    )
    assert created.status_code == 201, created.text
    await _hold(held.id)

    response = await client.post(
        f"/events/{created.json()['id']}/invites",
        headers=stranger.headers,
        json={"user_ids": [held.id]},
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "not_a_member"}


@pytest.mark.asyncio
async def test_invite_batch_accepts_full_500_candidate_limit(
    client: Any,
    make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("member")
    created = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        headers=setup.member.headers,
        json={**_event_body(), "visibility": "chapter"},
    )
    assert created.status_code == 201, created.text
    values = [
        {
            "firebase_uid": f"event-invite-{uuid.uuid4().hex}",
            "email": f"event-invite-{uuid.uuid4().hex}@example.edu",
            "display_name": f"Event invite {index}",
            "account_type": "greek",
        }
        for index in range(500)
    ]
    async with get_session_factory()() as session:
        target_ids = list(
            (
                await session.scalars(
                    insert(models.User).values(values).returning(models.User.id)
                )
            ).all()
        )
        await session.commit()
    response = await client.post(
        f"/events/{created.json()['id']}/invites",
        headers=setup.member.headers,
        json={"user_ids": [str(user_id) for user_id in target_ids]},
    )
    assert response.status_code == 201, response.text
    assert response.json()["created_count"] == 500
    assert {uuid.UUID(row["invited_user_id"]) for row in response.json()["created"]} == set(
        target_ids
    )


@pytest.mark.asyncio
async def test_event_create_racing_hold_activation_commits_before_hold_then_is_blocked(
    client: Any,
    make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("member")
    request_id = await _deletion_request(setup.member.id)
    entered, release = asyncio.Event(), asyncio.Event()
    writer_pids: list[int] = []
    app = client._transport.app
    original = get_current_membership_for_write

    async def pause_after_admission(
        chapter_id: uuid.UUID = Path(...),
        user: models.User = Depends(get_current_user_for_write),
        session: AsyncSession = Depends(get_session),
    ) -> models.Membership:
        membership = await original(chapter_id=chapter_id, user=user, session=session)
        writer_pids.append(await session.scalar(text("SELECT pg_backend_pid()")))
        entered.set()
        await release.wait()
        return membership

    app.dependency_overrides[original] = pause_after_admission
    request_task = asyncio.create_task(client.post(
        f"/chapters/{setup.chapter_id}/events",
        headers=setup.member.headers,
        json=_event_body("Admission won race"),
    ))
    activation_task = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        async with get_session_factory()() as activator:
            activator_pid = await activator.scalar(text("SELECT pg_backend_pid()"))
            activation_task = asyncio.create_task(activate_deletion_hold(
                activator,
                user_id=uuid.UUID(setup.member.id),
                request_id=request_id,
            ))
            await _observe_blocked(
                activation_task, get_engine(), activator_pid, writer_pids[0]
            )
            release.set()
            response = await asyncio.wait_for(request_task, timeout=5)
            assert response.status_code == 201, response.text
            await asyncio.wait_for(activation_task, timeout=5)
            await activator.commit()

        later = await client.post(
            f"/chapters/{setup.chapter_id}/events",
            headers=setup.member.headers,
            json=_event_body("Must remain blocked"),
        )
        assert later.status_code == 423, later.text
    finally:
        release.set()
        app.dependency_overrides.pop(original, None)
        if not request_task.done():
            request_task.cancel()
        if activation_task is not None and not activation_task.done():
            activation_task.cancel()
        await asyncio.gather(
            request_task,
            *([activation_task] if activation_task is not None else []),
            return_exceptions=True,
        )


@pytest.mark.asyncio
async def test_flag_off_preserves_held_actor_event_writes(
    client: Any,
    make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, False)
    setup = await make_chapter_with("member")
    created = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        headers=setup.member.headers,
        json=_event_body(),
    )
    assert created.status_code == 201, created.text
    event_id = created.json()["id"]
    await _hold(setup.member.id)

    rsvp = await client.put(
        f"/events/{event_id}/rsvps", headers=setup.member.headers,
        json={"status": "going"},
    )
    edit = await client.patch(
        f"/events/{event_id}", headers=setup.member.headers,
        json={"title": "Legacy allowed"},
    )
    cancel = await client.post(f"/events/{event_id}/cancel", headers=setup.member.headers)
    create = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        headers=setup.member.headers,
        json=_event_body("Legacy second event"),
    )
    assert [response.status_code for response in (rsvp, edit, cancel, create)] == [
        200, 200, 200, 201,
    ]

