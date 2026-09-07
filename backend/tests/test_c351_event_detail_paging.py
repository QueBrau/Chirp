"""Event detail must remain correct beyond the first guest page (c351)."""
from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy import insert

from app import models
from app.db import get_session_factory
from tests.conftest import MakeChapterWith, MakeUser
from tests.test_events import _event_body


async def test_500_invitees_own_answer_counts_and_exact_guest_continuation(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member")
    created = await client.post(
        f"/chapters/{setup.chapter_id}/events", json=_event_body(), headers=setup.president.headers
    )
    assert created.status_code == 201, created.text
    event_id = created.json()["id"]
    others = [uuid.uuid4() for _ in range(499)]
    viewer = uuid.UUID(setup.member.id)
    invited = others + [viewer]
    answered = others[:249] + [viewer]
    expected_status = {str(user_id): ("going", "maybe", "cant")[i % 3]
                       for i, user_id in enumerate(answered)}
    old = datetime.now(timezone.utc) - timedelta(days=1)
    async with get_session_factory()() as session:
        await session.execute(insert(models.User), [
            {"id": user_id, "firebase_uid": str(user_id), "email": f"{user_id}@example.edu",
             "display_name": "Guest", "account_type": "non_greek"}
            for user_id in others
        ])
        await session.execute(insert(models.EventInvite), [
            {"event_id": uuid.UUID(event_id), "invited_user_id": user_id,
             "invited_by": uuid.UUID(setup.president.id), "created_at": old}
            for user_id in invited
        ])
        await session.execute(insert(models.EventRsvp), [
            {"event_id": uuid.UUID(event_id), "user_id": user_id,
             "status": expected_status[str(user_id)],
             "created_at": old + timedelta(seconds=1) if user_id == viewer else old}
            for user_id in answered
        ])
        await session.commit()

    first = await client.get(f"/events/{event_id}/rsvps", params={"limit": 200}, headers=setup.member.headers)
    assert len(first.json()) == 200
    assert setup.member.id not in {row["user_id"] for row in first.json()}
    for _ in range(2):  # a refresh must recover the own answer and restart stable cursors
        mine = await client.get(f"/events/{event_id}/rsvps/mine", headers=setup.member.headers)
        assert mine.status_code == 200, mine.text
        assert mine.json() == {"status": expected_status[setup.member.id]}
        counts = await client.get(f"/events/{event_id}/rsvp-counts", headers=setup.member.headers)
        assert counts.json() == {**Counter(expected_status.values()), "invited_unanswered": 250}
        for resource, id_field, params, expected in (
            ("rsvps", "user_id", {}, set(map(str, answered))),
            ("invites", "invited_user_id", {}, set(map(str, invited))),
            ("invites", "invited_user_id", {"unanswered_only": True}, set(map(str, others[249:]))),
        ):
            seen: list[str] = []
            cursor: dict[str, str | int | bool] = {"limit": 200, **params}
            while True:
                response = await client.get(
                    f"/events/{event_id}/{resource}", params=cursor, headers=setup.member.headers
                )
                assert response.status_code == 200, response.text
                rows = response.json()
                assert len(rows) <= 200
                seen.extend(row[id_field] for row in rows)
                if len(rows) < 200:
                    break
                last = rows[-1]
                cursor.update(after=last["created_at"], after_user_id=last[id_field])
            assert len(seen) == len(set(seen)) == len(expected)
            assert set(seen) == expected


async def test_own_answer_uses_event_read_gate_without_disclosing_other_guests(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    setup = await make_chapter_with("member")
    outsider = await make_user("Public viewer")
    private = await client.post(
        f"/chapters/{setup.chapter_id}/events", json=_event_body(), headers=setup.president.headers
    )
    public = await client.post(
        f"/chapters/{setup.chapter_id}/events", json=_event_body(visibility="public"),
        headers=setup.president.headers,
    )
    private_id, public_id = private.json()["id"], public.json()["id"]
    await client.put(f"/events/{public_id}/rsvps", json={"status": "going"}, headers=setup.member.headers)
    mine = await client.get(f"/events/{public_id}/rsvps/mine", headers=outsider.headers)
    assert mine.json() == {"status": None}
    assert (await client.get(f"/events/{public_id}/rsvps", headers=outsider.headers)).status_code == 403
    assert (await client.get(f"/events/{private_id}/rsvps/mine", headers=outsider.headers)).status_code == 403
    assert (await client.get(f"/events/{private_id}/rsvps/mine")).status_code == 401
    assert (await client.get(f"/events/{uuid.uuid4()}/rsvps/mine", headers=outsider.headers)).status_code == 404
    await client.post(f"/events/{private_id}/invites", json={"user_ids": [outsider.id]}, headers=setup.president.headers)
    invited = await client.get(f"/events/{private_id}/rsvps/mine", headers=outsider.headers)
    assert invited.status_code == 200 and invited.json() == {"status": None}


async def test_unanswered_filter_is_event_scoped_and_keeps_guest_gate(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    setup = await make_chapter_with("member")
    outsider = await make_user("Walk-in")
    ids = []
    for _ in range(2):
        created = await client.post(
            f"/chapters/{setup.chapter_id}/events", json=_event_body(visibility="public"),
            headers=setup.president.headers,
        )
        ids.append(created.json()["id"])
        await client.post(f"/events/{ids[-1]}/invites", json={"user_ids": [setup.member.id]}, headers=setup.president.headers)
    await client.put(f"/events/{ids[0]}/rsvps", json={"status": "cant"}, headers=setup.member.headers)
    answered = await client.get(f"/events/{ids[0]}/invites", params={"unanswered_only": True}, headers=setup.member.headers)
    other = await client.get(f"/events/{ids[1]}/invites", params={"unanswered_only": True}, headers=setup.member.headers)
    assert answered.json() == []
    assert [row["invited_user_id"] for row in other.json()] == [setup.member.id]
    refused = await client.get(f"/events/{ids[1]}/invites", params={"unanswered_only": True}, headers=outsider.headers)
    assert refused.status_code == 403
