"""Real-Postgres tests for the post-tombstone cleanup journal and its trust boundary."""
from __future__ import annotations

import hashlib
import json
import uuid
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.config import get_settings
from app.db import get_session_factory
from app.services.account_fulfillment import PROVIDER_STEPS, run_deletion
from app.routers.data_requests import _cleanup_statuses
from app.services.account_post_deletion_cleanup import (
    CleanupCompletionProof,
    DeferredCleanupReceipt,
    PUBLIC_DISCLOSURES,
    _digest,
    claim_due_cleanup,
    run_post_deletion_cleanup_batch,
    settle_cleanup_claim,
)
from tests.c436_executor_helpers import activate_executor_hold
from tests.test_c436_fulfillment import ConfirmingProvider, provider_set
from tests.test_c436_gcs_provider import FakeReader, freeze, reviewed_retention, user_model
from app.services.account_provider_gcs import GCSMediaProvider, MediaProviderUnavailable, snapshot_user_media
from tests.conftest import ApiUser


def _receipt(category: str, user_id: uuid.UUID, request_id: uuid.UUID,
             *, remaining: int = 0) -> DeferredCleanupReceipt:
    if category == "temporary_media":
        provider = "media_storage"
        scope = {
            "bucket": "chirp-media",
            "tmp_prefix": f"tmp/{user_id}/",
            "initial_snapshot_digest": "a" * 64,
        }
        retained = ("gcs_soft_deleted_versions",)
    else:
        provider = "analytics"
        scope = {
            "project": "chirps-prod",
            "dataset": "run_googleapis_com_stdout",
            "tables": ["run_googleapis_com_stdout_20261016"],
            "table_pattern": "run_googleapis_com_stdout_YYYYMMDD",
            "predicate_version": "user_id_analytics_v1",
            "exclusions": [],
        }
        retained = ("bigquery_time_travel", "bigquery_fail_safe")
    return DeferredCleanupReceipt(
        category=category,
        provider=provider,
        subject=str(user_id),
        request_id=str(request_id),
        scope=scope,
        scope_sha256=_digest(scope),
        pass_digest=hashlib.sha256(f"{category}:{remaining}".encode()).hexdigest(),
        readback=True,
        retained_with_reason=retained,
        remaining_count=remaining if category == "temporary_media" else None,
    )


class DeferredProvider:
    def __init__(self, category: str) -> None:
        self.category = category
        self.deferred_cleanup = True
        self.calls = 0

    async def delete_account_data(self, *, user: models.User, idempotency_key: str):
        self.calls += 1
        request_id = uuid.UUID(idempotency_key.split(":")[1])
        return _receipt(self.category, user.id, request_id)


async def _request_and_hold(client: AsyncClient, make_user, name: str) -> tuple[ApiUser, uuid.UUID]:
    owner = await make_user(name)
    response = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    assert response.status_code == 201, response.text
    request_id = uuid.UUID(response.json()["id"])
    await activate_executor_hold(user_id=uuid.UUID(owner.id), request_id=request_id)
    return owner, request_id


@pytest.mark.asyncio
async def test_deferred_receipts_create_private_obligations_with_tombstone_and_disclose_at_intake(
    client: AsyncClient, make_user, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "account_deletion_deferred_cleanup_enabled", True)
    owner: ApiUser = await make_user("Deferred cleanup owner")
    response = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    assert response.status_code == 201, response.text
    body = response.json()
    request_id = uuid.UUID(body["id"])
    assert set(PUBLIC_DISCLOSURES.values()).issubset(set(body["retention_reasons"]))
    assert "chirps-prod" not in json.dumps(body)
    await activate_executor_hold(user_id=uuid.UUID(owner.id), request_id=request_id)

    providers = provider_set()
    providers["media_storage"] = DeferredProvider("temporary_media")
    providers["analytics"] = DeferredProvider("identifiable_analytics")
    async with get_session_factory()() as session:
        request = await session.get(models.AccountDataRequest, request_id)
        user = await session.get(models.User, uuid.UUID(owner.id))
        assert request is not None and user is not None
        result = await run_deletion(session, request_id, user, providers)
        assert result.status == "completed"

    async with get_session_factory()() as session:
        request = await session.get(models.AccountDataRequest, request_id)
        rows = list((await session.scalars(select(models.AccountPostDeletionCleanup)
                                           .where(models.AccountPostDeletionCleanup.request_id == request_id)
                                           .order_by(models.AccountPostDeletionCleanup.category))).all())
        user = await session.get(models.User, uuid.UUID(owner.id))
        assert request is not None and request.status == "completed"
        assert user is not None and user.is_ghost is True
        assert [(row.category, row.state) for row in rows] == [
            ("identifiable_analytics", "pending"), ("temporary_media", "pending"),
        ]
        assert all(row.user_id == uuid.UUID(owner.id) for row in rows)
        public_status = (await client.get(f"/me/data-requests/{request_id}", headers=owner.headers))
        # The user can no longer authenticate after Firebase deletion; intake held
        # the fixed disclosure before that point, and the journal keeps scope private.
        assert public_status.status_code in {401, 403}


@pytest.mark.asyncio
async def test_deferred_initial_media_pass_removes_captured_public_media_but_keeps_late_tmp(make_user) -> None:
    owner: ApiUser = await make_user("Deferred GCS initial pass")
    reader = FakeReader([
        (f"tmp/{owner.id}/captured.jpg", "1"),
        (f"posts/{owner.id}/post.jpg", "2"),
        (f"avatars/{owner.id}/avatar.jpg", "3"),
    ])
    async with get_session_factory()() as session:
        snapshot = await snapshot_user_media(
            session, uuid.UUID(owner.id), reader, None,
            bucket="chirp-media", deferred_cleanup=True,
        )
    reader.objects[f"tmp/{owner.id}/arrived-late.jpg"] = "4"
    provider = GCSMediaProvider(
        reader, bucket="chirp-media", retention=reviewed_retention(),
        snapshot=snapshot, freeze=None, deferred_cleanup=True,
    )
    result = await provider.delete_account_data(
        user=user_model(owner.id),
        idempotency_key=f"account-deletion:{uuid.uuid4()}:media_storage",
    )
    assert result.remaining_count == 1
    assert reader.objects == {f"tmp/{owner.id}/arrived-late.jpg": "4"}
    assert {name for kind, name in reader.calls if kind == "delete"} == {
        f"tmp/{owner.id}/captured.jpg", f"posts/{owner.id}/post.jpg", f"avatars/{owner.id}/avatar.jpg",
    }


@pytest.mark.asyncio
async def test_deferred_initial_media_pass_blocks_public_generation_drift_before_delete(make_user) -> None:
    owner: ApiUser = await make_user("Deferred GCS public drift")
    reader = FakeReader([(f"posts/{owner.id}/post.jpg", "2")])
    async with get_session_factory()() as session:
        snapshot = await snapshot_user_media(
            session, uuid.UUID(owner.id), reader, None,
            bucket="chirp-media", deferred_cleanup=True,
        )
    reader.objects[f"avatars/{owner.id}/late.jpg"] = "5"
    provider = GCSMediaProvider(
        reader, bucket="chirp-media", retention=reviewed_retention(),
        snapshot=snapshot, freeze=None, deferred_cleanup=True,
    )
    with pytest.raises(MediaProviderUnavailable, match="media_snapshot_drift"):
        await provider.delete_account_data(
            user=user_model(owner.id),
            idempotency_key=f"account-deletion:{uuid.uuid4()}:media_storage",
        )
    assert not any(kind == "delete" for kind, _name in reader.calls)


@pytest.mark.asyncio
async def test_feature_off_refuses_deferred_provider_before_call(client: AsyncClient, make_user) -> None:
    owner, request_id = await _request_and_hold(client, make_user, "Deferred mode remains off")
    providers = provider_set()
    deferred = DeferredProvider("temporary_media")
    providers["media_storage"] = deferred
    async with get_session_factory()() as session:
        request = await session.get(models.AccountDataRequest, request_id)
        user = await session.get(models.User, uuid.UUID(owner.id))
        assert request is not None and user is not None
        result = await run_deletion(session, request_id, user, providers)
        assert result.status == "blocked" and result.failed_step == "media_storage"
        assert deferred.calls == 0
        await session.refresh(user)
        assert user.firebase_uid == owner.firebase_uid and not user.is_ghost


@pytest.mark.asyncio
async def test_obligation_and_tombstone_roll_back_together_after_obligation_flush_failure(
    client: AsyncClient, make_user, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "account_deletion_deferred_cleanup_enabled", True)
    owner, request_id = await _request_and_hold(client, make_user, "Atomic deferred cleanup")
    providers = provider_set()
    providers["media_storage"] = DeferredProvider("temporary_media")

    original_commit = AsyncSession.commit
    original_flush = AsyncSession.flush
    original_add = AsyncSession.add
    injected = False

    def remember_obligation(session: AsyncSession, instance, *args, **kwargs):
        if isinstance(instance, models.AccountPostDeletionCleanup):
            session.info["test_has_deferred_obligation"] = True
        return original_add(session, instance, *args, **kwargs)

    async def fail_after_obligation_flush(session: AsyncSession) -> None:
        nonlocal injected
        if session.info.get("test_has_deferred_obligation") and not injected:
            injected = True
            await original_flush(session)
            raise RuntimeError("synthetic failure after obligation flush")
        await original_commit(session)

    monkeypatch.setattr(AsyncSession, "add", remember_obligation)
    monkeypatch.setattr(AsyncSession, "commit", fail_after_obligation_flush)
    async with get_session_factory()() as session:
        request = await session.get(models.AccountDataRequest, request_id)
        user = await session.get(models.User, uuid.UUID(owner.id))
        assert request is not None and user is not None
        with pytest.raises(RuntimeError, match="after obligation flush"):
            await run_deletion(session, request_id, user, providers)
        await session.rollback()
    assert injected is True

    async with get_session_factory()() as session:
        user = await session.get(models.User, uuid.UUID(owner.id))
        obligations = (await session.scalars(select(models.AccountPostDeletionCleanup)
                                             .where(models.AccountPostDeletionCleanup.request_id == request_id))).all()
        assert user is not None and user.firebase_uid == owner.firebase_uid and not user.is_ghost
        assert obligations == []


@pytest.mark.asyncio
async def test_status_is_category_and_state_only_and_flag_off_performs_no_cleanup_query(
    client: AsyncClient, make_user, monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner: ApiUser = await make_user("Cleanup status owner")
    created = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    request_id = uuid.UUID(created.json()["id"])
    receipt = _receipt("temporary_media", uuid.UUID(owner.id), request_id)
    async with get_session_factory()() as session:
        session.add(models.AccountPostDeletionCleanup(
            request_id=request_id, user_id=uuid.UUID(owner.id), category="temporary_media",
            state="pending", scope=receipt.scope, scope_sha256=receipt.scope_sha256,
            last_pass_digest=receipt.pass_digest, remaining_count=0, attempt_count=0,
            next_attempt_at=datetime.now(timezone.utc),
        ))
        await session.commit()

    monkeypatch.setattr(get_settings(), "account_deletion_deferred_cleanup_enabled", True)
    response = await client.get(f"/me/data-requests/{request_id}", headers=owner.headers)
    assert response.status_code == 200, response.text
    assert response.json()["post_deletion_cleanup"] == [
        {"category": "temporary_media", "state": "pending"},
    ]
    assert "chirp-media" not in response.text and "tmp/" not in response.text

    class NoQuerySession:
        async def execute(self, *_args, **_kwargs):
            raise AssertionError("flag-off path queried the new cleanup table")

    monkeypatch.setattr(get_settings(), "account_deletion_deferred_cleanup_enabled", False)
    assert await _cleanup_statuses(NoQuerySession(), [request_id]) == {}


async def _make_tombstone_obligation(make_user, category: str = "temporary_media"):
    owner: ApiUser = await make_user(f"Deferred worker {uuid.uuid4()}")
    user_id, request_id = uuid.UUID(owner.id), uuid.uuid4()
    receipt = _receipt(category, user_id, request_id)
    async with get_session_factory()() as session:
        user = await session.get(models.User, user_id)
        assert user is not None
        user.is_ghost = True
        user.firebase_uid = f"deleted:{user_id}"
        user.email = f"deleted+{user_id}@invalid.chirp"
        request = models.AccountDataRequest(
            id=request_id, user_id=user_id, kind="deletion", status="completed",
            open_key=None, scope=[], excluded=[], retention_reasons=[], provider_steps={},
            created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
        )
        session.add(request)
        await session.flush()
        row = models.AccountPostDeletionCleanup(
            request_id=request_id, user_id=user_id, category=category, state="pending",
            scope=receipt.scope, scope_sha256=receipt.scope_sha256,
            last_pass_digest=receipt.pass_digest, remaining_count=receipt.remaining_count,
            attempt_count=0, next_attempt_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        session.add(row)
        await session.commit()
    return user_id, request_id, row.id, receipt


@pytest.mark.asyncio
async def test_empty_media_pass_is_rescheduled_and_expired_lease_is_never_stolen(
    make_user, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "account_deletion_deferred_cleanup_enabled", True)
    user_id, _request_id, row_id, receipt = await _make_tombstone_obligation(make_user)
    async with get_session_factory()() as session:
        claim = (await claim_due_cleanup(session, limit=1))[0]
    async with get_session_factory()() as session:
        wrong_owner = replace(claim, owner_token=uuid.uuid4())
        assert not await settle_cleanup_claim(session, wrong_owner, state="uncertain")
    async with get_session_factory()() as session:
        wrong_request = replace(receipt, request_id=str(uuid.uuid4()))
        with pytest.raises(ValueError, match="deferred_cleanup_receipt_invalid"):
            await settle_cleanup_claim(
                session, claim, state="pending", receipt=wrong_request,
                next_attempt_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        await session.rollback()
    async with get_session_factory()() as session:
        await session.execute(update(models.AccountPostDeletionCleanup)
                              .where(models.AccountPostDeletionCleanup.id == row_id)
                              .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(days=1)))
        await session.commit()
    async with get_session_factory()() as session:
        assert await claim_due_cleanup(session, limit=1) == []
        persisted = await session.get(models.AccountPostDeletionCleanup, row_id)
        assert persisted is not None and persisted.state == "running"
        assert persisted.owner_token == claim.owner_token

    _user_id2, _request_id2, row_id2, receipt2 = await _make_tombstone_obligation(make_user)

    async def empty_pass(_claim):
        return receipt2

    counts = await run_post_deletion_cleanup_batch(get_session_factory(), empty_pass, limit=1)
    assert counts == {"claimed": 1, "rescheduled": 1, "completed": 0, "uncertain": 0}
    async with get_session_factory()() as session:
        pending = await session.get(models.AccountPostDeletionCleanup, row_id2)
        assert pending is not None and pending.state == "pending" and pending.remaining_count == 0


@pytest.mark.asyncio
async def test_batch_never_completes_from_generic_drain_claim_and_marks_unknown_permanent(
    make_user, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "account_deletion_deferred_cleanup_enabled", True)
    _user_id, _request_id, row_id, _receipt_value = await _make_tombstone_obligation(make_user)
    proof = CleanupCompletionProof(
        category="temporary_media", request_id=str(_request_id), subject=str(_user_id),
        scope_sha256="a" * 64, pass_digest="b" * 64,
        attempt_token=str(uuid.uuid4()),
        proof_id="gcs_prefix_empty_after_signed_put_drain", verified=True,
        source_drained=True, readback=True,
    )

    async def untrusted_generic_result(_claim):
        return proof

    counts = await run_post_deletion_cleanup_batch(
        get_session_factory(), untrusted_generic_result, limit=1,
    )
    assert counts == {"claimed": 1, "rescheduled": 0, "completed": 0, "uncertain": 1}
    async with get_session_factory()() as session:
        row = await session.get(models.AccountPostDeletionCleanup, row_id)
        assert row is not None and row.state == "uncertain"
        assert row.last_error == "provider_outcome_unknown"
        assert row.owner_token is not None and row.lease_expires_at is None
        assert await claim_due_cleanup(session, limit=1) == []


@pytest.mark.asyncio
async def test_invalid_immutable_scope_is_manual_review_not_provider_work(make_user) -> None:
    _user_id, _request_id, row_id, _receipt_value = await _make_tombstone_obligation(make_user)
    async with get_session_factory()() as session:
        row = await session.get(models.AccountPostDeletionCleanup, row_id)
        assert row is not None
        row.scope = {**row.scope, "unapproved": "scope"}
        row.scope_sha256 = _digest(row.scope)
        await session.commit()
    async with get_session_factory()() as session:
        assert await claim_due_cleanup(session, limit=1) == []
        row = await session.get(models.AccountPostDeletionCleanup, row_id)
        assert row is not None and row.state == "manual_review"
        assert row.last_error == "cleanup_scope_invalid"


@pytest.mark.asyncio
async def test_batch_cancellation_records_uncertain_attempt_and_propagates(make_user, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "account_deletion_deferred_cleanup_enabled", True)
    _user_id, _request_id, row_id, _receipt_value = await _make_tombstone_obligation(make_user)
    started = asyncio.Event()

    async def blocked_provider(_claim):
        started.set()
        await asyncio.Event().wait()

    worker = asyncio.create_task(run_post_deletion_cleanup_batch(
        get_session_factory(), blocked_provider, limit=1,
    ))
    await asyncio.wait_for(started.wait(), timeout=5)
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(worker, timeout=7)
    async with get_session_factory()() as session:
        row = await session.get(models.AccountPostDeletionCleanup, row_id)
        assert row is not None and row.state == "uncertain"
        assert row.owner_token is not None and row.lease_expires_at is None
