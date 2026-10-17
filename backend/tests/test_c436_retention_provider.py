from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import models
from app.db import get_session_factory
from app.jobs.account_deletion_preflight import _receipt
from app.services.account_fulfillment import (
    FirebaseProvider,
    ProviderUnavailable,
    PROVIDER_RETENTION_REASONS,
    _validate_step_receipt,
    PROVIDER_STEPS,
    run_deletion,
)
from app.services.account_provider_retention import (
    RETENTION_CODES,
    SCOPE,
    EmailLogsBackupsRetentionProvider,
    RetentionOnlyReceipt,
    retention_only_receipt_from_journal,
)


def _receipt_value(user_id: uuid.UUID, request_id: uuid.UUID) -> dict[str, object]:
    return {
        "receipt_type": "retention_only",
        "provider": "email_logs_backups",
        "subject": str(user_id),
        "request_id": str(request_id),
        "action": "retained_disclosed",
        "expiry": "unknown",
        "email_backend": "log",
        "scope": list(SCOPE),
        "retained_with_reason": list(RETENTION_CODES),
    }


@pytest.mark.asyncio
async def test_retention_provider_returns_only_fixed_disclosure_and_roundtrips() -> None:
    user_id, request_id = uuid.uuid4(), uuid.uuid4()
    provider = EmailLogsBackupsRetentionProvider(request_id=request_id, email_backend="resend")
    receipt = await provider.delete_account_data(
        user=SimpleNamespace(id=user_id),
        idempotency_key=f"account-deletion:{request_id}:email_logs_backups",
    )
    assert isinstance(receipt, RetentionOnlyReceipt)
    stored = receipt.journal_value()
    assert retention_only_receipt_from_journal(
        stored, subject=user_id, request_id=request_id,
    ) == receipt
    codes, validated = _validate_step_receipt("email_logs_backups", user_id, stored, request_id)
    assert codes == list(RETENTION_CODES)
    assert validated == receipt
    ready, summary, error = _receipt("email_logs_backups", stored, user_id, request_id)
    assert ready and error is None
    assert summary == {
        "present": True,
        "disposition": "retained_disclosed",
        "expiry": "unknown",
        "email_backend": "resend",
        "scope_count": 3,
        "retention_code_count": 3,
    }
    assert "confirmed" not in summary and "readback" not in summary


@pytest.mark.asyncio
async def test_retention_provider_rejects_wrong_request_and_noncanonical_receipts() -> None:
    user_id, request_id = uuid.uuid4(), uuid.uuid4()
    provider = EmailLogsBackupsRetentionProvider(request_id=request_id)
    with pytest.raises(ValueError, match="retention_request_binding_invalid"):
        await provider.delete_account_data(
            user=SimpleNamespace(id=user_id),
            idempotency_key="account-deletion:wrong:email_logs_backups",
        )

    base = _receipt_value(user_id, request_id)
    invalid_values = []
    for field, value in (
        ("subject", str(uuid.uuid4())),
        ("request_id", str(uuid.uuid4())),
        ("action", "deleted"),
        ("expiry", "30_days"),
        ("email_backend", "unknown"),
        ("scope", ["email_provider_delivery_records"]),
        ("retained_with_reason", ["database_backups"]),
        ("confirmed", True),
        ("readback", True),
    ):
        candidate = dict(base)
        if field in {"confirmed", "readback"}:
            candidate[field] = value
        else:
            candidate[field] = value
        invalid_values.append(candidate)
    for candidate in invalid_values:
        with pytest.raises(ProviderUnavailable, match="^provider_confirmation_missing$"):
            _validate_step_receipt("email_logs_backups", user_id, candidate, request_id)
    with pytest.raises(ProviderUnavailable, match="^provider_confirmation_missing$"):
        _validate_step_receipt("analytics", user_id, base, request_id)


@pytest.mark.asyncio
async def test_firebase_provider_binds_named_app_only_when_supplied(monkeypatch) -> None:
    from firebase_admin import auth

    app = object()
    calls: list[tuple[str, str, object | None]] = []

    def missing(uid: str, *, app=None) -> object:
        calls.append(("delete", uid, app))
        raise auth.UserNotFoundError("missing")

    def readback(uid: str, *, app=None) -> object:
        calls.append(("readback", uid, app))
        raise auth.UserNotFoundError("missing")

    monkeypatch.setattr(auth, "delete_user", missing)
    monkeypatch.setattr(auth, "get_user", readback)
    user = SimpleNamespace(id=uuid.uuid4(), firebase_uid="firebase-retention-test")

    result = await FirebaseProvider(app=app).delete_account_data(
        user=user, idempotency_key="firebase-test",
    )
    assert result["confirmed"] is True
    assert calls == [
        ("delete", user.firebase_uid, app),
        ("readback", user.firebase_uid, app),
    ]

    calls.clear()
    def legacy_missing(uid: str) -> object:
        calls.append(("delete", uid, None))
        raise auth.UserNotFoundError("missing")

    def legacy_readback(uid: str) -> object:
        calls.append(("readback", uid, None))
        raise auth.UserNotFoundError("missing")

    monkeypatch.setattr(auth, "delete_user", legacy_missing)
    monkeypatch.setattr(auth, "get_user", legacy_readback)
    await FirebaseProvider().delete_account_data(user=user, idempotency_key="firebase-test")
    assert calls == [
        ("delete", user.firebase_uid, None),
        ("readback", user.firebase_uid, None),
    ]


@pytest.mark.asyncio
async def test_retention_receipt_is_journaled_in_successful_deletion(
    client, make_user,
) -> None:
    from tests.c436_executor_helpers import activate_executor_hold
    from tests.test_c436_fulfillment import provider_set

    owner = await make_user("Retention adapter end to end")
    response = await client.post(
        "/me/data-requests", json={"kind": "deletion"}, headers=owner.headers,
    )
    assert response.status_code == 201, response.text
    request_id = uuid.UUID(response.json()["id"])
    await activate_executor_hold(user_id=uuid.UUID(owner.id), request_id=request_id)
    providers = provider_set()
    providers["email_logs_backups"] = EmailLogsBackupsRetentionProvider(
        request_id=request_id, email_backend="log",
    )

    async with get_session_factory()() as session:
        request = await session.get(models.AccountDataRequest, request_id)
        user = await session.get(models.User, uuid.UUID(owner.id))
        assert request is not None and user is not None
        result = await run_deletion(session, request_id, user, providers)
        assert result.status == "completed"
        step = (await session.scalars(select(models.AccountFulfillmentStep).where(
            models.AccountFulfillmentStep.request_id == request_id,
            models.AccountFulfillmentStep.step_key == "email_logs_backups",
        ))).one()
        assert step.status == "succeeded"
        assert step.provider_ref == _receipt_value(uuid.UUID(owner.id), request_id)
        await session.refresh(request)
        assert set(request.retention_reasons) >= {
            PROVIDER_RETENTION_REASONS[code] for code in RETENTION_CODES
        }
        assert all(
            provider.keys == [f"account-deletion:{request_id}:{key}"]
            for key, provider in providers.items() if key != "email_logs_backups"
        )
