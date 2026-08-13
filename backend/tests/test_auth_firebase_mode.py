"""Unit tests for the firebase branch of get_verified_uid, with firebase_admin mocked."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import firebase_admin
import pytest
from fastapi import HTTPException
from firebase_admin import auth as firebase_auth

import app.middleware.auth as auth_module
from app.middleware.auth import get_verified_uid


def _use_firebase_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point get_verified_uid's settings lookup at auth_mode="firebase" for this test only."""
    monkeypatch.setattr(auth_module, "get_settings", lambda: SimpleNamespace(auth_mode="firebase"))


def _stub_already_initialized_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make firebase_admin.get_app() succeed so get_verified_uid skips initialize_app()
    (which would otherwise try to reach real GCP credentials)."""
    monkeypatch.setattr(firebase_admin, "get_app", lambda: object())


async def test_firebase_mode_valid_token_returns_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    """A well-formed Bearer token that firebase_admin verifies returns the decoded uid."""
    _use_firebase_mode(monkeypatch)
    _stub_already_initialized_app(monkeypatch)
    monkeypatch.setattr(
        firebase_auth, "verify_id_token", lambda token: {"uid": "firebase-uid-123"}
    )

    uid = await get_verified_uid(x_debug_firebase_uid=None, authorization="Bearer good-token")

    assert uid == "firebase-uid-123"


async def test_firebase_mode_invalid_token_raises_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """A token firebase_admin rejects (expired/tampered/etc.) raises 401 invalid_token."""
    _use_firebase_mode(monkeypatch)
    _stub_already_initialized_app(monkeypatch)

    def _raise_invalid(token: str) -> dict[str, Any]:
        raise ValueError("Firebase ID token has expired.")

    monkeypatch.setattr(firebase_auth, "verify_id_token", _raise_invalid)

    with pytest.raises(HTTPException) as exc_info:
        await get_verified_uid(x_debug_firebase_uid=None, authorization="Bearer bad-token")

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "invalid_token"


async def test_firebase_mode_decoded_token_without_uid_raises_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token that verifies but decodes without a uid claim is still 401 invalid_token."""
    _use_firebase_mode(monkeypatch)
    _stub_already_initialized_app(monkeypatch)
    monkeypatch.setattr(firebase_auth, "verify_id_token", lambda token: {"email": "no-uid@example.edu"})

    with pytest.raises(HTTPException) as exc_info:
        await get_verified_uid(x_debug_firebase_uid=None, authorization="Bearer good-token")

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "invalid_token"


async def test_firebase_mode_missing_authorization_header_raises_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No Authorization header (and no debug uid, irrelevant in this mode) raises 401."""
    _use_firebase_mode(monkeypatch)

    with pytest.raises(HTTPException) as exc_info:
        await get_verified_uid(x_debug_firebase_uid=None, authorization=None)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "missing_bearer_token"


async def test_firebase_mode_non_bearer_authorization_header_raises_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An Authorization header present but not "Bearer <token>" is treated as missing."""
    _use_firebase_mode(monkeypatch)

    with pytest.raises(HTTPException) as exc_info:
        await get_verified_uid(x_debug_firebase_uid=None, authorization="Basic dXNlcjpwYXNz")

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "missing_bearer_token"
