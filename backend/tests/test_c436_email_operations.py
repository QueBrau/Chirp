"""Real-PostgreSQL tests for the campus verification email operation boundary."""
from __future__ import annotations

import asyncio
import hashlib
import re
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text

from app import models
from app.config import get_settings
from app.db import get_engine, get_session_factory
from app.services import campus_verification, email_service
from app.services.account_deletion_hold import activate_deletion_hold
from app.services.account_email_operations import send_account_verification_email
from app.services.account_external_operations import inspect_registered_operation_drain
from tests.conftest import MakeCampus, MakeUser


@pytest.fixture(autouse=True)
def _clear_settings():
    yield
    get_settings.cache_clear()


def _admission(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setenv("ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED", str(enabled).lower())
    get_settings.cache_clear()


def _effect_key(verification_id: uuid.UUID) -> str:
    return hashlib.sha256(str(verification_id).encode("ascii")).hexdigest()


async def _hold(user_id: uuid.UUID) -> uuid.UUID:
    async with get_session_factory()() as session:
        request = models.AccountDataRequest(
            user_id=user_id, kind="deletion", open_key=f"email-test-{uuid.uuid4()}"
        )
        session.add(request)
        await session.flush()
        request_id = request.id
        await session.commit()
    async with get_session_factory()() as session:
        await activate_deletion_hold(session, user_id=user_id, request_id=request_id)
        await session.commit()
    return request_id


async def _operation(user_id: uuid.UUID, verification_id: uuid.UUID):
    async with get_session_factory()() as session:
        return await session.scalar(select(models.AccountExternalOperation).where(
            models.AccountExternalOperation.actor_user_id == user_id,
            models.AccountExternalOperation.kind == "campus_verification_email",
            models.AccountExternalOperation.effect_key == _effect_key(verification_id),
        ))


async def test_registration_commits_before_email_send_and_drain_sees_inflight_work(
    client, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _admission(monkeypatch, True)
    owner = await make_user("Email operation owner")
    user_id = uuid.UUID(owner.id)
    verification_id = uuid.uuid4()
    entered, release = asyncio.Event(), asyncio.Event()
    observed: dict[str, object] = {}

    async def send() -> str:
        observed["checked_out"] = get_engine().sync_engine.pool.checkedout()
        row = await _operation(user_id, verification_id)
        observed["row_state"] = None if row is None else row.state
        entered.set()
        await release.wait()
        return "provider-accepted-reference"

    task = asyncio.create_task(send_account_verification_email(
        user_id, owner.firebase_uid, verification_id, send=send,
    ))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        request_id = await _hold(user_id)
        drain = await inspect_registered_operation_drain(user_id=user_id, request_id=request_id)
        assert (drain.running_count, drain.uncertain_count) == (1, 0)
        assert not drain.registered_operations_drained
        assert observed == {"checked_out": 0, "row_state": "running"}
    finally:
        release.set()
    assert await asyncio.wait_for(task, 5) == "provider-accepted-reference"
    row = await _operation(user_id, verification_id)
    assert row is not None and row.state == "finished"
    # A successful provider return means acceptance by the local provider API;
    # it does not claim delivery to the recipient's inbox.
    assert row.effect_key == _effect_key(verification_id)
    assert "provider-accepted-reference" not in repr(row)
    drain = await inspect_registered_operation_drain(user_id=user_id, request_id=request_id)
    assert drain.registered_operations_drained


@pytest.mark.asyncio
async def test_hold_before_registration_refuses_without_send(client, make_user, monkeypatch):
    _admission(monkeypatch, True)
    owner = await make_user("Held email operation owner")
    user_id = uuid.UUID(owner.id)
    request_id = await _hold(user_id)
    calls = 0

    async def send() -> str:
        nonlocal calls
        calls += 1
        return "should-not-send"

    with pytest.raises(Exception) as caught:
        await send_account_verification_email(
            user_id, owner.firebase_uid, uuid.uuid4(), send=send,
        )
    from app.services.account_deletion_hold import DeletionHoldActive
    assert isinstance(caught.value, DeletionHoldActive)
    assert calls == 0
    drain = await inspect_registered_operation_drain(user_id=user_id, request_id=request_id)
    assert drain.registered_operations_drained


@pytest.mark.asyncio
async def test_registration_activation_race_serializes_then_drain_catches_send(
    client, make_user, monkeypatch,
):
    """Pause at the real INSERT after admission, then queue hold activation."""
    _admission(monkeypatch, True)
    owner = await make_user("Email admission barrier owner")
    user_id = uuid.UUID(owner.id)
    verification_id = uuid.uuid4()
    engine = get_engine()
    barrier_key = int.from_bytes(uuid.uuid4().bytes[:8], "big", signed=True)
    barrier_connection = await engine.connect()
    barrier_transaction = await barrier_connection.begin()
    await barrier_connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": barrier_key})
    async with engine.begin() as connection:
        await connection.execute(text(f"""
            CREATE FUNCTION public.test_pause_campus_email_operation_insert() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN
                PERFORM pg_advisory_xact_lock({barrier_key});
                RETURN NEW;
            END $$
        """))
        await connection.execute(text("""
            CREATE TRIGGER test_pause_campus_email_operation_insert
            BEFORE INSERT ON account_external_operations
            FOR EACH ROW WHEN (NEW.kind = 'campus_verification_email')
            EXECUTE FUNCTION public.test_pause_campus_email_operation_insert()
        """))

    send_started, release_send = asyncio.Event(), asyncio.Event()

    async def send() -> str:
        send_started.set()
        await release_send.wait()
        return "accepted"

    operation_task = asyncio.create_task(send_account_verification_email(
        user_id, owner.firebase_uid, verification_id, send=send,
    ))
    hold_task = None
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10
        insert_waiting = False
        while loop.time() < deadline:
            async with engine.connect() as connection:
                insert_waiting = bool(await connection.scalar(text(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                    "WHERE datname = current_database() AND lower(wait_event) = 'advisory' "
                    "AND query ILIKE '%INSERT INTO account_external_operations%')"
                )))
            if insert_waiting:
                break
            await asyncio.sleep(.02)
        assert insert_waiting, "email operation never reached the post-admission INSERT barrier"

        hold_task = asyncio.create_task(_hold(user_id))
        deadline = loop.time() + 10
        hold_waiting = False
        while loop.time() < deadline:
            async with engine.connect() as connection:
                hold_waiting = bool(await connection.scalar(text(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                    "WHERE datname = current_database() AND lower(wait_event) = 'advisory' "
                    "AND query ILIKE '%pg_advisory_xact_lock%' "
                    "AND query NOT ILIKE '%INSERT INTO account_external_operations%')"
                )))
            if hold_waiting:
                break
            await asyncio.sleep(.02)
        assert hold_waiting, "hold activation did not queue behind the registration's shared gate"

        await barrier_transaction.rollback()
        request_id = await asyncio.wait_for(hold_task, 10)
        await asyncio.wait_for(send_started.wait(), 5)
        drain = await inspect_registered_operation_drain(user_id=user_id, request_id=request_id)
        assert drain.running_count == 1 and not drain.registered_operations_drained
        row = await _operation(user_id, verification_id)
        assert row is not None and row.state == "running"
    finally:
        release_send.set()
        if barrier_transaction.is_active:
            await barrier_transaction.rollback()
        await barrier_connection.close()
        tasks = [task for task in (operation_task, hold_task) if task is not None]
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 10)
            except asyncio.TimeoutError:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        async with engine.begin() as connection:
            await connection.execute(text(
                "DROP TRIGGER IF EXISTS test_pause_campus_email_operation_insert ON account_external_operations"
            ))
            await connection.execute(text("DROP FUNCTION IF EXISTS public.test_pause_campus_email_operation_insert()"))
    assert operation_task.result() == "accepted"


@pytest.mark.asyncio
async def test_duplicate_challenge_and_identity_drift_never_send_again(client, make_user, monkeypatch):
    _admission(monkeypatch, True)
    from app.services.account_external_operations import ExternalOperationConflict

    owner = await make_user("Duplicate email operation owner")
    verification_id = uuid.uuid4()
    calls = 0

    async def send() -> str:
        nonlocal calls
        calls += 1
        return "accepted"

    assert await send_account_verification_email(
        uuid.UUID(owner.id), owner.firebase_uid, verification_id, send=send,
    ) == "accepted"
    with pytest.raises(HTTPException) as caught:
        await send_account_verification_email(
            uuid.UUID(owner.id), owner.firebase_uid, verification_id, send=send,
        )
    assert caught.value.status_code == 409
    assert calls == 1
    assert isinstance(await _operation(uuid.UUID(owner.id), verification_id), models.AccountExternalOperation)

    drifted_challenge_id = uuid.uuid4()
    from app.services.account_deletion_hold import DeletionHoldInvalid
    with pytest.raises(DeletionHoldInvalid, match="write_guard_identity_changed"):
        await send_account_verification_email(
            uuid.UUID(owner.id), "not-the-owner-firebase-uid", drifted_challenge_id,
            send=send,
        )
    assert calls == 1
    assert await _operation(uuid.UUID(owner.id), drifted_challenge_id) is None

    # Registry-level retries are also fenced, including after completion.
    from app.services.account_external_operations import register_external_operation
    with pytest.raises(ExternalOperationConflict):
        await register_external_operation(
            user_id=uuid.UUID(owner.id), firebase_uid=owner.firebase_uid,
            kind="campus_verification_email", effect_key=_effect_key(verification_id),
        )


@pytest.mark.asyncio
async def test_provider_exception_and_repeated_cancellation_leave_non_drained_evidence(
    client, make_user, monkeypatch,
):
    _admission(monkeypatch, True)
    owner = await make_user("Uncertain email operation owner")
    user_id = uuid.UUID(owner.id)
    verification_id = uuid.uuid4()
    entered, release = asyncio.Event(), asyncio.Event()

    async def fail() -> str:
        entered.set()
        await release.wait()
        raise RuntimeError("provider accepted then disconnected")

    task = asyncio.create_task(send_account_verification_email(
        user_id, owner.firebase_uid, verification_id, send=fail,
    ))
    await asyncio.wait_for(entered.wait(), 5)
    request_id = await _hold(user_id)
    release.set()
    with pytest.raises(RuntimeError):
        await task
    row = await _operation(user_id, verification_id)
    assert row is not None and row.state == "uncertain"
    drain = await inspect_registered_operation_drain(user_id=user_id, request_id=request_id)
    assert drain.uncertain_count == 1 and not drain.registered_operations_drained

    # A fresh challenge models a provider coroutine canceled while in flight.
    canceled_id = uuid.uuid4()
    started, release_finish = asyncio.Event(), asyncio.Event()
    from app.services import account_email_operations as wrapper
    from app.services.account_external_operations import finish_external_operation as real_finish

    async def slow_finish(handle, *, state):
        if state == "uncertain":
            started.set()
            await release_finish.wait()
        await real_finish(handle, state=state)

    monkeypatch.setattr(wrapper, "finish_external_operation", slow_finish)

    provider_entered = asyncio.Event()

    async def blocked_send() -> str:
        provider_entered.set()
        await asyncio.Event().wait()
        return "unreachable"

    # A deletion hold already exists for the first actor, so use a fresh actor
    # to prove admission first and hold activation second for cancellation.
    second_owner = await make_user("Canceled email operation owner")
    second_id = uuid.UUID(second_owner.id)
    canceled_task = asyncio.create_task(send_account_verification_email(
        second_id, second_owner.firebase_uid, canceled_id, send=blocked_send,
    ))
    canceled_hold_id = None
    try:
        await asyncio.wait_for(provider_entered.wait(), 5)
        canceled_hold_id = await _hold(second_id)
        canceled_task.cancel()
        await asyncio.wait_for(started.wait(), 5)
        canceled_task.cancel()
        release_finish.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(canceled_task, 5)
    finally:
        release_finish.set()
        if not canceled_task.done():
            canceled_task.cancel()
        try:
            await asyncio.wait_for(canceled_task, 5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    assert canceled_hold_id is not None
    canceled_row = await _operation(second_id, canceled_id)
    assert canceled_row is not None and canceled_row.state == "uncertain"
    drain = await inspect_registered_operation_drain(user_id=second_id, request_id=canceled_hold_id)
    assert drain.uncertain_count == 1 and not drain.registered_operations_drained


@pytest.mark.asyncio
async def test_failed_finished_transition_keeps_running_registration_blocking_drain(
    client, make_user, monkeypatch,
):
    _admission(monkeypatch, True)
    owner = await make_user("Completion failure email owner")
    user_id = uuid.UUID(owner.id)
    verification_id = uuid.uuid4()
    from app.services import account_email_operations as wrapper

    async def fail_finished(handle, *, state):
        assert state == "finished"
        raise RuntimeError("journal completion unavailable")

    entered, release = asyncio.Event(), asyncio.Event()

    async def send() -> str:
        entered.set()
        await release.wait()
        return "provider-accepted"

    task = asyncio.create_task(send_account_verification_email(
        user_id, owner.firebase_uid, verification_id, send=send,
    ))
    await asyncio.wait_for(entered.wait(), 5)
    request_id = await _hold(user_id)
    monkeypatch.setattr(wrapper, "finish_external_operation", fail_finished)
    release.set()
    with pytest.raises(RuntimeError, match="journal completion unavailable"):
        await task
    row = await _operation(user_id, verification_id)
    assert row is not None and row.state == "running"
    drain = await inspect_registered_operation_drain(user_id=user_id, request_id=request_id)
    assert drain.running_count == 1 and not drain.registered_operations_drained


async def _resolved(value: str) -> str:
    return value


async def _set_domains(campus_id: str) -> None:
    async with get_session_factory()() as session:
        await session.execute(text(
            "UPDATE campuses SET email_domains = ARRAY['c436test.edu'] WHERE id = :id"
        ), {"id": campus_id})
        await session.commit()


@pytest.mark.asyncio
async def test_actual_verification_route_keeps_plaintext_out_of_registry_and_flag_off_works(
    client, make_user: MakeUser, make_campus: MakeCampus,
    monkeypatch: pytest.MonkeyPatch,
):
    owner = await make_user("Campus email route actor")
    campus_id = await make_campus()
    await _set_domains(campus_id)
    sent: list[dict[str, str]] = []

    async def fake_send(*, to: str, subject: str, html: str, text_body: str | None = None, text: str | None = None):
        sent.append({"to": to, "subject": subject, "html": html, "text": text or text_body or ""})
        return "email-acceptance-reference"

    monkeypatch.setattr(email_service, "send_email", fake_send)
    monkeypatch.setattr(campus_verification.email_service, "send_email", fake_send)
    _admission(monkeypatch, False)
    off = await client.post(
        "/auth/campus-verification", headers=owner.headers,
        json={"edu_email": "student@c436test.edu"},
    )
    assert off.status_code == 202, off.text
    async with get_session_factory()() as session:
        count = await session.scalar(select(func.count()).select_from(models.AccountExternalOperation).where(
            models.AccountExternalOperation.actor_user_id == uuid.UUID(owner.id),
            models.AccountExternalOperation.kind == "campus_verification_email",
        ))
    assert count == 0

    _admission(monkeypatch, True)
    on = await client.post(
        "/auth/campus-verification", headers=owner.headers,
        json={"edu_email": "student@c436test.edu"},
    )
    assert on.status_code == 202, on.text
    async with get_session_factory()() as session:
        operation = await session.scalar(select(models.AccountExternalOperation).where(
            models.AccountExternalOperation.actor_user_id == uuid.UUID(owner.id),
            models.AccountExternalOperation.kind == "campus_verification_email",
        ).order_by(models.AccountExternalOperation.created_at.desc()))
        verification = await session.scalar(select(models.CampusVerification).where(
            models.CampusVerification.user_id == uuid.UUID(owner.id),
        ).order_by(models.CampusVerification.sent_at.desc()))
        assert operation is not None and verification is not None
        assert operation.state == "finished"
        assert operation.effect_key == _effect_key(verification.id)
        assert operation.effect_key not in verification.code_hash
        persisted_values = " ".join(str(getattr(operation, name)) for name in (
            "id", "actor_user_id", "kind", "effect_key", "owner_token", "state",
            "created_at", "completed_at",
        ))
        assert verification.edu_email not in persisted_values
        assert sent[-1]["to"] == "student@c436test.edu"
        assert "email-acceptance-reference" not in persisted_values
        assert sent[-1]["to"] not in persisted_values
        code_match = re.search(r"\b(\d{6})\b", sent[-1]["html"])
        assert code_match is not None
        assert code_match.group(1) not in persisted_values
        assert sent[-1]["html"] not in persisted_values
        assert not hasattr(operation, "provider_ref")


@pytest.mark.asyncio
async def test_actual_route_with_existing_hold_never_invokes_email_provider(
    client, make_user: MakeUser, make_campus: MakeCampus,
    monkeypatch: pytest.MonkeyPatch,
):
    _admission(monkeypatch, True)
    owner = await make_user("Held campus email route actor")
    campus_id = await make_campus()
    await _set_domains(campus_id)
    await _hold(uuid.UUID(owner.id))
    sends: list[object] = []

    async def fake_send(**kwargs):
        sends.append(kwargs)
        return "unexpected"

    monkeypatch.setattr(email_service, "send_email", fake_send)
    monkeypatch.setattr(campus_verification.email_service, "send_email", fake_send)
    response = await client.post(
        "/auth/campus-verification", headers=owner.headers,
        json={"edu_email": "student@c436test.edu"},
    )
    assert response.status_code == 423, response.text
    assert sends == []
    async with get_session_factory()() as session:
        count = await session.scalar(select(func.count()).select_from(models.AccountExternalOperation).where(
            models.AccountExternalOperation.actor_user_id == uuid.UUID(owner.id),
            models.AccountExternalOperation.kind == "campus_verification_email",
        ))
    assert count == 0
