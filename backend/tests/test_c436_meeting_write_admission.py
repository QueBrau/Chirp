"""Account-deletion admission for organization meeting writers."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from app import models
from app.config import get_settings
from app.db import get_session_factory
from app.services.account_deletion_hold import activate_deletion_hold


def _set_admission(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setenv("ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED", str(enabled).lower())
    get_settings.cache_clear()


async def _hold(user_id: str) -> None:
    async with get_session_factory()() as session:
        request = models.AccountDataRequest(
            user_id=uuid.UUID(user_id), kind="deletion", open_key="meeting-test"
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


async def _meeting_count() -> int:
    async with get_session_factory()() as session:
        return int(await session.scalar(select(func.count()).select_from(models.Meeting)))


@pytest.mark.asyncio
async def test_held_actor_is_refused_before_each_meeting_mutation(
    client, make_chapter_with, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("secretary")
    created = await client.post(
        f"/chapters/{setup.chapter_id}/meetings",
        headers=setup.president.headers,
        json={"title": "Original", "meeting_date": datetime.now(timezone.utc).isoformat()},
    )
    assert created.status_code == 201, created.text
    meeting_id = created.json()["id"]
    attendance_url = f"/chapters/{setup.chapter_id}/meetings/{meeting_id}/attendance"
    seeded = await client.put(
        attendance_url,
        headers=setup.president.headers,
        json={"entries": [{"user_id": setup.president.id, "status": "present"}]},
    )
    assert seeded.status_code == 200, seeded.text
    before_count = await _meeting_count()
    await _hold(setup.member.id)

    create = await client.post(
        f"/chapters/{setup.chapter_id}/meetings",
        headers=setup.member.headers,
        json={"title": "Refused", "meeting_date": datetime.now(timezone.utc).isoformat()},
    )
    assert create.status_code == 423, create.text
    assert await _meeting_count() == before_count

    update = await client.patch(
        f"/chapters/{setup.chapter_id}/meetings/{meeting_id}",
        headers=setup.member.headers,
        json={"title": "Mutated"},
    )
    assert update.status_code == 423, update.text

    attendance = await client.put(
        attendance_url,
        headers=setup.member.headers,
        json={"entries": [{"user_id": setup.president.id, "status": "absent"}]},
    )
    assert attendance.status_code == 423, attendance.text

    deleted = await client.delete(
        f"/chapters/{setup.chapter_id}/meetings/{meeting_id}",
        headers=setup.member.headers,
    )
    assert deleted.status_code == 423, deleted.text
    async with get_session_factory()() as session:
        meeting = await session.get(models.Meeting, uuid.UUID(meeting_id))
        row = await session.get(
            models.MeetingAttendance,
            (uuid.UUID(meeting_id), uuid.UUID(setup.president.id)),
        )
        assert meeting is not None and meeting.title == "Original"
        assert row is not None and row.status == "present"

    readable = await client.get(
        f"/chapters/{setup.chapter_id}/meetings", headers=setup.member.headers
    )
    assert readable.status_code == 200, readable.text


@pytest.mark.asyncio
async def test_held_attendee_does_not_freeze_secretary_history_write(
    client, make_chapter_with, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("secretary")
    await _hold(setup.president.id)
    created = await client.post(
        f"/chapters/{setup.chapter_id}/meetings",
        headers=setup.member.headers,
        json={"title": "Secretary record", "meeting_date": datetime.now(timezone.utc).isoformat()},
    )
    assert created.status_code == 201, created.text
    url = f"/chapters/{setup.chapter_id}/meetings/{created.json()['id']}/attendance"
    written = await client.put(
        url,
        headers=setup.member.headers,
        json={"entries": [{"user_id": setup.president.id, "status": "present"}]},
    )
    assert written.status_code == 200, written.text


@pytest.mark.asyncio
async def test_flag_off_preserves_meeting_writer_behavior(
    client, make_chapter_with, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission(monkeypatch, False)
    setup = await make_chapter_with("secretary")
    await _hold(setup.member.id)
    response = await client.post(
        f"/chapters/{setup.chapter_id}/meetings",
        headers=setup.member.headers,
        json={"title": "Legacy", "meeting_date": datetime.now(timezone.utc).isoformat()},
    )
    assert response.status_code == 201, response.text


@pytest.mark.asyncio
async def test_wrong_role_remains_refused(
    client, make_chapter_with, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("member")
    response = await client.post(
        f"/chapters/{setup.chapter_id}/meetings",
        headers=setup.member.headers,
        json={"title": "Nope", "meeting_date": datetime.now(timezone.utc).isoformat()},
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "insufficient_role"
