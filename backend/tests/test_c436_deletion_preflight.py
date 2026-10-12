"""Real-PostgreSQL tests for the read-only deletion preflight."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import models
from app.db import get_session_factory
from app.jobs.account_deletion_preflight import inspect_request, run_preflight


@pytest.mark.asyncio
async def test_preflight_binds_owner_and_reports_blocked_plan(client, make_user, database_url):
    owner = await make_user("Preflight owner")
    response = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    assert response.status_code == 201, response.text
    request_id = uuid.UUID(response.json()["id"])
    result = await run_preflight(
        database_url, request_id=request_id, expected_user_id=uuid.UUID(owner.id)
    )
    assert result["status"] == "ok"
    assert result["request_found"] is True
    assert result["request_kind"] == "deletion"
    assert result["request_status"] == "blocked"
    assert result["ready_for_deletion"] is False
    assert "provider_not_configured" not in result["receipt_errors"]
    assert set(result["step_statuses"]) == {
        "payment_provider", "media_storage", "email_logs_backups", "firebase_auth", "database_tombstone",
    }
    assert "provider_ref" not in str(result)
    assert owner.email not in str(result)

    foreign = await make_user("Preflight foreign")
    foreign_result = await run_preflight(
        database_url, request_id=request_id, expected_user_id=uuid.UUID(foreign.id)
    )
    assert foreign_result == {"status": "not_found_or_owner_mismatch", "request_found": False}


@pytest.mark.asyncio
async def test_preflight_detects_active_and_unknown_leases_without_mutating(
    client, make_user, database_url,
):
    owner = await make_user("Preflight lease")
    response = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    request_id = uuid.UUID(response.json()["id"])
    async with get_session_factory()() as session:
        request = await session.get(models.AccountDataRequest, request_id)
        assert request is not None
        before_status = request.status
        request.fulfillment_lease_token = uuid.uuid4()
        request.fulfillment_lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=2)
        step = (await session.scalars(select(models.AccountFulfillmentStep).where(
            models.AccountFulfillmentStep.request_id == request_id,
            models.AccountFulfillmentStep.step_key == "firebase_auth",
        ))).one()
        step.status = "manual_review"
        step.last_error = "firebase_outcome_unknown"
        await session.commit()
    result = await run_preflight(
        database_url, request_id=request_id, expected_user_id=uuid.UUID(owner.id)
    )
    assert "request_lease_active" in result["blockers"]
    assert result["unknown_outcome_steps"] == ["firebase_auth"]
    async with get_session_factory()() as session:
        request = await session.get(models.AccountDataRequest, request_id)
        assert request is not None
        assert request.status == before_status
        assert request.fulfillment_lease_token is not None


@pytest.mark.asyncio
async def test_preflight_rejects_malformed_succeeded_receipt_without_leaking_values(
    client, make_user, database_url,
):
    owner = await make_user("Preflight receipt")
    response = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    request_id = uuid.UUID(response.json()["id"])
    async with get_session_factory()() as session:
        step = (await session.scalars(select(models.AccountFulfillmentStep).where(
            models.AccountFulfillmentStep.request_id == request_id,
            models.AccountFulfillmentStep.step_key == "firebase_auth",
        ))).one()
        step.status = "succeeded"
        step.provider_ref = {
            "confirmed": "true",
            "readback": True,
            "provider": "firebase_auth",
            "subject": owner.id,
            "scope": ["private-secret-scope"],
        }
        await session.commit()
    result = await run_preflight(
        database_url, request_id=request_id, expected_user_id=uuid.UUID(owner.id)
    )
    assert "succeeded_receipt_invalid" in result["blockers"]
    assert result["receipt_errors"] == ["firebase_auth:provider_confirmation_invalid"]
    assert "private-secret-scope" not in str(result)


@pytest.mark.asyncio
async def test_preflight_read_only_report_has_no_provider_execution(client, make_user, database_url):
    owner = await make_user("Preflight read only")
    response = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    request_id = uuid.UUID(response.json()["id"])
    async with get_session_factory()() as session:
        before = await session.scalar(select(models.AccountDataRequest.updated_at).where(
            models.AccountDataRequest.id == request_id
        ))
    await run_preflight(database_url, request_id=request_id, expected_user_id=uuid.UUID(owner.id))
    async with get_session_factory()() as session:
        after = await session.scalar(select(models.AccountDataRequest.updated_at).where(
            models.AccountDataRequest.id == request_id
        ))
    assert after == before


@pytest.mark.asyncio
async def test_preflight_reports_expired_step_and_unknown_extra_step(client, make_user, database_url):
    owner = await make_user("Preflight expired")
    response = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    request_id = uuid.UUID(response.json()["id"])
    async with get_session_factory()() as session:
        step = (await session.scalars(select(models.AccountFulfillmentStep).where(
            models.AccountFulfillmentStep.request_id == request_id,
            models.AccountFulfillmentStep.step_key == "firebase_auth",
        ))).one()
        step.status = "running"
        step.lease_token = uuid.uuid4()
        step.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.add(models.AccountFulfillmentStep(
            request_id=request_id,
            step_key="unrecognized_provider",
            idempotency_key="test-extra",
        ))
        await session.commit()
    result = await run_preflight(
        database_url, request_id=request_id, expected_user_id=uuid.UUID(owner.id)
    )
    assert result["step_leases"]["firebase_auth"] == "expired"
    assert "firebase_auth_outcome_unknown" in result["blockers"]
    assert result["unknown_journal_step_count"] == 1
    assert "unrecognized_provider" not in str(result)
    assert result["durable_completion_evidence"] is False


@pytest.mark.asyncio
async def test_preflight_accepts_only_exact_completed_tombstone_evidence(client, make_user, database_url):
    owner = await make_user("Preflight completed")
    response = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    request_id = uuid.UUID(response.json()["id"])
    now = datetime.now(timezone.utc)
    async with get_session_factory()() as session:
        request = await session.get(models.AccountDataRequest, request_id)
        user = await session.get(models.User, uuid.UUID(owner.id))
        assert request is not None and user is not None
        request.status = "completed"
        request.completed_at = now
        user.is_ghost = True
        user.firebase_uid = f"deleted:{owner.id}"
        user.email = f"deleted+{owner.id}@invalid.chirp"
        for step in (await session.scalars(select(models.AccountFulfillmentStep).where(
            models.AccountFulfillmentStep.request_id == request_id,
        ))).all():
            step.status = "succeeded"
            step.completed_at = now
            if step.step_key == "database_tombstone":
                continue
            step.provider_ref = {
                "confirmed": True,
                "provider": step.step_key,
                "subject": owner.id,
                "scope": [step.step_key],
                "readback": True,
                "retained_with_reason": [],
            }
        await session.commit()
    result = await run_preflight(
        database_url, request_id=request_id, expected_user_id=uuid.UUID(owner.id)
    )
    assert result["durable_completion_evidence"] is True
    assert result["ready_for_deletion"] is False

    async with get_session_factory()() as session:
        user = await session.get(models.User, uuid.UUID(owner.id))
        assert user is not None
        user.email = "deleted+wrong@invalid.chirp"
        await session.commit()
    corrupted = await run_preflight(
        database_url, request_id=request_id, expected_user_id=uuid.UUID(owner.id)
    )
    assert corrupted["tombstone_evidence"] is False
    assert corrupted["durable_completion_evidence"] is False
