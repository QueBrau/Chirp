"""PostgreSQL proof that deletion execution is downstream of hold admission."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text

from app import models
from app.db import get_session_factory
from app.services.account_deletion_hold import activate_deletion_hold, advisory_key
from app.services.account_external_operations import (
    finish_external_operation,
    register_external_operation,
)
from app.services.account_fulfillment import (
    PROVIDER_STEPS, run_deletion, validate_provider_receipt,
)
from tests.c436_executor_helpers import activate_executor_hold
from tests.test_c436_fulfillment import ConfirmingProvider, provider_set


async def _create_request(client, owner) -> uuid.UUID:
    response = await client.post(
        "/me/data-requests", json={"kind": "deletion"}, headers=owner.headers,
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


@pytest.mark.asyncio
async def test_nonterminal_executor_requires_exact_hold_and_drain(client, make_user):
    owner = await make_user("Executor admission owner")
    request_id = await _create_request(client, owner)
    providers = provider_set()

    async with get_session_factory()() as session:
        user = await session.get(models.User, uuid.UUID(owner.id))
        assert user is not None
        no_hold = await run_deletion(session, request_id, user, providers)
        assert no_hold.status == "blocked"
        assert no_hold.failed_step == "deletion_admission"
        assert all(not provider.keys for provider in providers.values())

    handle = await register_external_operation(
        user_id=uuid.UUID(owner.id), firebase_uid=owner.firebase_uid,
        kind="media_finalize_post", effect_key="a" * 64,
    )
    await activate_executor_hold(user_id=uuid.UUID(owner.id), request_id=request_id)
    async with get_session_factory()() as session:
        user = await session.get(models.User, uuid.UUID(owner.id))
        assert user is not None
        running = await run_deletion(session, request_id, user, providers)
        assert running.status == "blocked"
        assert running.failed_step == "registered_external_operations"
        assert all(not provider.keys for provider in providers.values())

    await finish_external_operation(handle, state="uncertain")
    async with get_session_factory()() as session:
        user = await session.get(models.User, uuid.UUID(owner.id))
        assert user is not None
        uncertain = await run_deletion(session, request_id, user, providers)
        assert uncertain.status == "blocked"
        assert uncertain.failed_step == "registered_external_operations"
        assert all(not provider.keys for provider in providers.values())
        await session.refresh(user)
        assert user.is_ghost is False


@pytest.mark.asyncio
async def test_hold_for_another_request_does_not_admit_executor(client, make_user):
    owner = await make_user("Mismatched hold owner")
    held_request_id = await _create_request(client, owner)
    async with get_session_factory()() as session:
        other_request = models.AccountDataRequest(
            user_id=uuid.UUID(owner.id), kind="deletion", status="received",
            open_key=None, scope=[], excluded=[], retention_reasons=[],
            provider_steps={},
        )
        session.add(other_request)
        await session.commit()
        unheld_request_id = other_request.id
    await activate_executor_hold(
        user_id=uuid.UUID(owner.id), request_id=held_request_id,
    )
    providers = provider_set()
    async with get_session_factory()() as session:
        user = await session.get(models.User, uuid.UUID(owner.id))
        assert user is not None
        result = await run_deletion(session, unheld_request_id, user, providers)
        assert result.status == "blocked"
        assert result.failed_step == "deletion_admission"
        assert all(not provider.keys for provider in providers.values())


@pytest.mark.asyncio
async def test_target_scoped_operation_from_another_actor_blocks_executor(client, make_user):
    actor = await make_user("Executor target-operation actor")
    target = await make_user("Executor target-operation owner")
    request_id = await _create_request(client, target)
    handle = await register_external_operation(
        user_id=uuid.UUID(actor.id), firebase_uid=actor.firebase_uid,
        kind="media_finalize_post", effect_key="b" * 64,
        target_user_ids=[uuid.UUID(target.id)],
    )
    await activate_executor_hold(user_id=uuid.UUID(target.id), request_id=request_id)
    providers = provider_set()
    async with get_session_factory()() as session:
        user = await session.get(models.User, uuid.UUID(target.id))
        assert user is not None
        result = await run_deletion(session, request_id, user, providers)
        assert result.status == "blocked"
        assert result.failed_step == "registered_external_operations"
        assert all(not provider.keys for provider in providers.values())
        await session.refresh(user)
        assert user.is_ghost is False
    # The target scope remains until the external owner resolves it.
    await finish_external_operation(handle, state="finished")


@pytest.mark.asyncio
async def test_valid_completed_history_remains_read_only_without_hold(client, make_user):
    owner = await make_user("Historical completed owner")
    request_id = await _create_request(client, owner)
    now = datetime.now(timezone.utc)
    async with get_session_factory()() as seed:
        user = await seed.get(models.User, uuid.UUID(owner.id))
        request = await seed.get(models.AccountDataRequest, request_id)
        assert user is not None and request is not None
        steps = list((await seed.scalars(select(models.AccountFulfillmentStep).where(
            models.AccountFulfillmentStep.request_id == request_id,
        ))).all())
        for step in steps:
            step.status = "succeeded"
            step.completed_at = now
            step.lease_token = None
            step.lease_expires_at = None
            if step.step_key == "database_tombstone":
                step.attempt_count = 1
                continue
            result = await ConfirmingProvider(step.step_key).delete_account_data(
                user=user, idempotency_key=step.idempotency_key,
            )
            retained = validate_provider_receipt(
                step.step_key, user.id, result, expected_request_id=request_id,
            )
            receipt = {
                "confirmed": True, "provider": result["provider"],
                "subject": result["subject"], "scope": result["scope"],
                "readback": True, "retained_with_reason": retained,
            }
            if step.step_key == "analytics":
                receipt.update({key: result[key] for key in (
                    "time_travel_hours", "fail_safe_days", "excluded_scope",
                    "request_id", "snapshot_digest",
                )})
            step.provider_ref = receipt
        request.status = "completed"
        request.completed_at = now
        request.failure_code = None
        request.provider_steps = {step.step_key: "succeeded" for step in steps}
        request.fulfillment_lease_token = None
        request.fulfillment_lease_expires_at = None
        user.is_ghost = True
        user.firebase_uid = f"deleted:{user.id}"
        user.email = f"deleted+{user.id}@invalid.chirp"
        await seed.commit()

    providers = provider_set()
    async with get_session_factory()() as session:
        user = await session.get(models.User, uuid.UUID(owner.id))
        assert user is not None
        result = await run_deletion(session, request_id, user, providers)
        assert result.status == "completed"
        assert all(not provider.keys for provider in providers.values())
        request = await session.get(models.AccountDataRequest, request_id)
        assert request is not None and request.status == "completed"


@pytest.mark.asyncio
async def test_executor_final_gate_serializes_idempotent_hold_activation(
    client, make_user, monkeypatch,
):
    """An activation cannot take user/request locks ahead of final tombstoning."""
    owner = await make_user("Final gate ordering owner")
    request_id = await _create_request(client, owner)
    await activate_executor_hold(user_id=uuid.UUID(owner.id), request_id=request_id)

    # Seed valid immutable receipts so every provider step takes the reused path.
    # The last reuse must commit its request fence before the exclusive final gate.
    async with get_session_factory()() as seed:
        user = await seed.get(models.User, uuid.UUID(owner.id))
        request = await seed.get(models.AccountDataRequest, request_id)
        assert user is not None and request is not None
        steps = list((await seed.scalars(select(models.AccountFulfillmentStep).where(
            models.AccountFulfillmentStep.request_id == request_id,
        ))).all())
        for step in steps:
            if step.step_key == "database_tombstone":
                continue
            provider = ConfirmingProvider(step.step_key)
            result = await provider.delete_account_data(
                user=user, idempotency_key=step.idempotency_key,
            )
            retained = validate_provider_receipt(
                step.step_key, user.id, result, expected_request_id=request_id,
            )
            receipt = {
                "confirmed": True, "provider": result["provider"],
                "subject": result["subject"], "scope": result["scope"],
                "readback": True, "retained_with_reason": retained,
            }
            if step.step_key == "analytics":
                receipt.update({key: result[key] for key in (
                    "time_travel_hours", "fail_safe_days", "excluded_scope",
                    "request_id", "snapshot_digest",
                )})
            step.status = "succeeded"
            step.provider_ref = receipt
            step.completed_at = datetime.now(timezone.utc)
        request.provider_steps = {step.step_key: "succeeded" for step in steps}
        await seed.commit()

    import app.services.account_fulfillment as fulfillment

    cleanup_entered = asyncio.Event()
    release_cleanup = asyncio.Event()
    real_cleanup = fulfillment.cleanup_local_account
    real_final_gate = fulfillment._lock_executor_finalization

    async def prove_request_fence_released(session, user_id, final_request_id):
        # This runs immediately before the final advisory gate. The reused-receipt
        # branch must have committed its last request-fence UPDATE already.
        async with get_session_factory()() as probe:
            await probe.execute(
                select(models.AccountDataRequest.id).where(
                    models.AccountDataRequest.id == final_request_id,
                ).with_for_update(nowait=True)
            )
            await probe.rollback()
        return await real_final_gate(session, user_id, final_request_id)

    async def gated_cleanup(session, user):
        cleanup_entered.set()
        await release_cleanup.wait()
        return await real_cleanup(session, user)

    monkeypatch.setattr(fulfillment, "cleanup_local_account", gated_cleanup)
    monkeypatch.setattr(
        fulfillment, "_lock_executor_finalization", prove_request_fence_released,
    )
    providers = provider_set()
    runner = None
    activator = None
    activation_pid = None
    cleanup_waiter = None
    try:
        async with get_session_factory()() as runner_session:
            user = await runner_session.get(models.User, uuid.UUID(owner.id))
            assert user is not None
            runner = asyncio.create_task(
                run_deletion(runner_session, request_id, user, providers)
            )
            cleanup_waiter = asyncio.create_task(cleanup_entered.wait())
            done, _ = await asyncio.wait(
                (runner, cleanup_waiter), timeout=20,
                return_when=asyncio.FIRST_COMPLETED,
            )
            assert done, "executor never reached the finalization boundary"
            if runner in done:
                await runner
            await cleanup_waiter

            async def activate_again():
                nonlocal activation_pid
                async with get_session_factory()() as activation_session:
                    connection = await activation_session.connection()
                    activation_pid = connection.sync_connection.connection.driver_connection.get_server_pid()
                    await activate_deletion_hold(
                        activation_session, user_id=uuid.UUID(owner.id),
                        request_id=request_id,
                    )
                    await activation_session.commit()

            activator = asyncio.create_task(activate_again())

            # Wait for PostgreSQL to show the activation blocked on the executor's
            # advisory lock. This uses DB state as the synchronization point.
            key_value = advisory_key(uuid.UUID(owner.id)) & ((1 << 64) - 1)
            lock_class, lock_object = key_value >> 32, key_value & 0xFFFFFFFF
            async with get_session_factory()() as observer:
                for _ in range(200):
                    waiters = await observer.scalar(text(
                        "SELECT count(*) FROM pg_locks "
                        "WHERE pid = :pid AND locktype = 'advisory' AND NOT granted "
                        "AND classid = :lock_class AND objid = :lock_object AND objsubid = 1"
                    ), {"pid": activation_pid, "lock_class": lock_class, "lock_object": lock_object})
                    if waiters:
                        break
                    await asyncio.sleep(0.025)
                assert waiters, (
                    "activation never waited on the final advisory gate; "
                    f"activation_done={activator.done()}"
                )

            release_cleanup.set()
            result = await asyncio.wait_for(runner, timeout=20)
            from app.services.account_deletion_hold import DeletionHoldInvalid
            with pytest.raises(DeletionHoldInvalid, match="deletion_request_invalid"):
                await asyncio.wait_for(activator, timeout=20)
            assert result.status == "completed"
            assert all(not provider.keys for provider in providers.values())
    finally:
        release_cleanup.set()
        for task in (runner, activator, cleanup_waiter):
            if task is not None and not task.done():
                task.cancel()
        for task in (runner, activator, cleanup_waiter):
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_dirty_executor_session_fails_closed_without_flushing(client, make_user):
    owner = await make_user("Dirty executor session owner")
    request_id = await _create_request(client, owner)
    await activate_executor_hold(user_id=uuid.UUID(owner.id), request_id=request_id)
    providers = provider_set()
    async with get_session_factory()() as session:
        user = await session.get(models.User, uuid.UUID(owner.id))
        assert user is not None
        original_email = user.email
        user.display_name = "must not flush"
        result = await run_deletion(session, request_id, user, providers)
        assert result.status == "blocked"
        assert result.failed_step == "executor_session_not_clean"
        assert all(not provider.keys for provider in providers.values())
        await session.rollback()
    async with get_session_factory()() as observer:
        stored = await observer.get(models.User, uuid.UUID(owner.id))
        assert stored is not None and stored.email == original_email
        assert stored.display_name != "must not flush"


@pytest.mark.asyncio
async def test_non_read_committed_executor_session_fails_before_provider_io(client, make_user):
    owner = await make_user("Isolation executor owner")
    request_id = await _create_request(client, owner)
    await activate_executor_hold(user_id=uuid.UUID(owner.id), request_id=request_id)
    providers = provider_set()
    async with get_session_factory()() as session:
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        user = await session.get(models.User, uuid.UUID(owner.id))
        assert user is not None
        result = await run_deletion(session, request_id, user, providers)
        assert result.status == "blocked"
        assert result.failed_step == "executor_isolation_invalid"
        assert all(not provider.keys for provider in providers.values())
