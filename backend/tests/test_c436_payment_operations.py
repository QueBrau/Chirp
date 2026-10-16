"""Real-PostgreSQL effect identity, attempt fencing, and payment drain tests."""
from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app import models
from app.db import get_session_factory
from app.services.account_deletion_hold import DeletionHoldActive, activate_deletion_hold
from app.services.account_external_operations import register_external_operation
from app.services.account_payment_operations import (
    MAX_UNRESOLVED_ATTEMPTS,
    PaymentAttemptHandle,
    PaymentOperationConflict,
    PaymentOperationInvalid,
    finish_payment_attempt,
    inspect_payment_operation_drain,
    mark_payment_attempt_uncertain,
    register_payment_attempt,
    register_payment_effect,
)
from tests.test_c436_external_operations_migration import (
    _admin_execute,
    _alembic,
    _database_of,
    _probe,
    _swap_database,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def _parent(user, label: str):
    return await register_external_operation(
        user_id=uuid.UUID(user.id),
        firebase_uid=user.firebase_uid,
        kind="payment_request",
        effect_key=_sha(f"payment-request:{label}:{uuid.uuid4()}"),
    )


async def _effect(parent, label: str, *, kind: str = "payment_intent_create", **kwargs):
    if kind in {
        "connect_account_link", "customer_create", "customer_session_create",
        "payment_intent_create", "payment_intent_cancel",
    }:
        kwargs.setdefault("connected_account_id", "acct_testaccount123")
    if kind in {"payment_intent_create", "payment_intent_cancel"}:
        kwargs.setdefault("reservation_id", uuid.uuid5(uuid.NAMESPACE_URL, label))
    return await register_payment_effect(
        parent,
        kind=kind,
        effect_key=_sha(f"payment-effect:{label}"),
        request_fingerprint=_sha(f"payment-payload:{label}"),
        idempotency_key=f"chirp:{kind}:{_sha(label)[:32]}",
        **kwargs,
    )


async def _counts(effect_id: uuid.UUID) -> dict[str, int]:
    async with get_session_factory()() as session:
        rows = await session.execute(
            select(models.AccountPaymentAttempt.state, func.count(models.AccountPaymentAttempt.id))
            .where(models.AccountPaymentAttempt.effect_id == effect_id)
            .group_by(models.AccountPaymentAttempt.state)
        )
        return {state: int(count) for state, count in rows}


def test_payment_journal_schema_and_representations_exclude_secrets_and_personal_payloads():
    effect_columns = set(models.AccountPaymentEffect.__table__.columns.keys())
    attempt_columns = set(models.AccountPaymentAttempt.__table__.columns.keys())
    forbidden = {
        "url", "client_secret", "email", "display_name", "name",
        "raw_error", "error_message", "provider_response",
    }
    assert not (effect_columns | attempt_columns) & forbidden
    handle = PaymentAttemptHandle(
        attempt_id=uuid.uuid4(),
        effect_id=uuid.uuid4(),
        parent_operation_id=uuid.uuid4(),
        actor_user_id=uuid.uuid4(),
        owner_token=uuid.uuid4(),
    )
    assert str(handle.owner_token) not in repr(handle)


async def _deletion_request(client, user) -> uuid.UUID:
    response = await client.post(
        "/me/data-requests", json={"kind": "deletion"}, headers=user.headers,
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


async def _activate(user_id: uuid.UUID, request_id: uuid.UUID) -> None:
    async with get_session_factory()() as session:
        await activate_deletion_hold(
            session, user_id=user_id, request_id=request_id,
        )
        await session.commit()


@pytest.mark.asyncio
async def test_effect_identity_is_global_immutable_and_shared_across_connect_officers(client, make_user):
    officer_a = await make_user("Shared payment effect officer A")
    officer_b = await make_user("Shared payment effect officer B")
    parent_a = await _parent(officer_a, "shared-connect-A")
    parent_b = await _parent(officer_b, "shared-connect-B")
    key = _sha("connect-account-for-chapter-identity")
    fingerprint = _sha("chapter-id-and-authorized-org-name")

    async def register(parent, idem="chirp:connect:create:stable"):
        return await register_payment_effect(
            parent,
            kind="connect_account_create",
            effect_key=key,
            request_fingerprint=fingerprint,
            idempotency_key=idem,
        )

    effect_a, effect_b = await asyncio.gather(register(parent_a), register(parent_b))
    assert effect_a.effect_id == effect_b.effect_id
    attempt_a = await register_payment_attempt(parent_a, effect_a)
    with pytest.raises(PaymentOperationConflict, match="payment_attempt_already_running"):
        await register_payment_attempt(parent_b, effect_b)
    await mark_payment_attempt_uncertain(parent_a, effect_a, attempt_a)
    attempt_b = await register_payment_attempt(parent_b, effect_b)
    async with get_session_factory()() as session:
        await finish_payment_attempt(
            session, parent_b, effect_b, attempt_b,
            provider_id="acct_sharedconnect123",
        )
        await session.commit()
    assert await _counts(effect_a.effect_id) == {"finished": 1, "reconciled": 1}

    with pytest.raises(PaymentOperationConflict, match="payment_effect_binding_mismatch"):
        await register_payment_effect(
            parent_b,
            kind="connect_account_create",
            effect_key=key,
            request_fingerprint=_sha("different-org-name"),
            idempotency_key="chirp:connect:create:stable",
        )
    with pytest.raises(PaymentOperationConflict, match="payment_effect_binding_mismatch"):
        await register(parent_b, idem="chirp:connect:create:different")


@pytest.mark.asyncio
async def test_attempt_owner_parent_and_provider_binding_are_fenced(client, make_user):
    owner = await make_user("Payment attempt owner")
    other = await make_user("Payment attempt other actor")
    parent = await _parent(owner, "attempt-fence")
    other_parent = await _parent(other, "attempt-fence-other")
    effect = await _effect(parent, "attempt-fence")
    attempt = await register_payment_attempt(parent, effect)

    with pytest.raises(PaymentOperationInvalid, match="payment_attempt_fence_invalid"):
        async with get_session_factory()() as session:
            await finish_payment_attempt(
                session, other_parent, effect,
                replace(attempt, parent_operation_id=other_parent.operation_id,
                        actor_user_id=other_parent.actor_user_id),
                provider_id="pi_wrongparent123",
            )
    with pytest.raises(PaymentOperationInvalid, match="payment_attempt_fence_invalid"):
        async with get_session_factory()() as session:
            await finish_payment_attempt(
                session, parent, effect,
                replace(attempt, owner_token=uuid.uuid4()),
                provider_id="pi_wrongtoken123",
            )
    for forged_parent in (
        replace(parent, owner_token=uuid.uuid4()),
        replace(parent, effect_key=_sha("forged-parent-effect")),
        replace(parent, user_ids=()),
    ):
        with pytest.raises(PaymentOperationInvalid):
            await mark_payment_attempt_uncertain(forged_parent, effect, attempt)
    assert await _counts(effect.effect_id) == {"running": 1}

    async with get_session_factory()() as session:
        await finish_payment_attempt(
            session, parent, effect, attempt, provider_id="pi_goodprovider123"
        )
        # Completion is flushed in this transaction, not committed by the helper.
        await session.rollback()
    assert await _counts(effect.effect_id) == {"running": 1}
    async with get_session_factory()() as session:
        row = await session.get(models.AccountPaymentEffect, effect.effect_id)
        assert row.provider_object_id is None

    async with get_session_factory()() as session:
        await finish_payment_attempt(
            session, parent, effect, attempt, provider_id="pi_goodprovider123"
        )
        await session.commit()
    assert await _counts(effect.effect_id) == {"finished": 1}
    with pytest.raises(PaymentOperationConflict, match="payment_effect_already_confirmed"):
        await register_payment_attempt(parent, effect)


@pytest.mark.asyncio
async def test_cancel_effect_can_reobserve_same_intent_without_changing_its_binding(client, make_user):
    owner = await make_user("Retryable cancellation owner")
    parent = await _parent(owner, "cancel-retry")
    effect = await _effect(parent, "cancel-same-intent", kind="payment_intent_cancel")
    first = await register_payment_attempt(parent, effect)
    async with get_session_factory()() as session:
        await finish_payment_attempt(
            session, parent, effect, first, provider_id="pi_sameintent123"
        )
        await session.commit()

    second = await register_payment_attempt(parent, effect)
    with pytest.raises(PaymentOperationConflict, match="payment_provider_binding_mismatch"):
        async with get_session_factory()() as session:
            await finish_payment_attempt(
                session, parent, effect, second, provider_id="pi_otherintent123"
            )
    async with get_session_factory()() as session:
        await finish_payment_attempt(
            session, parent, effect, second, provider_id="pi_sameintent123"
        )
        await session.commit()
    assert await _counts(effect.effect_id) == {"finished": 2}


@pytest.mark.asyncio
async def test_admitted_parent_can_register_finish_during_hold_but_new_parent_is_refused(client, make_user):
    owner = await make_user("Payment hold owner")
    parent = await _parent(owner, "hold-admitted-before")
    request_id = await _deletion_request(client, owner)
    await _activate(uuid.UUID(owner.id), request_id)

    effect = await _effect(parent, "hold-child-effect", kind="customer_session_create")
    attempt = await register_payment_attempt(parent, effect)
    async with get_session_factory()() as session:
        await finish_payment_attempt(session, parent, effect, attempt)
        await session.commit()

    async with get_session_factory()() as session:
        drain = await inspect_payment_operation_drain(
            session=session,
            user_id=uuid.UUID(owner.id),
        )
    assert drain == {"running": 0, "uncertain": 0}
    with pytest.raises(DeletionHoldActive):
        await _parent(owner, "new-parent-after-hold")


@pytest.mark.asyncio
async def test_only_one_running_attempt_per_effect_and_uncertain_attempts_are_bounded(client, make_user):
    owner = await make_user("Concurrent payment attempt owner")
    parent = await _parent(owner, "single-runner")
    effect = await _effect(parent, "single-running-effect", kind="customer_create")

    outcomes = await asyncio.gather(
        register_payment_attempt(parent, effect),
        register_payment_attempt(parent, effect),
        return_exceptions=True,
    )
    handles = [value for value in outcomes if not isinstance(value, BaseException)]
    conflicts = [value for value in outcomes if isinstance(value, PaymentOperationConflict)]
    assert len(handles) == len(conflicts) == 1

    await mark_payment_attempt_uncertain(parent, effect, handles[0])
    for _ in range(MAX_UNRESOLVED_ATTEMPTS - 1):
        next_attempt = await register_payment_attempt(parent, effect)
        await mark_payment_attempt_uncertain(parent, effect, next_attempt)
    with pytest.raises(PaymentOperationConflict, match="payment_attempt_review_required"):
        await register_payment_attempt(parent, effect)
    assert await _counts(effect.effect_id) == {"uncertain": MAX_UNRESOLVED_ATTEMPTS}


@pytest.mark.asyncio
async def test_account_customer_create_retry_stops_before_stripe_idempotency_horizon(client, make_user):
    owner = await make_user("Old payment retry owner")
    parent = await _parent(owner, "create-retry-window")
    effect = await _effect(parent, "create-retry-window", kind="customer_create")
    attempt = await register_payment_attempt(parent, effect)
    await mark_payment_attempt_uncertain(parent, effect, attempt)
    async with get_session_factory()() as session:
        row = await session.get(models.AccountPaymentAttempt, attempt.attempt_id)
        row.created_at -= timedelta(days=800)
        await session.commit()
    with pytest.raises(PaymentOperationConflict, match="payment_create_retry_window_expired"):
        await register_payment_attempt(parent, effect)


async def _migration_rows(url: str, query: str, params: dict | None = None):
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            return (await connection.execute(text(query), params or {})).all()
    finally:
        await engine.dispose()


async def _migration_execute(url: str, statements: list[tuple[str, dict]]):
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            for statement, params in statements:
                await connection.execute(text(statement), params)
    finally:
        await engine.dispose()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_0049_payment_journal_migration_preserves_rows_and_refuses_lossy_downgrade():
    requested = os.environ.get(
        "TEST_DATABASE_URL", "postgresql+asyncpg://chirp:chirp@localhost:5432/chirp_test"
    )
    admin_url = _swap_database(requested, "postgres")
    db_name = f"{_database_of(requested)}_c436pay_{uuid.uuid4().hex[:8]}"
    url = _swap_database(requested, db_name)
    try:
        asyncio.run(_probe(admin_url))
    except Exception:
        pytest.skip("local PostgreSQL required")

    original_database_url = os.environ.get("DATABASE_URL")
    asyncio.run(_admin_execute(admin_url, [
        f'CREATE DATABASE "{db_name}" ENCODING \'UTF8\' TEMPLATE template0',
    ]))
    actor_id, prior_op_id, payment_op_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    prior_key, payment_key = _sha("prior-email-op"), _sha("payment-parent")
    prior_token, child_token = uuid.uuid4(), uuid.uuid4()
    try:
        _alembic(url, "0048")
        asyncio.run(_migration_execute(url, [
            ("INSERT INTO users (id, firebase_uid, email, display_name, account_type) "
             "VALUES (:id, :uid, :email, 'Payment migration user', 'greek')",
             {"id": actor_id, "uid": f"uid-{actor_id}", "email": f"{actor_id}@example.edu"}),
            ("INSERT INTO account_external_operations "
             "(id, actor_user_id, kind, effect_key, owner_token, state) "
             "VALUES (:id, :actor, 'campus_verification_email', :effect, :token, 'running')",
             {"id": prior_op_id, "actor": actor_id, "effect": prior_key, "token": prior_token}),
        ]))
        prior_query = (
            "SELECT id, actor_user_id, kind, effect_key, owner_token, state "
            "FROM account_external_operations WHERE id = :id"
        )
        prior_rows = asyncio.run(_migration_rows(url, prior_query, {"id": prior_op_id}))

        _alembic(url, "0049")
        effect_id, attempt_id = uuid.uuid4(), uuid.uuid4()
        effect_key, fingerprint = _sha("migrated-effect"), _sha("payload")
        asyncio.run(_migration_execute(url, [
            ("INSERT INTO account_external_operations "
             "(id, actor_user_id, kind, effect_key, owner_token, state) "
             "VALUES (:id, :actor, 'payment_request', :effect, :token, 'running')",
             {"id": payment_op_id, "actor": actor_id, "effect": payment_key, "token": child_token}),
            ("INSERT INTO account_payment_effects "
             "(id, kind, effect_key, request_fingerprint, idempotency_key, connected_account_id) "
             "VALUES (:id, 'customer_create', :effect, :fingerprint, "
             "'chirp:customer:create:test', 'acct_testaccount123')",
             {"id": effect_id, "effect": effect_key, "fingerprint": fingerprint}),
            ("INSERT INTO account_payment_attempts "
             "(id, effect_id, parent_operation_id, actor_user_id, owner_token, state, completed_at) "
             "VALUES (:id, :effect_id, :parent, :actor, :token, 'uncertain', now())",
             {"id": attempt_id, "effect_id": effect_id, "parent": payment_op_id,
              "actor": actor_id, "token": child_token}),
        ]))
        assert asyncio.run(_migration_rows(
            url,
            "SELECT id, effect_id, parent_operation_id, actor_user_id, state "
            "FROM account_payment_attempts",
        )) == [(attempt_id, effect_id, payment_op_id, actor_id, "uncertain")]

        with pytest.raises(Exception, match="account_payment_operations_not_empty"):
            _alembic(url, "0048", down=True)
        assert asyncio.run(_migration_rows(url, "SELECT version_num FROM alembic_version")) == [("0049",)]
        assert asyncio.run(_migration_rows(url, prior_query, {"id": prior_op_id})) == prior_rows

        asyncio.run(_migration_execute(url, [
            ("DELETE FROM account_payment_attempts", {}),
            ("DELETE FROM account_payment_effects", {}),
            ("DELETE FROM account_external_operations WHERE id = :id", {"id": payment_op_id}),
        ]))
        _alembic(url, "0048", down=True)
        assert asyncio.run(_migration_rows(url, "SELECT version_num FROM alembic_version")) == [("0048",)]
        assert asyncio.run(_migration_rows(url, prior_query, {"id": prior_op_id})) == prior_rows
        _alembic(url, "0049")
        assert asyncio.run(_migration_rows(url, "SELECT count(*) FROM account_payment_effects")) == [(0,)]
        assert asyncio.run(_migration_rows(url, "SELECT version_num FROM alembic_version")) == [("0049",)]
    finally:
        if original_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original_database_url
        from app.config import get_settings
        get_settings.cache_clear()
        asyncio.run(_admin_execute(admin_url, [f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)']))

