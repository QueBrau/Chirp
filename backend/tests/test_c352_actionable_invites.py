"""Home invitation filtering precedes pagination and preserves explicit grants (c352)."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy import insert

from app import models
from app.db import get_session_factory
from tests.conftest import MakeChapterWith, MakeUser


async def test_actionable_pages_survive_answered_history_and_reach_every_pending_invite(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    setup = await make_chapter_with("member")
    viewer = await make_user("Unverified cross-chapter invitee")
    now = datetime.now(timezone.utc)
    past = [uuid.uuid4() for _ in range(60)]
    pending = [uuid.uuid4() for _ in range(61)]
    answered_future, canceled_future, old_silent, ongoing = (uuid.uuid4() for _ in range(4))
    ids = past + pending + [answered_future, canceled_future, old_silent, ongoing]
    async with get_session_factory()() as session:
        await session.execute(insert(models.Event), [
            {"id": event_id, "chapter_id": uuid.UUID(setup.chapter_id), "title": str(event_id),
             "host_id": uuid.UUID(setup.president.id), "cover_url": "https://example.com/cover.jpg",
             "location": "Hall", "visibility": "chapter",
             "starts_at": now - timedelta(days=2) if event_id in past + [old_silent]
                 else now - timedelta(hours=1) if event_id == ongoing else now + timedelta(days=2),
             "ends_at": now + timedelta(hours=1) if event_id == ongoing else None,
             "canceled_at": now if event_id == canceled_future else None}
            for event_id in ids
        ])
        await session.execute(insert(models.EventInvite), [
            {"event_id": event_id, "invited_user_id": uuid.UUID(viewer.id),
             "invited_by": uuid.UUID(setup.president.id)} for event_id in ids
        ])
        await session.execute(insert(models.EventRsvp), [
            {"event_id": event_id, "user_id": uuid.UUID(viewer.id), "status": "going"}
            for event_id in past + [answered_future, canceled_future]
        ])
        # Someone else's answer cannot remove an invitation from the caller's action set.
        await session.execute(insert(models.EventRsvp), {
            "event_id": pending[0], "user_id": uuid.UUID(setup.member.id), "status": "cant"
        })
        await session.commit()

    legacy = await client.get("/me/event-invites-with-rsvps", headers=viewer.headers)
    assert len(legacy.json()) == 50
    assert not {str(event_id) for event_id in pending} & {row["event"]["id"] for row in legacy.json()}
    expected = {str(event_id) for event_id in pending + [canceled_future, ongoing]}
    seen = []
    cursor: dict[str, str | int] = {"view": "actionable", "limit": 50}
    while True:
        page = await client.get("/me/event-invites-with-rsvps", params=cursor, headers=viewer.headers)
        assert page.status_code == 200, page.text
        rows = page.json()
        seen.extend(row["event"]["id"] for row in rows)
        assert all(row["my_rsvp_status"] is None or row["event"]["canceled_at"] is not None for row in rows)
        if len(rows) < 50:
            break
        last = rows[-1]["event"]
        cursor.update(before=last["starts_at"], before_id=last["id"])
    assert len(seen) == len(set(seen)) == len(expected)
    assert set(seen) == expected
    history = await client.get("/me/event-invites-with-rsvps", params={"view": "history", "limit": 200}, headers=viewer.headers)
    assert {row["event"]["id"] for row in history.json()} == set(map(str, past + [old_silent]))
    assert (await client.get("/me/event-invites-with-rsvps", params={"view": "actionable"})).status_code == 401
    assert (await client.get("/me/event-invites-with-rsvps", params={"view": "unknown"}, headers=viewer.headers)).status_code == 422
