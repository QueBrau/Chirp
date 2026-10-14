"""C436 write-admission behavior for privacy data requests."""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

import firebase_admin
from firebase_admin import auth as firebase_auth
import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.db import get_engine, get_session_factory
from app.services.account_deletion_hold import activate_deletion_hold


def _configure_admission(monkeypatch: pytest.MonkeyPatch, *, enabled: bool, auth_mode: str = "emulated") -> None:
    import app.middleware.auth as auth_module
    import app.routers.data_requests as routes

    from app.config import get_settings

    settings = get_settings().model_copy(update={
        "account_deletion_http_admission_enabled": enabled,
        "auth_mode": auth_mode,
        "firebase_project_id": "chirps-test",
        "privacy_reauth_max_age_seconds": 900,
    })
    monkeypatch.setattr(auth_module, "get_settings", lambda: settings)
    monkeypatch.setattr(routes, "get_settings", lambda: settings)


async def _activate_hold(user_id: str, request_id: str | None = None) -> None:
    uid = uuid.UUID(user_id)
    if request_id is None:
        async with get_session_factory()() as session:
            request = models.AccountDataRequest(user_id=uid, kind="deletion", open_key=None)
            session.add(request)
            await session.flush()
            hold_request_id = request.id
            await session.commit()
    else:
        hold_request_id = uuid.UUID(request_id)
    async with get_session_factory()() as session:
        await activate_deletion_hold(session, user_id=uid, request_id=hold_request_id)
        await session.commit()


async def _counts(user_id: str) -> tuple[int, int, int]:
    uid = uuid.UUID(user_id)
    async with get_session_factory()() as session:
        exports = await session.scalar(select(func.count()).select_from(models.AccountDataRequest).where(
            models.AccountDataRequest.user_id == uid, models.AccountDataRequest.kind == "export",
        ))
        artifacts = await session.scalar(select(func.count()).select_from(models.AccountDataArtifact).join(
            models.AccountDataRequest, models.AccountDataArtifact.request_id == models.AccountDataRequest.id,
        ).where(models.AccountDataRequest.user_id == uid))
        deletions = await session.scalar(select(func.count()).select_from(models.AccountDataRequest).where(
            models.AccountDataRequest.user_id == uid, models.AccountDataRequest.kind == "deletion",
        ))
    return exports, artifacts, deletions


@pytest.mark.asyncio
async def test_held_export_is_rejected_without_request_or_artifact(client, make_user, monkeypatch):
    _configure_admission(monkeypatch, enabled=True)
    owner = await make_user("Held export owner")
    await _activate_hold(owner.id)
    before = await _counts(owner.id)

    response = await client.post("/me/data-requests", json={"kind": "export"}, headers=owner.headers)

    assert response.status_code == 423, response.text
    assert response.json()["detail"] == "account_deletion_in_progress"
    after = await _counts(owner.id)
    assert after[:2] == before[:2] == (0, 0)


@pytest.mark.asyncio
async def test_deletion_intake_and_duplicate_remain_available_for_held_suspended_user(
    client, make_user, monkeypatch,
):
    _configure_admission(monkeypatch, enabled=True)
    owner = await make_user("Held suspended deletion owner")
    first = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    assert first.status_code == 201, first.text
    await _activate_hold(owner.id, first.json()["id"])
    async with get_session_factory()() as session:
        user = await session.get(models.User, uuid.UUID(owner.id))
        user.suspended_at = datetime.now(timezone.utc)
        await session.commit()

    second = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    assert second.status_code == 201, second.text
    assert second.json()["id"] == first.json()["id"]
    assert first.json()["status"] in {"blocked", "partially_completed"}
    assert set(first.json()["provider_steps"]) == {
        "payment_provider", "media_storage", "email_logs_backups", "analytics",
        "firebase_auth", "database_tombstone",
    }
    async with get_session_factory()() as session:
        steps = list((await session.scalars(select(models.AccountFulfillmentStep).where(
            models.AccountFulfillmentStep.request_id == uuid.UUID(first.json()["id"]),
        ))).all())
        open_deletions = await session.scalar(select(func.count()).select_from(models.AccountDataRequest).where(
            models.AccountDataRequest.user_id == uuid.UUID(owner.id),
            models.AccountDataRequest.kind == "deletion",
            models.AccountDataRequest.open_key == "open",
        ))
    assert len(steps) == 6
    assert open_deletions == 1


@pytest.mark.asyncio
async def test_suspended_user_retains_export_right_without_a_hold(client, make_user, monkeypatch):
    _configure_admission(monkeypatch, enabled=True)
    owner = await make_user("Suspended export owner")
    async with get_session_factory()() as session:
        user = await session.get(models.User, uuid.UUID(owner.id))
        user.suspended_at = datetime.now(timezone.utc)
        await session.commit()

    response = await client.post("/me/data-requests", json={"kind": "export"}, headers=owner.headers)
    assert response.status_code == 201, response.text
    assert (await _counts(owner.id))[:2] == (1, 1)


@pytest.mark.asyncio
async def test_existing_export_status_and_download_stay_readable_under_hold_and_owner_scoped(
    client, make_user, monkeypatch,
):
    _configure_admission(monkeypatch, enabled=True)
    owner = await make_user("Readable export owner")
    other = await make_user("Other export user")
    created = await client.post("/me/data-requests", json={"kind": "export"}, headers=owner.headers)
    assert created.status_code == 201, created.text
    request_id = created.json()["id"]
    assert created.json()["expires_at"]
    await _activate_hold(owner.id)

    status = await client.get(f"/me/data-requests/{request_id}", headers=owner.headers)
    download = await client.get(f"/me/data-requests/{request_id}/download", headers=owner.headers)
    assert status.status_code == 200, status.text
    assert download.status_code == 200, download.text
    assert download.json()["account"]["id"] == owner.id
    assert (await client.get(f"/me/data-requests/{request_id}", headers=other.headers)).status_code == 404
    assert (await client.get(f"/me/data-requests/{request_id}/download", headers=other.headers)).status_code == 404


@pytest.mark.asyncio
async def test_flag_off_export_does_not_query_hold_table(client, make_user, monkeypatch):
    _configure_admission(monkeypatch, enabled=False)
    owner = await make_user("Flag-off export owner")
    await _activate_hold(owner.id)
    statements: list[str] = []
    sync_engine = get_engine().sync_engine

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    event.listen(sync_engine, "before_cursor_execute", capture)
    try:
        response = await client.post("/me/data-requests", json={"kind": "export"}, headers=owner.headers)
    finally:
        event.remove(sync_engine, "before_cursor_execute", capture)
    assert response.status_code == 201, response.text
    assert not any("account_deletion_holds" in statement for statement in statements)
    assert (await _counts(owner.id))[:2] == (1, 1)


@pytest.mark.asyncio
async def test_export_commit_response_survives_hold_activated_immediately_after_commit(
    client, make_user, monkeypatch,
):
    _configure_admission(monkeypatch, enabled=True)
    owner = await make_user("Commit boundary export owner")
    original_commit = AsyncSession.commit
    activated = False

    async def commit_then_activate(session: AsyncSession) -> None:
        nonlocal activated
        await original_commit(session)
        if not activated and any(isinstance(obj, models.AccountDataRequest) and obj.kind == "export" for obj in session.identity_map.values()):
            activated = True
            await _activate_hold(owner.id)

    monkeypatch.setattr(AsyncSession, "commit", commit_then_activate)
    response = await client.post("/me/data-requests", json={"kind": "export"}, headers=owner.headers)
    assert activated
    assert response.status_code == 201, response.text
    assert response.json()["expires_at"]
    assert (await _counts(owner.id))[:2] == (1, 1)
    expiry = datetime.fromisoformat(response.json()["expires_at"].replace("Z", "+00:00"))
    remaining = (expiry - datetime.now(timezone.utc)).total_seconds()
    assert 6 * 24 * 60 * 60 < remaining < 8 * 24 * 60 * 60


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["revoked", "stale"])
async def test_export_keeps_fresh_firebase_auth_checks(client, make_user, monkeypatch, failure):
    owner = await make_user("Fresh-auth export owner")
    _configure_admission(monkeypatch, enabled=True, auth_mode="firebase")
    monkeypatch.setattr(firebase_admin, "get_app", lambda: object())
    calls: list[bool] = []

    def verify(_token: str, *, check_revoked: bool = False):
        calls.append(check_revoked)
        if failure == "revoked":
            raise firebase_auth.RevokedIdTokenError("revoked")
        return {"uid": owner.firebase_uid, "auth_time": time.time() - 3600}

    monkeypatch.setattr(firebase_auth, "verify_id_token", verify)
    response = await client.post(
        "/me/data-requests", json={"kind": "export"},
        headers={"Authorization": "Bearer privacy-token"},
    )
    assert response.status_code == 401, response.text
    assert response.json()["detail"] == ("invalid_token" if failure == "revoked" else "recent_authentication_required")
    assert calls == [True]
    assert (await _counts(owner.id))[:2] == (0, 0)
