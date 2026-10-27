"""Deletion completion capabilities, owner scope and committed-tombstone checks."""

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import logging
import uuid

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import select

from app import models
from app.config import get_settings
from app.core.log_scrub import _RedactCredentialQueryParamsFilter
from app.db import get_session_factory
from app.services import account_deletion_completion as completion
from app.services.storage_service import mint_media_token, verify_media_token

SECRET = "synthetic-completion-signing-secret-32-bytes"
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setattr(get_settings(), "media_signing_secret", SECRET)


@pytest.fixture
def api_secret(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "media_signing_secret", SECRET)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _signed_payload(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    key = hmac.new(SECRET.encode(), b"chirp:account-deletion-completion:v1", hashlib.sha256).digest()
    return _b64(payload) + "." + _b64(hmac.new(key, payload, hashlib.sha256).digest())


def _claims() -> dict:
    return {
        "version": 1, "purpose": "account-deletion-completion",
        "request_id": str(uuid.uuid4()), "user_id": str(uuid.uuid4()),
        "iat": int(NOW.timestamp()), "exp": int(NOW.timestamp()) + completion.TOKEN_TTL_SECONDS,
    }


def test_capability_lifetime_is_exactly_ninety_days_and_rejects_expiry(secret) -> None:
    request_id, user_id = uuid.uuid4(), uuid.uuid4()
    token, claims = completion.mint_completion_token(request_id, user_id, now=NOW)
    assert claims.expires_at == NOW + timedelta(days=90)
    assert completion.verify_completion_token(token, now=NOW) == claims
    assert completion.verify_completion_token(token, now=claims.expires_at - timedelta(seconds=1)) == claims
    with pytest.raises(completion.CompletionTokenError, match="token_expired") as exc:
        completion.verify_completion_token(token, now=claims.expires_at)
    assert exc.value.status_code == 410


@pytest.mark.parametrize("field,value", [
    ("version", True), ("version", 2), ("purpose", "media"),
    ("iat", True), ("iat", 1.0), ("iat", "1793102400"), ("iat", -1),
    ("exp", True), ("exp", 1.0), ("exp", "1800878400"),
    ("request_id", "bad"), ("request_id", 123), ("user_id", None),
    ("user_id", "00000000-0000-0000-0000-00000000000A"),
])
def test_signed_but_invalid_claim_types_or_purpose_are_rejected(secret, field, value) -> None:
    claims = _claims()
    claims[field] = value
    with pytest.raises(completion.CompletionTokenError, match="invalid_deletion_completion_token"):
        completion.verify_completion_token(_signed_payload(claims), now=NOW)


@pytest.mark.parametrize("alter", ["extra", "missing", "future", "overlong", "zero_lifetime"])
def test_signed_claim_schema_and_lifetime_are_bounded(secret, alter) -> None:
    claims = _claims()
    if alter == "extra":
        claims["extra"] = True
    elif alter == "missing":
        del claims["user_id"]
    elif alter == "future":
        claims["iat"] += 31
        claims["exp"] += 31
    elif alter == "overlong":
        claims["exp"] += 1
    else:
        claims["exp"] = claims["iat"]
    with pytest.raises(completion.CompletionTokenError):
        completion.verify_completion_token(_signed_payload(claims), now=NOW)


@pytest.mark.parametrize("token", ["", "a" * 1025, "invalid", "a.b.c", "a=.b", "."])
def test_malformed_tokens_are_rejected_without_input_in_error(secret, token) -> None:
    with pytest.raises(completion.CompletionTokenError) as exc:
        completion.verify_completion_token(token, now=NOW)
    assert str(exc.value) == "invalid_deletion_completion_token"


def test_media_tokens_and_completion_tokens_cannot_cross_protocols(secret) -> None:
    media = mint_media_token("posts/owned/image.jpg", str(uuid.uuid4()), now=NOW)
    with pytest.raises(completion.CompletionTokenError):
        completion.verify_completion_token(media, now=NOW)
    token, _ = completion.mint_completion_token(uuid.uuid4(), uuid.uuid4(), now=NOW)
    with pytest.raises(HTTPException) as exc:
        verify_media_token(token, now=NOW)
    assert exc.value.status_code == 403


def test_tampering_truncation_and_key_rotation_fail_closed(secret, monkeypatch) -> None:
    token, _ = completion.mint_completion_token(uuid.uuid4(), uuid.uuid4(), now=NOW)
    payload, signature = token.split(".")
    for damaged in (payload + "." + signature[:-1], payload + "." + "A" * 43,
                    "A" + payload[1:] + "." + signature):
        with pytest.raises(completion.CompletionTokenError):
            completion.verify_completion_token(damaged, now=NOW)
    monkeypatch.setattr(get_settings(), "media_signing_secret", "rotated-key")
    with pytest.raises(completion.CompletionTokenError):
        completion.verify_completion_token(token, now=NOW)


@pytest.mark.parametrize("key", [None, "", "   "])
def test_absent_secret_refuses_mint_and_verify(monkeypatch, key) -> None:
    monkeypatch.setattr(get_settings(), "media_signing_secret", key)
    for operation in (
        lambda: completion.mint_completion_token(uuid.uuid4(), uuid.uuid4()),
        lambda: completion.verify_completion_token("not-a-token"),
    ):
        with pytest.raises(completion.CompletionTokenError) as exc:
            operation()
        assert exc.value.status_code == 503


async def _request_token(client: AsyncClient, owner) -> tuple[str, str]:
    request = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    assert request.status_code == 201, request.text
    request_id = request.json()["id"]
    issued = await client.post(f"/me/data-requests/{request_id}/completion-token", headers=owner.headers)
    assert issued.status_code == 200, issued.text
    assert issued.headers["cache-control"] == "private, no-store"
    assert issued.json()["request_id"] == request_id
    assert issued.json()["user_id"] == owner.id
    assert set(issued.json()) == {"token", "user_id", "request_id", "expires_at"}
    return request_id, issued.json()["token"]


async def _complete(request_id: str, user_id: str, *, session=None) -> None:
    """Commit the exact final transition shape; no external providers are invoked."""
    if session is None:
        async with get_session_factory()() as owned:
            await _complete(request_id, user_id, session=owned)
            await owned.commit()
        return
    request = await session.get(models.AccountDataRequest, uuid.UUID(request_id))
    user = await session.get(models.User, uuid.UUID(user_id))
    step = await session.scalar(select(models.AccountFulfillmentStep).where(
        models.AccountFulfillmentStep.request_id == request.id,
        models.AccountFulfillmentStep.step_key == "database_tombstone",
    ))
    request.status = "completed"
    request.completed_at = datetime.now(timezone.utc)
    request.failure_code = None
    request.open_key = None
    user.is_ghost = True
    user.firebase_uid = f"deleted:{user_id}"
    user.email = f"deleted+{user_id}@invalid.chirp"
    step.status = "succeeded"
    step.completed_at = request.completed_at
    step.attempt_count = 1


async def _check(client: AsyncClient, token: str, *, expected: bool) -> dict:
    response = await client.post("/account-deletion/completion", json={"token": token})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json()["completed"] is expected
    assert set(response.json()) == {"request_id", "user_id", "completed"}
    return response.json()


async def test_owner_capability_observes_completion_after_firebase_identity_is_removed(
    client, make_user, api_secret, monkeypatch,
) -> None:
    owner = await make_user("Completion owner")
    request_id, token = await _request_token(client, owner)
    await _check(client, token, expected=False)
    await _complete(request_id, owner.id)
    # The old ordinary status route can no longer find this Firebase UID.
    old_status = await client.get(f"/me/data-requests/{request_id}", headers=owner.headers)
    assert old_status.status_code == 401
    from app.middleware import auth
    async def no_firebase(*args, **kwargs):
        raise AssertionError("completion must not verify a deleted Firebase user")
    monkeypatch.setattr(auth, "_verify_identity", no_firebase)
    assert await _check(client, token, expected=True) == {
        "request_id": request_id, "user_id": owner.id, "completed": True,
    }


async def test_mint_is_fresh_authenticated_owner_only_and_refuses_exports(
    client, make_user, api_secret, monkeypatch,
) -> None:
    owner, stranger = await make_user("Owner"), await make_user("Stranger")
    request_id, _ = await _request_token(client, owner)
    path = f"/me/data-requests/{request_id}/completion-token"
    assert (await client.post(path)).status_code == 401
    assert (await client.post(path, headers=stranger.headers)).status_code == 404
    export = await client.post("/me/data-requests", json={"kind": "export"}, headers=owner.headers)
    assert export.status_code == 201
    denied = await client.post(f"/me/data-requests/{export.json()['id']}/completion-token", headers=owner.headers)
    assert denied.status_code == 409
    from app.middleware import auth
    calls = []
    async def stale_identity(*args, **kwargs):
        calls.append(kwargs)
        raise HTTPException(401, "recent_authentication_required")
    monkeypatch.setattr(auth, "_verify_identity", stale_identity)
    assert (await client.post(path, headers=owner.headers)).status_code == 401
    assert calls == [{"require_fresh": True}]


@pytest.mark.parametrize("status", ["received", "verifying", "processing", "blocked", "failed", "canceled"])
async def test_nonterminal_or_failed_request_never_proves_completion(client, make_user, api_secret, status) -> None:
    owner = await make_user("Nonterminal")
    request_id, token = await _request_token(client, owner)
    await _complete(request_id, owner.id)
    async with get_session_factory()() as session:
        request = await session.get(models.AccountDataRequest, uuid.UUID(request_id))
        request.status = status
        await session.commit()
    await _check(client, token, expected=False)


@pytest.mark.parametrize("target,field,value", [
    ("request", "completed_at", None), ("request", "kind", "export"),
    ("request", "failure_code", "provider_outcome_unknown"), ("request", "open_key", "open"),
    ("request", "fulfillment_lease_token", "uuid"), ("request", "fulfillment_lease_expires_at", "now"),
    ("user", "is_ghost", False), ("user", "firebase_uid", "still-live"), ("user", "email", "still-live@example.test"),
    ("step", "status", "running"), ("step", "completed_at", None), ("step", "attempt_count", 0),
    ("step", "idempotency_key", "different-request"), ("step", "last_error", "provider_outcome_unknown"),
    ("step", "lease_token", "uuid"), ("step", "lease_expires_at", "now"),
])
async def test_inconsistent_tombstone_never_proves_completion(client, make_user, api_secret, target, field, value) -> None:
    owner = await make_user("Inconsistent")
    request_id, token = await _request_token(client, owner)
    await _complete(request_id, owner.id)
    async with get_session_factory()() as session:
        if target == "request":
            row = await session.get(models.AccountDataRequest, uuid.UUID(request_id))
        elif target == "user":
            row = await session.get(models.User, uuid.UUID(owner.id))
        else:
            row = await session.scalar(select(models.AccountFulfillmentStep).where(
                models.AccountFulfillmentStep.request_id == uuid.UUID(request_id),
                models.AccountFulfillmentStep.step_key == "database_tombstone",
            ))
        value = uuid.uuid4() if value == "uuid" else datetime.now(timezone.utc) if value == "now" else value
        setattr(row, field, value)
        await session.commit()
    await _check(client, token, expected=False)


async def test_missing_tombstone_and_foreign_request_binding_fail_closed(client, make_user, api_secret) -> None:
    owner, stranger = await make_user("Owner"), await make_user("Other")
    request_id, token = await _request_token(client, owner)
    await _complete(request_id, owner.id)
    foreign_token, _ = completion.mint_completion_token(uuid.UUID(request_id), uuid.UUID(stranger.id))
    await _check(client, foreign_token, expected=False)
    missing_token, _ = completion.mint_completion_token(uuid.uuid4(), uuid.UUID(owner.id))
    await _check(client, missing_token, expected=False)
    async with get_session_factory()() as session:
        step = await session.scalar(select(models.AccountFulfillmentStep).where(
            models.AccountFulfillmentStep.request_id == uuid.UUID(request_id),
            models.AccountFulfillmentStep.step_key == "database_tombstone",
        ))
        await session.delete(step)
        await session.commit()
    await _check(client, token, expected=False)


async def test_invalid_expired_and_unconfigured_api_results_have_no_completion(client, api_secret, monkeypatch) -> None:
    expired, _ = completion.mint_completion_token(uuid.uuid4(), uuid.uuid4(), now=datetime.now(timezone.utc) - timedelta(days=91))
    for token, code in (("malformed", 403), (expired, 410)):
        response = await client.post("/account-deletion/completion", json={"token": token})
        assert response.status_code == code
        assert response.headers["cache-control"] == "private, no-store"
        assert "completed" not in response.json()
        assert token not in response.text
    monkeypatch.setattr(get_settings(), "media_signing_secret", None)
    response = await client.post("/account-deletion/completion", json={"token": "malformed"})
    assert response.status_code == 503


async def test_uncommitted_tombstone_cannot_authorize_local_erasure(client, make_user, api_secret) -> None:
    owner = await make_user("Uncommitted")
    request_id, token = await _request_token(client, owner)
    async with get_session_factory()() as session:
        await _complete(request_id, owner.id, session=session)
        await session.flush()
        await _check(client, token, expected=False)
        await session.commit()
    await _check(client, token, expected=True)


@pytest.mark.parametrize("body", [
    {"token": "secret-input-" + "x" * 1024},
    {"token": ["secret-input"]},
    {"token": "secret-input", "extra": "secret-input"},
    {},
])
async def test_validation_errors_never_echo_capability_material(client, api_secret, body) -> None:
    response = await client.post("/account-deletion/completion", json=body)
    assert response.status_code == 422
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json() == {"detail": "invalid_deletion_completion_request"}


async def test_mint_auth_errors_are_not_cacheable(client, api_secret) -> None:
    response = await client.post(f"/me/data-requests/{uuid.uuid4()}/completion-token")
    assert response.status_code == 401
    assert response.headers["cache-control"] == "private, no-store"


def test_existing_access_log_tripwire_scrubs_accidentally_queried_completion_token(secret) -> None:
    token, _ = completion.mint_completion_token(uuid.uuid4(), uuid.uuid4(), now=NOW)
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
                               "POST /account-deletion/completion?token=" + token, (), None)
    assert _RedactCredentialQueryParamsFilter().filter(record)
    assert token not in record.getMessage()
    assert "[REDACTED]" in record.getMessage()
