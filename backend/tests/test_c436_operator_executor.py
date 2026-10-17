from __future__ import annotations

import json
import os
import stat
import time
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.config import get_settings
from app.db import get_session_factory
from app.jobs import account_deletion_executor as executor
from app.services import account_provider_analytics as analytics
from app.services import account_provider_gcs as media
from app.services.account_fulfillment import PROVIDER_STEPS
from tests.c436_executor_helpers import activate_executor_hold
from tests.conftest import ApiUser


def _snapshots(request_id: uuid.UUID, user_id: uuid.UUID) -> dict[str, object]:
    media_snapshot = media.MediaSnapshot(
        user_id=str(user_id), bucket="chirp-test-bucket", prefixes=media.PREFIXES,
        objects=(), freeze_id="deferred", snapshot_digest="",
    )
    media_snapshot = replace(media_snapshot, snapshot_digest=media._digest(media_snapshot.payload()))
    freeze = analytics.AnalyticsFreezeReceipt(
        user_id=str(user_id), freeze_id="reviewed-freeze-1",
        expires_at=time.time() + 600, verified=True, ingestion_drained=False,
    )
    table = analytics.AnalyticsTable(
        name="run_googleapis_com_stdout_20261016", schema_digest="0" * 64,
        preview_count=0, max_bytes_billed=10_000_000, estimated_delete_bytes=0,
    )
    snapshot = analytics.AnalyticsSnapshot(
        request_id=str(request_id), user_id=str(user_id), project="chirps-prod",
        dataset="chirp_analytics", tables=(table,), exclusions=(),
        freeze_id=freeze.freeze_id, created_at=time.time(), time_travel_hours=168,
    )
    snapshot = replace(snapshot, snapshot_digest=analytics._digest(snapshot.payload()))
    return {
        "stripe_references": [],
        "media_snapshot": executor_factory.serialize_media_snapshot(media_snapshot),
        "media_references": [],
        "media_retention": {
            "bucket": "chirp-test-bucket", "versioning_enabled": False,
            "soft_delete_seconds": 604800, "verified": True,
        },
        "analytics_snapshot": executor_factory.serialize_analytics_snapshot(snapshot),
        "analytics_freeze": {
            "user_id": freeze.user_id, "freeze_id": freeze.freeze_id,
            "expires_at": freeze.expires_at, "verified": True,
            "ingestion_drained": False,
        },
    }


from app.services import account_deletion_provider_factory as executor_factory


def _approval(request_id: uuid.UUID, user_id: uuid.UUID, database_name: str, role: str) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    return {
        "type": "c436_operator_approval_v1",
        "request_id": str(request_id), "user_id": str(user_id),
        "database_name": database_name, "database_role": role, "schema_head": "0050",
        "reviewed_by": "test operator", "reviewed_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=45)).isoformat(),
        "old_revisions_drained": True,
        "admission_flags": {key: True for key in executor.ADMISSION_FLAGS},
        "firebase_project": "chirps-prod",
        "analytics_project": "chirps-prod",
        "analytics_dataset": "chirp_analytics",
        "bucket": "chirp-test-bucket", "soft_delete_seconds": 604800,
        "analytics_tables": ["run_googleapis_com_stdout_20261016"],
        "analytics_exclusions": [],
        "analytics_freeze": {
            "user_id": str(user_id), "freeze_id": "reviewed-freeze-1",
            "expires_at": time.time() + 600, "verified": True,
            "ingestion_drained": False,
        },
    }


class _ConfirmingProvider:
    def __init__(self, key: str) -> None:
        self.key = key
        self.calls: list[str] = []

    async def delete_account_data(self, *, user, idempotency_key: str) -> dict[str, object]:
        self.calls.append(idempotency_key)
        result: dict[str, object] = {
            "confirmed": True, "provider": self.key, "subject": str(user.id),
            "scope": [self.key], "readback": True,
        }
        if self.key == "analytics":
            result.update({
                "request_id": idempotency_key.split(":")[1],
                "snapshot_digest": "0" * 64, "scope": ["run_googleapis_com_stdout_20261016"],
                "time_travel_hours": 168, "fail_safe_days": 7, "excluded_scope": [],
                "retained_with_reason": ["bigquery_time_travel", "bigquery_fail_safe"],
            })
        return result


@pytest.mark.asyncio
async def test_prepare_review_execute_is_bound_and_provider_injected(
    client, make_user, tmp_path, monkeypatch,
) -> None:
    owner: ApiUser = await make_user("Operator executor")
    response = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    assert response.status_code == 201, response.text
    request_id, user_id = uuid.UUID(response.json()["id"]), uuid.UUID(owner.id)
    await activate_executor_hold(user_id=user_id, request_id=request_id)

    async with get_session_factory()() as session:
        db_name, role = (await session.execute(text("SELECT current_database(), current_user"))).one()
    database_url = make_url(os.environ["DATABASE_URL"])
    assert database_url.database == db_name and database_url.username == role
    settings = get_settings().model_copy(update={
        **{key: True for key in executor.ADMISSION_FLAGS},
        "media_bucket_name": "chirp-test-bucket",
    })
    monkeypatch.setattr(executor, "get_settings", lambda: settings)
    monkeypatch.setenv(executor.GCS_METADATA_ENV, "true")

    tmp_path.chmod(0o700)
    approval_path = tmp_path / "approval.json"
    plan_path = tmp_path / "plan.json"
    approval_path.write_text(json.dumps(_approval(request_id, user_id, db_name, role)))
    approval_path.chmod(0o600)

    async def snapshot_builder(**kwargs):
        assert kwargs["request_id"] == request_id
        assert kwargs["user"].id == user_id
        return _snapshots(request_id, user_id)

    digest = await executor.prepare_deletion_plan(
        request_id=request_id, expected_user_id=user_id, plan_path=plan_path,
        approval_path=approval_path, expected_database=db_name, expected_role=role,
        snapshot_builder=snapshot_builder,
    )
    assert stat.S_IMODE(plan_path.stat().st_mode) == 0o600
    assert _ConfirmingProvider("unused").calls == []

    providers = {key: _ConfirmingProvider(key) for key in PROVIDER_STEPS}
    built: list[bool] = []

    async def provider_builder(**kwargs):
        built.append(True)
        assert kwargs["request_id"] == request_id
        assert kwargs["user_id"] == user_id
        return providers, None

    with pytest.raises(executor.ExecutorRefused, match="plan_digest_mismatch"):
        await executor.execute_deletion_plan(
            plan_path=plan_path, approval_path=approval_path, confirm_digest="0" * 64,
            expected_request_id=request_id, expected_user_id=user_id,
            expected_database=db_name, expected_role=role,
            provider_builder=provider_builder,
        )
    assert not built

    result = await executor.execute_deletion_plan(
        plan_path=plan_path, approval_path=approval_path, confirm_digest=digest,
        expected_request_id=request_id, expected_user_id=user_id,
        expected_database=db_name, expected_role=role,
        provider_builder=provider_builder,
    )
    assert result["status"] == "completed"
    assert built == [True]
    for key, provider in providers.items():
        assert provider.calls == [f"account-deletion:{request_id}:{key}"]
