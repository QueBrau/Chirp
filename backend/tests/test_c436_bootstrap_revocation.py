"""Bootstrap rejects Firebase identities that are no longer current."""
from __future__ import annotations

from types import SimpleNamespace
import uuid

import firebase_admin
from firebase_admin import auth as firebase_auth
import pytest
from sqlalchemy import select

from app import models
from app.db import get_session_factory
import app.middleware.auth as auth_module
import app.routers.auth as auth_router_module


def _firebase_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        auth_module,
        "get_settings",
        lambda: SimpleNamespace(
            auth_mode="firebase",
            firebase_project_id="chirps-test",
            privacy_reauth_max_age_seconds=900,
        ),
    )
    monkeypatch.setattr(auth_router_module, "get_settings", auth_module.get_settings)
    monkeypatch.setattr(firebase_admin, "get_app", lambda: object())


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["deleted", "revoked", "disabled", "provider"])
async def test_bootstrap_rejects_noncurrent_old_identity(
    client, make_user, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    original = await make_user("Deleted Firebase identity")
    async with get_session_factory()() as session:
        user = await session.scalar(
            select(models.User).where(models.User.id == uuid.UUID(original.id))
        )
        assert user is not None
        user.firebase_uid = f"deleted:{user.id}"
        user.email = f"deleted+{user.id}@invalid.chirp"
        user.is_ghost = True
        await session.commit()

    _firebase_settings(monkeypatch)
    calls: list[bool] = []

    def verify(token: str, *, check_revoked: bool = False) -> dict[str, str]:
        calls.append(check_revoked)
        if failure != "provider" and not check_revoked:
            return {"uid": original.firebase_uid, "email": "old@example.edu"}
        if failure == "provider":
            raise RuntimeError("firebase unavailable")
        exception = {
            "deleted": firebase_auth.UserNotFoundError("user deleted"),
            "revoked": firebase_auth.RevokedIdTokenError("token revoked"),
            "disabled": firebase_auth.UserDisabledError("user disabled"),
        }[failure]
        raise exception

    monkeypatch.setattr(firebase_auth, "verify_id_token", verify)
    response = await client.post(
        "/auth/bootstrap",
        headers={"Authorization": "Bearer old-token"},
        json={
            "email": "old@example.edu",
            "display_name": "Recreated",
            "account_type": "greek",
        },
    )
    assert response.status_code == 401
    assert calls == [True]
    async with get_session_factory()() as session:
        assert (
            await session.scalar(
                select(models.User).where(
                    models.User.firebase_uid == original.firebase_uid
                )
            )
            is None
        )
        assert (
            await session.scalar(
                select(models.User).where(models.User.email == "old@example.edu")
            )
            is None
        )
        tombstone = await session.scalar(
            select(models.User).where(models.User.id == uuid.UUID(original.id))
        )
        assert tombstone is not None
        assert tombstone.firebase_uid == f"deleted:{original.id}"
        assert tombstone.email == f"deleted+{original.id}@invalid.chirp"
        assert tombstone.is_ghost is True


@pytest.mark.asyncio
async def test_bootstrap_allows_current_new_identity_and_uses_verified_email(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    _firebase_settings(monkeypatch)

    def verify(token: str, *, check_revoked: bool = False) -> dict[str, str]:
        assert check_revoked is True
        return {"uid": "new-firebase-uid", "email": "verified@example.edu"}

    monkeypatch.setattr(firebase_auth, "verify_id_token", verify)
    response = await client.post(
        "/auth/bootstrap",
        headers={"Authorization": "Bearer new-token"},
        json={
            "email": "verified@example.edu",
            "display_name": "New account",
            "account_type": "greek",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["email"] == "verified@example.edu"


@pytest.mark.asyncio
async def test_bootstrap_rejects_verified_token_email_mismatch_without_insert(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    _firebase_settings(monkeypatch)
    monkeypatch.setattr(firebase_auth, "verify_id_token", lambda token, *, check_revoked=False: {"uid": "mismatch-uid", "email": "verified@example.edu"})
    response = await client.post(
        "/auth/bootstrap",
        headers={"Authorization": "Bearer mismatch-token"},
        json={"email": "attacker@example.edu", "display_name": "Mismatch", "account_type": "greek"},
    )
    assert response.status_code == 400
    async with get_session_factory()() as session:
        assert await session.scalar(select(models.User).where(models.User.firebase_uid == "mismatch-uid")) is None


@pytest.mark.asyncio
async def test_bootstrap_emulated_identity_contract_is_unchanged(client) -> None:
    response = await client.post(
        "/auth/bootstrap",
        headers={"X-Debug-Firebase-Uid": "emulated-bootstrap-uid"},
        json={
            "email": "emulated@example.edu",
            "display_name": "Emulated",
            "account_type": "greek",
        },
    )
    assert response.status_code == 201, response.text
