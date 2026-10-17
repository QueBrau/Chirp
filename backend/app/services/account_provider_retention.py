"""Explicit disclosure receipt for email, operational-log, and backup retention.

This adapter does not call or delete anything at an external provider. Its receipt
records the already-approved retained-data boundary and never claims provider
readback, deletion, or a measured expiry.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from app import models
from app.config import get_settings

ProviderBackend = Literal["log", "resend", "ses"]
RETENTION_CODES = (
    "email_delivery_records",
    "operational_logs",
    "database_backups",
)
SCOPE = (
    "email_provider_delivery_records",
    "cloud_operational_logs",
    "database_backups",
)
_ALLOWED_BACKENDS = frozenset({"log", "resend", "ses"})
_JOURNAL_FIELDS = frozenset({
    "receipt_type", "provider", "subject", "request_id", "action", "expiry",
    "email_backend", "scope", "retained_with_reason",
})


def _canonical_uuid(value: object) -> str:
    if isinstance(value, uuid.UUID):
        parsed = uuid.UUID(int=value.int)
        return str(parsed)
    if type(value) is not str:
        raise ValueError("retention_receipt_invalid")
    try:
        parsed = uuid.UUID(value)
    except (TypeError, ValueError, AttributeError):
        raise ValueError("retention_receipt_invalid") from None
    if str(parsed) != value:
        raise ValueError("retention_receipt_invalid")
    return value


@dataclass(frozen=True)
class RetentionOnlyReceipt:
    """A fixed, owner/request-bound retention acknowledgment, not a delete receipt."""

    provider: Literal["email_logs_backups"]
    subject: str
    request_id: str
    action: Literal["retained_disclosed"]
    expiry: Literal["unknown"]
    email_backend: ProviderBackend
    scope: tuple[str, str, str]
    retained_with_reason: tuple[str, str, str]

    def validate(self, *, subject: object, request_id: object) -> None:
        expected_subject = _canonical_uuid(subject)
        expected_request = _canonical_uuid(request_id)
        if (
            self.provider != "email_logs_backups"
            or _canonical_uuid(self.subject) != expected_subject
            or _canonical_uuid(self.request_id) != expected_request
            or self.action != "retained_disclosed"
            or self.expiry != "unknown"
            or type(self.email_backend) is not str
            or self.email_backend not in _ALLOWED_BACKENDS
            or type(self.scope) is not tuple
            or self.scope != SCOPE
            or type(self.retained_with_reason) is not tuple
            or self.retained_with_reason != RETENTION_CODES
        ):
            raise ValueError("retention_receipt_invalid")

    def journal_value(self) -> dict[str, object]:
        self.validate(subject=self.subject, request_id=self.request_id)
        return {
            "receipt_type": "retention_only",
            "provider": self.provider,
            "subject": self.subject,
            "request_id": self.request_id,
            "action": self.action,
            "expiry": self.expiry,
            "email_backend": self.email_backend,
            "scope": list(self.scope),
            "retained_with_reason": list(self.retained_with_reason),
        }


def retention_only_receipt_from_journal(
    value: object, *, subject: object, request_id: object,
) -> RetentionOnlyReceipt:
    """Parse the exact JSON form stored in a fulfillment step."""
    expected_subject = _canonical_uuid(subject)
    expected_request = _canonical_uuid(request_id)
    if type(value) is not dict or set(value) != _JOURNAL_FIELDS:
        raise ValueError("retention_receipt_invalid")
    scope = value.get("scope")
    retained = value.get("retained_with_reason")
    if type(scope) is not list or type(retained) is not list:
        raise ValueError("retention_receipt_invalid")
    receipt = RetentionOnlyReceipt(
        provider=value.get("provider"),  # type: ignore[arg-type]
        subject=_canonical_uuid(value.get("subject")),
        request_id=_canonical_uuid(value.get("request_id")),
        action=value.get("action"),  # type: ignore[arg-type]
        expiry=value.get("expiry"),  # type: ignore[arg-type]
        email_backend=value.get("email_backend"),  # type: ignore[arg-type]
        scope=tuple(scope),  # type: ignore[arg-type]
        retained_with_reason=tuple(retained),  # type: ignore[arg-type]
    )
    receipt.validate(subject=expected_subject, request_id=expected_request)
    return receipt


class EmailLogsBackupsRetentionProvider:
    """Record fixed disclosed retention categories without external provider calls."""

    def __init__(self, *, request_id: uuid.UUID, email_backend: str | None = None) -> None:
        self._request_id = _canonical_uuid(request_id)
        backend = email_backend if email_backend is not None else get_settings().email_provider
        if type(backend) is not str or backend not in _ALLOWED_BACKENDS:
            raise ValueError("retention_backend_invalid")
        self._email_backend: ProviderBackend = backend  # type: ignore[assignment]

    async def delete_account_data(
        self, *, user: models.User, idempotency_key: str,
    ) -> RetentionOnlyReceipt:
        expected_key = f"account-deletion:{self._request_id}:email_logs_backups"
        if (
            not isinstance(user.id, uuid.UUID)
            or _canonical_uuid(user.id) != _canonical_uuid(str(user.id))
            or idempotency_key != expected_key
        ):
            raise ValueError("retention_request_binding_invalid")
        receipt = RetentionOnlyReceipt(
            provider="email_logs_backups",
            subject=str(uuid.UUID(int=user.id.int)),
            request_id=self._request_id,
            action="retained_disclosed",
            expiry="unknown",
            email_backend=self._email_backend,
            scope=SCOPE,
            retained_with_reason=RETENTION_CODES,
        )
        receipt.validate(subject=user.id, request_id=self._request_id)
        return receipt
