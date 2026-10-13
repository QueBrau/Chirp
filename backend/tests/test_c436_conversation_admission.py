"""Deletion-hold admission for conversation creation and DM reuse."""
from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from sqlalchemy import func, select

from app import models
from app.config import get_settings
from app.db import get_session_factory
from app.services.account_deletion_hold import activate_deletion_hold
from app.routers import messages
from tests.conftest import share_verified_campus


def _set_admission(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setenv("ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED", str(enabled).lower())
    get_settings.cache_clear()


async def _hold(user_id: str) -> None:
    async with get_session_factory()() as session:
        request = models.AccountDataRequest(
            user_id=uuid.UUID(user_id), kind="deletion", open_key="conversation-test"
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
async def test_held_requested_participant_blocks_new_and_reused_dm_without_mutation(
    client, make_user, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission(monkeypatch, True)
    caller = await make_user("conversation caller")
    target = await make_user("conversation target")
    await share_verified_campus(caller.id, target.id)
    created = await client.post(
        "/conversations",
        headers=caller.headers,
        json={"kind": "dm", "member_user_ids": [target.id]},
    )
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]
    left = await client.post(f"/conversations/{conversation_id}/leave", headers=target.headers)
    assert left.status_code == 200 and left.json()["left_at"] is not None, left.text
    await _hold(target.id)

    before = await _conversation_count()
    response = await client.post(
        "/conversations",
        headers=caller.headers,
        json={"kind": "dm", "member_user_ids": [target.id]},
    )
    assert response.status_code == 423, response.text
    fresh = await client.post(
        "/conversations", headers=caller.headers,
        json={"kind": "group", "member_user_ids": [target.id]},
    )
    assert fresh.status_code == 423, fresh.text
    assert await _conversation_count() == before
    async with get_session_factory()() as session:
        member = await session.get(
            models.ConversationMember,
            (uuid.UUID(conversation_id), uuid.UUID(target.id)),
        )
        assert member is not None and member.left_at is not None


async def _conversation_count() -> int:
    async with get_session_factory()() as session:
        return int(await session.scalar(select(func.count()).select_from(models.Conversation)))


@pytest.mark.asyncio
async def test_flag_off_preserves_dm_creation_with_held_target(
    client, make_user, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission(monkeypatch, False)
    caller = await make_user("legacy conversation caller")
    target = await make_user("legacy conversation target")
    await share_verified_campus(caller.id, target.id)
    await _hold(target.id)
    response = await client.post(
        "/conversations",
        headers=caller.headers,
        json={"kind": "dm", "member_user_ids": [target.id]},
    )
    assert response.status_code == 201, response.text


@pytest.mark.asyncio
async def test_ineligible_held_targets_keep_original_creation_refusals(
    client, make_user, monkeypatch,
):
    _set_admission(monkeypatch, True)
    caller, target = await make_user("Caller"), await make_user("Unreachable target")
    blocked_target = await make_user("Blocked target")
    await share_verified_campus(caller.id, blocked_target.id)
    blocked = await client.post(
        "/moderation/blocks", headers=blocked_target.headers,
        json={"blocked_id": caller.id},
    )
    assert blocked.status_code == 201, blocked.text
    # Unrelated accounts have no reachable-campus or chapter relationship.
    await _hold(target.id)
    await _hold(blocked_target.id)
    refused = await client.post(
        "/conversations", headers=caller.headers,
        json={"kind": "dm", "member_user_ids": [target.id]},
    )
    assert refused.status_code == 403, refused.text
    assert refused.json() == {"detail": "recipient_not_reachable"}
    refused = await client.post(
        "/conversations", headers=caller.headers,
        json={"kind": "dm", "member_user_ids": [blocked_target.id]},
    )
    assert refused.status_code == 403, refused.text
    assert refused.json() == {"detail": "recipient_not_reachable"}
    missing = await client.post(
        "/conversations", headers=caller.headers,
        json={"kind": "dm", "member_user_ids": [str(uuid.uuid4())]},
    )
    assert missing.status_code == 404, missing.text
    assert missing.json() == {"detail": "user_not_found"}
    assert await _conversation_count() == 0


@pytest.mark.asyncio
async def test_creation_rechecks_policy_after_preflight_state_change(
    client, make_user, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission(monkeypatch, True)
    caller = await make_user("preflight drift caller")
    target = await make_user("preflight drift target")
    await share_verified_campus(caller.id, target.id)
    original = messages._validate_conversation_request
    calls = 0

    async def drift_after_preflight(session, actor, body, member_ids):
        nonlocal calls
        await original(session, actor, body, member_ids)
        calls += 1
        if calls == 1:
            async with get_session_factory()() as external:
                external.add(
                    models.UserBlock(
                        blocker_id=uuid.UUID(target.id),
                        blocked_id=uuid.UUID(caller.id),
                        source="named",
                    )
                )
                await external.commit()

    monkeypatch.setattr(messages, "_validate_conversation_request", drift_after_preflight)
    before = await _conversation_count()
    response = await client.post(
        "/conversations",
        headers=caller.headers,
        json={"kind": "dm", "member_user_ids": [target.id]},
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "recipient_not_reachable"
    assert await _conversation_count() == before


@pytest.mark.asyncio
async def test_integrity_retry_rechecks_policy_and_preserves_left_at(
    client, make_user, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission(monkeypatch, True)
    caller = await make_user("rollback drift caller")
    target = await make_user("rollback drift target")
    await share_verified_campus(caller.id, target.id)
    created = await client.post(
        "/conversations",
        headers=caller.headers,
        json={"kind": "dm", "member_user_ids": [target.id]},
    )
    assert created.status_code == 201, created.text
    conversation_id = uuid.UUID(created.json()["id"])
    left = await client.post(
        f"/conversations/{conversation_id}/leave", headers=target.headers
    )
    assert left.status_code == 200, left.text
    assert left.json()["left_at"] is not None

    original = messages._reuse_dm
    calls = 0

    async def force_integrity_then_drift(session, dm_key, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            async with get_session_factory()() as external:
                external.add(
                    models.UserBlock(
                        blocker_id=uuid.UUID(target.id),
                        blocked_id=uuid.UUID(caller.id),
                        source="named",
                    )
                )
                await external.commit()
            return None
        return await original(session, dm_key, **kwargs)

    monkeypatch.setattr(messages, "_reuse_dm", force_integrity_then_drift)
    response = await client.post(
        "/conversations",
        headers=caller.headers,
        json={"kind": "dm", "member_user_ids": [target.id]},
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "recipient_not_reachable"
    assert calls == 1
    async with get_session_factory()() as session:
        member = await session.get(models.ConversationMember, (conversation_id, uuid.UUID(target.id)))
        assert member is not None and member.left_at == datetime.fromisoformat(left.json()["left_at"])


@pytest.mark.asyncio
async def test_enabled_creation_accepts_257_members_and_rejects_258(
    client, make_user, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission(monkeypatch, True)
    # Bootstrap is intentionally rate limited; insert this boundary-sized fixture
    # directly so the test exercises the conversation bound rather than auth throttling.
    users: list[tuple[str, models.User]] = []
    async with get_session_factory()() as session:
        for index in range(257):
            uid = f"c436-group-{uuid.uuid4().hex}"
            user = models.User(
                firebase_uid=uid,
                email=f"{uid}@example.edu",
                display_name=f"group member {index}",
                account_type="greek",
            )
            session.add(user)
            users.append((uid, user))
        await session.flush()
        user_ids = [str(user.id) for _, user in users]
        await session.commit()
    await share_verified_campus(*user_ids)
    caller_headers = {"X-Debug-Firebase-Uid": users[0][0]}
    member_ids = user_ids[1:]
    accepted = await client.post(
        "/conversations", headers=caller_headers,
        json={"kind": "group", "member_user_ids": member_ids},
    )
    assert accepted.status_code == 201, accepted.text
    assert len(accepted.json()["members"]) == 257

    rejected = await client.post(
        "/conversations",
        headers=caller_headers,
        json={
            "kind": "group",
            "member_user_ids": [*member_ids, str(uuid.uuid4())],
        },
    )
    assert rejected.status_code == 422, rejected.text
