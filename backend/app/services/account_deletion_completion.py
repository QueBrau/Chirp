"""Narrow completion capabilities that survive deletion of the Firebase identity."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.config import get_settings

TOKEN_TTL_SECONDS = 90 * 24 * 60 * 60
MAX_TOKEN_LENGTH = 1024
_KEY_CONTEXT = b"chirp:account-deletion-completion:v1"
_PURPOSE = "account-deletion-completion"
_B64 = re.compile(r"^[A-Za-z0-9_-]+$")


class CompletionTokenError(ValueError):
    """A sanitized, credential-free capability validation failure."""

    def __init__(self, detail: str, status_code: int = 403) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True)
class CompletionClaims:
    request_id: uuid.UUID
    user_id: uuid.UUID
    expires_at: datetime


def _key() -> bytes:
    secret = get_settings().media_signing_secret
    if not secret or not secret.strip():
        raise CompletionTokenError("deletion_completion_not_configured", 503)
    # A distinct subkey and wire purpose prevent media capabilities from being
    # interpreted as deletion proof. Rotating the root secret invalidates both.
    return hmac.new(secret.encode("utf-8"), _KEY_CONTEXT, hashlib.sha256).digest()


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    if _B64.fullmatch(value) is None:
        raise ValueError("invalid_encoding")
    decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    if _b64(decoded) != value:
        raise ValueError("noncanonical_encoding")
    return decoded


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def mint_completion_token(
    request_id: uuid.UUID, user_id: uuid.UUID, *, now: datetime | None = None,
) -> tuple[str, CompletionClaims]:
    """Mint only after the router establishes fresh authentication and ownership."""
    issued_at = int((now or datetime.now(timezone.utc)).timestamp())
    expiry = issued_at + TOKEN_TTL_SECONDS
    payload = _json({
        "version": 1, "purpose": _PURPOSE, "request_id": str(request_id),
        "user_id": str(user_id), "iat": issued_at, "exp": expiry,
    })
    signature = hmac.new(_key(), payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(signature)}", CompletionClaims(
        request_id, user_id, datetime.fromtimestamp(expiry, timezone.utc),
    )


def verify_completion_token(token: str, *, now: datetime | None = None) -> CompletionClaims:
    """Authenticate bounded claims without consulting the deleted Firebase user."""
    key = _key()
    try:
        if type(token) is not str or not 1 <= len(token) <= MAX_TOKEN_LENGTH:
            raise ValueError("token_shape")
        encoded, encoded_signature = token.split(".")
        payload, signature = _decode(encoded), _decode(encoded_signature)
        expected = hmac.new(key, payload, hashlib.sha256).digest()
        if len(signature) != 32 or not hmac.compare_digest(signature, expected):
            raise ValueError("token_signature")
        value = json.loads(payload)
        if type(value) is not dict or set(value) != {
            "version", "purpose", "request_id", "user_id", "iat", "exp",
        } or _json(value) != payload:
            raise ValueError("token_claims")
        if type(value["version"]) is not int or value["version"] != 1 or value["purpose"] != _PURPOSE:
            raise ValueError("token_purpose")
        request_id, user_id = uuid.UUID(value["request_id"]), uuid.UUID(value["user_id"])
        if str(request_id) != value["request_id"] or str(user_id) != value["user_id"]:
            raise ValueError("token_identity")
        issued_at, expiry = value["iat"], value["exp"]
        current = int((now or datetime.now(timezone.utc)).timestamp())
        if (type(issued_at) is not int or type(expiry) is not int
                or issued_at < 0 or issued_at > current + 30
                or not 0 < expiry - issued_at <= TOKEN_TTL_SECONDS):
            raise ValueError("token_lifetime")
        if current >= expiry:
            raise CompletionTokenError("deletion_completion_token_expired", 410)
        return CompletionClaims(request_id, user_id, datetime.fromtimestamp(expiry, timezone.utc))
    except CompletionTokenError:
        raise
    except (ValueError, TypeError, AttributeError, UnicodeError, binascii.Error, OverflowError):
        raise CompletionTokenError("invalid_deletion_completion_token") from None


async def is_deletion_completed(session: AsyncSession, claims: CompletionClaims) -> bool:
    """Read the exact committed request and tombstone in one database snapshot.

    This proves the account tombstone, not expiry of retained provider copies or
    completion of deferred cleanup. The capability grants no export or write.
    """
    request, user, step = models.AccountDataRequest, models.User, models.AccountFulfillmentStep
    result = await session.scalar(
        select(request.id)
        .join(user, user.id == request.user_id)
        .join(step, step.request_id == request.id)
        .where(
            request.id == claims.request_id, request.user_id == claims.user_id,
            request.kind == "deletion", request.status == "completed",
            request.completed_at.is_not(None), request.failure_code.is_(None),
            request.open_key.is_(None), request.fulfillment_lease_token.is_(None),
            request.fulfillment_lease_expires_at.is_(None),
            user.is_ghost.is_(True), user.firebase_uid == f"deleted:{claims.user_id}",
            user.email == f"deleted+{claims.user_id}@invalid.chirp",
            step.step_key == "database_tombstone", step.status == "succeeded",
            step.completed_at.is_not(None), step.attempt_count > 0,
            step.idempotency_key == f"account-deletion:{claims.request_id}:database_tombstone",
            step.last_error.is_(None), step.lease_token.is_(None), step.lease_expires_at.is_(None),
        )
    )
    return result is not None
