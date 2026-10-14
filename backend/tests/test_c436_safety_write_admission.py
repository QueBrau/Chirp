"""Deletion-hold admission for reporter and blocker-owned safety mutations."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app import models
from app.config import get_settings
from app.db import get_session_factory
from app.services.account_cleanup import cleanup_local_account
from app.services.account_deletion_hold import activate_deletion_hold
from tests.conftest import share_verified_campus


def _set_admission(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setenv("ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED", str(enabled).lower())
    get_settings.cache_clear()


async def _hold(user_id: str) -> None:
    async with get_session_factory()() as session:
        request = models.AccountDataRequest(
            user_id=uuid.UUID(user_id), kind="deletion", status="processing",
            open_key=f"safety-review-{uuid.uuid4()}",
        )
        session.add(request)
        await session.flush()
        request_id = request.id
        await session.commit()
    async with get_session_factory()() as session:
        await activate_deletion_hold(session, user_id=uuid.UUID(user_id), request_id=request_id)
        await session.commit()


async def _block_row_count() -> int:
    async with get_session_factory()() as session:
        return int(await session.scalar(select(func.count()).select_from(models.UserBlock)))


async def _report_count() -> int:
    async with get_session_factory()() as session:
        return int(await session.scalar(select(func.count()).select_from(models.ContentReport)))


@pytest.fixture(autouse=True)
def _clear_settings():
    yield
    get_settings.cache_clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["report", "named_create", "anonymous_create", "named_delete", "anonymous_delete"])
async def test_held_actor_denied_all_safety_mutations_without_rows(
    client, make_user, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    _set_admission(monkeypatch, True)
    actor, target = await make_user("safety actor"), await make_user("safety target")
    campus_id = await share_verified_campus(actor.id, target.id)
    chirp = await client.post(f"/campuses/{campus_id}/chirps", headers=target.headers, json={"body": "review chirp"})
    assert chirp.status_code == 201, chirp.text
    chirp_id = chirp.json()["id"]
    if operation == "named_delete":
        initial = await client.post("/moderation/blocks", headers=actor.headers, json={"blocked_id": target.id})
        assert initial.status_code == 201, initial.text
    elif operation == "anonymous_delete":
        initial = await client.post(f"/moderation/blocks/by-chirp/{chirp_id}", headers=actor.headers)
        assert initial.status_code == 204, initial.text
    before_blocks, before_reports = await _block_row_count(), await _report_count()
    await _hold(actor.id)
    if operation == "report":
        response = await client.post("/moderation/reports", headers=actor.headers, json={"target_type": "chirp", "target_id": chirp_id, "reason": "review"})
    elif operation == "named_create":
        response = await client.post("/moderation/blocks", headers=actor.headers, json={"blocked_id": target.id})
    elif operation == "anonymous_create":
        response = await client.post(f"/moderation/blocks/by-chirp/{chirp_id}", headers=actor.headers)
    elif operation == "named_delete":
        response = await client.delete("/moderation/blocks", headers=actor.headers, params={"blocked_id": target.id})
    else:
        response = await client.delete(f"/moderation/blocks/by-chirp/{chirp_id}", headers=actor.headers)
    assert response.status_code == 423, response.text
    assert await _block_row_count() == before_blocks
    assert await _report_count() == before_reports


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["create", "delete"])
async def test_held_anonymous_target_does_not_affect_blocker_or_reveal_author(
    client, make_user, monkeypatch: pytest.MonkeyPatch, endpoint: str,
) -> None:
    _set_admission(monkeypatch, True)
    blocker, author = await make_user("anonymous blocker"), await make_user("anonymous author")
    campus_id = await share_verified_campus(blocker.id, author.id)
    chirp = await client.post(f"/campuses/{campus_id}/chirps", headers=author.headers, json={"body": "anonymous safety target"})
    assert chirp.status_code == 201, chirp.text
    chirp_id = chirp.json()["id"]
    await _hold(author.id)
    if endpoint == "create":
        response = await client.post(f"/moderation/blocks/by-chirp/{chirp_id}", headers=blocker.headers)
        assert response.status_code == 204, response.text
    else:
        created = await client.post(f"/moderation/blocks/by-chirp/{chirp_id}", headers=blocker.headers)
        assert created.status_code == 204, created.text
        response = await client.delete(f"/moderation/blocks/by-chirp/{chirp_id}", headers=blocker.headers)
        assert response.status_code == 204, response.text
    assert response.text == ""
    async with get_session_factory()() as session:
        row = (await session.execute(select(models.UserBlock).where(models.UserBlock.blocker_id == uuid.UUID(blocker.id)))).scalar_one_or_none()
        assert (row is not None) == (endpoint == "create")


@pytest.mark.asyncio
async def test_incoming_block_survives_target_cleanup_and_flag_off_preserves_actor_write(
    client, make_user, monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocker, target = await make_user("retained blocker"), await make_user("retained target")
    await share_verified_campus(blocker.id, target.id)
    _set_admission(monkeypatch, False)
    created = await client.post("/moderation/blocks", headers=blocker.headers, json={"blocked_id": target.id})
    assert created.status_code == 201, created.text
    await _hold(target.id)
    async with get_session_factory()() as session:
        await cleanup_local_account(session, uuid.UUID(target.id))
        await session.commit()
    async with get_session_factory()() as session:
        row = (await session.execute(select(models.UserBlock).where(models.UserBlock.blocker_id == uuid.UUID(blocker.id), models.UserBlock.blocked_id == uuid.UUID(target.id)))).scalar_one_or_none()
        assert row is not None and row.source == "named"
