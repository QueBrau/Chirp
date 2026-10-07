"""Focused c436 safety tests with synthetic database fixtures and no providers."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from tests.conftest import ApiUser

from app import models
from app.services import account_data
from app.db import get_session_factory
from app.jobs.account_data import main as expiry_main
from app.jobs.account_data import run_export_artifact_expiry
import app.jobs.account_data as expiry_job


def test_unknown_account_edge_does_not_silently_disappear() -> None:
    with pytest.raises(RuntimeError, match="unclassified_account_data_edge"):
        # The check happens before any session query, so this synthetic session is
        # enough to prove a schema mapping typo cannot become an empty export.
        import asyncio
        asyncio.run(account_data._rows(object(), models.User, "not_a_user_column", uuid.uuid4()))


@pytest.mark.asyncio
async def test_blocked_deletion_does_not_tombstone_account() -> None:
    user = models.User(
        id=uuid.uuid4(), firebase_uid="live-uid", email="person@example.test",
        display_name="Person", account_type="non_greek", pseudonym_seed="seed",
    )
    request = models.AccountDataRequest(user_id=user.id, kind="deletion", open_key="open")
    result = await account_data.fulfill_request(object(), request, user)
    assert result.status == "blocked"
    assert result.failure_code == "manual_processing_required"
    assert user.firebase_uid == "live-uid"
    assert user.email == "person@example.test"


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_request_status_is_owner_scoped_and_manual_processing_is_idempotently_reused(
    client: AsyncClient, make_user
) -> None:
    owner: ApiUser = await make_user("Owner")
    other: ApiUser = await make_user("Other")
    created = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "blocked"
    request_id = body["id"]

    assert (await client.get(f"/me/data-requests/{request_id}", headers=other.headers)).status_code == 404
    own = await client.get(f"/me/data-requests/{request_id}", headers=owner.headers)
    assert own.status_code == 200
    assert own.json()["failure_code"] == "manual_processing_required"
    duplicate = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    assert duplicate.status_code == 201
    assert duplicate.json()["id"] == request_id
    # Manual processing leaves the account available for status inspection.
    assert (await client.get("/auth/me", headers=owner.headers)).status_code == 200
    async with get_session_factory()() as session:
        user = await session.get(models.User, uuid.UUID(owner.id))
        user.suspended_at = datetime.now(timezone.utc)
        await session.commit()
    assert (await client.get(f"/me/data-requests/{request_id}", headers=owner.headers)).status_code == 200


@pytest.mark.asyncio
async def test_export_is_immutable_authenticated_and_explicitly_partial(
    client: AsyncClient, make_user, make_campus
) -> None:
    owner: ApiUser = await make_user("Export owner")
    other: ApiUser = await make_user("Other account")
    now = datetime.now(timezone.utc)
    async with get_session_factory()() as session:
        owner_id, other_id = uuid.UUID(owner.id), uuid.UUID(other.id)
        chapter = models.Chapter(campus_id=uuid.UUID(await make_campus()), org_name="Authority test")
        session.add(chapter)
        await session.flush()
        owner_membership = models.Membership(user_id=owner_id, chapter_id=chapter.id, role="president", status="active")
        other_membership = models.Membership(user_id=other_id, chapter_id=chapter.id, role="treasurer", status="active")
        session.add_all([owner_membership, other_membership])
        await session.flush()
        owner_term = models.RoleTerm(membership_id=owner_membership.id, role="president", started_at=now)
        other_term = models.RoleTerm(membership_id=other_membership.id, role="treasurer", started_at=now)
        session.add_all([owner_term, other_term])
        await session.flush()
        session.add_all([
            models.OrganizationAuthorityAcceptance(
                user_id=owner_id, chapter_id=chapter.id, membership_id=owner_membership.id,
                role_term_id=owner_term.id, role="president", purpose="organization_create",
                policy_version="2026-10-06", stripe_account_id=None,
            ),
            models.OrganizationAuthorityAcceptance(
                user_id=other_id, chapter_id=chapter.id, membership_id=other_membership.id,
                role_term_id=other_term.id, role="treasurer", purpose="payment_setup",
                policy_version="2026-10-06", stripe_account_id="acct_other",
            ),
        ])
        owner_device = models.Device(user_id=owner_id, registration_id=1, identity_key=b"o" * 32)
        other_device = models.Device(user_id=other_id, registration_id=2, identity_key=b"t" * 32)
        session.add_all([owner_device, other_device])
        await session.flush()
        conversation = models.Conversation(kind="dm", title=None)
        session.add(conversation)
        await session.flush()
        message = models.Message(
            conversation_id=conversation.id, sender_device_id=other_device.id,
            ciphertext=b"opaque", message_type="signal",
        )
        session.add(message)
        await session.flush()
        session.add_all([
            models.MessageReceipt(message_id=message.id, device_id=owner_device.id),
            models.MessageReceipt(message_id=message.id, device_id=other_device.id),
            models.UserBlock(blocker_id=owner_id, blocked_id=other_id, source="named"),
            models.UserBlock(blocker_id=other_id, blocked_id=owner_id, source="named"),
        ])
        await session.commit()
    created = await client.post("/me/data-requests", json={"kind": "export"}, headers=owner.headers)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "partially_completed"
    assert body["download_url"] is None
    export = await client.get(f"/me/data-requests/{body['id']}/download", headers=owner.headers)
    assert export.status_code == 200
    assert export.headers["content-disposition"].startswith("attachment;")
    assert export.json()["format"] == "chirp-account-export-v1"
    assert export.headers["cache-control"] == "private, no-store"
    assert len(export.json()["records"]["message_receipts"]) == 1
    assert len(export.json()["records"]["user_blocks"]) == 1
    authority = export.json()["records"]["organization_authority_acceptances"]
    assert len(authority) == 1 and authority[0]["user_id"] == owner.id
    assert "acct_other" not in export.text and other.id not in export.text
    # A second synthetic account must not bleed into the caller's artifact.
    assert other.id not in export.text
    assert (await client.get(f"/me/data-requests/{body['id']}/download", headers=other.headers)).status_code == 404


@pytest.mark.asyncio
async def test_expired_export_artifact_is_purged(
    client: AsyncClient, make_user
) -> None:
    owner: ApiUser = await make_user("Retention owner")
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        request = models.AccountDataRequest(user_id=uuid.UUID(owner.id), kind="export", open_key=None)
        session.add(request)
        await session.flush()
        session.add(models.AccountDataArtifact(
            request_id=request.id,
            content={"format": "synthetic"},
            content_sha256=uuid.uuid4().hex,
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        ))
        await session.commit()
        report = await run_export_artifact_expiry(
            session, now=datetime.now(timezone.utc), apply=True,
        )
        assert report["deleted"] == 1
        await session.commit()


@pytest.mark.asyncio
async def test_export_expiry_job_is_dry_run_bounded_owner_safe_and_idempotent(
    client: AsyncClient, make_user
) -> None:
    owner: ApiUser = await make_user("Expiry owner")
    other: ApiUser = await make_user("Expiry other")
    now = datetime(2026, 10, 7, tzinfo=timezone.utc)
    async with get_session_factory()() as session:
        requests = []
        for user_id in (uuid.UUID(owner.id), uuid.UUID(other.id)):
            request = models.AccountDataRequest(user_id=user_id, kind="export", open_key=None, status="partially_completed")
            session.add(request)
            await session.flush()
            requests.append(request)
            session.add(models.AccountDataArtifact(
                request_id=request.id, content={"owner": str(user_id)},
                content_sha256=uuid.uuid4().hex, expires_at=now - timedelta(seconds=1),
            ))
        fresh_request = models.AccountDataRequest(
            user_id=uuid.UUID(owner.id), kind="export", open_key=None, status="partially_completed",
        )
        session.add(fresh_request)
        await session.flush()
        session.add(models.AccountDataArtifact(
            request_id=fresh_request.id, content={"owner": owner.id},
            content_sha256=uuid.uuid4().hex, expires_at=now + timedelta(days=1),
        ))
        await session.commit()

        preview = await run_export_artifact_expiry(session, now=now, batch_size=1, max_batches=1)
        assert preview == {
            "mode": "dry_run", "cutoff": now.isoformat(), "eligible": 1,
            "deleted": 0, "capped": True, "budget": 1,
        }
        assert (await session.scalar(select(func.count()).select_from(models.AccountDataArtifact))) == 3

        applied = await run_export_artifact_expiry(session, now=now, batch_size=1, max_batches=1, apply=True)
        assert applied["mode"] == "apply" and applied["deleted"] == 1
        await session.commit()
        assert (await session.scalar(select(models.AccountDataRequest.id).where(
            models.AccountDataRequest.id == requests[0].id,
        ))) == requests[0].id
        rerun = await run_export_artifact_expiry(session, now=now, apply=True)
        assert rerun["deleted"] == 1
        await session.commit()
        assert (await session.scalar(select(func.count()).select_from(models.AccountDataArtifact))) == 1
        for request in requests:
            persisted = await session.get(models.AccountDataRequest, request.id)
            assert persisted is not None and persisted.status == "partially_completed"
        final = await run_export_artifact_expiry(session, now=now, apply=True)
        assert final["deleted"] == 0
        await session.rollback()


def test_export_expiry_job_rejects_non_boolean_apply_and_naive_time() -> None:
    with pytest.raises(TypeError, match="apply must be a bool"):
        import asyncio
        asyncio.run(run_export_artifact_expiry(object(), apply="false"))
    with pytest.raises(ValueError, match="timezone"):
        import asyncio
        asyncio.run(run_export_artifact_expiry(object(), now=datetime(2026, 10, 7)))
    with pytest.raises(SystemExit):
        expiry_main(["--max-rows", "1001"])


def test_export_expiry_cli_defaults_to_preview_and_fails_closed(monkeypatch, capsys) -> None:
    calls = []

    async def fake_run_cli(**kwargs):
        calls.append(kwargs)
        return {"mode": "apply" if kwargs["delete_rows"] else "dry_run", "status": "ok"}

    monkeypatch.setattr(expiry_job, "_run_cli", fake_run_cli)
    expiry_main([])
    expiry_main(["--delete", "--max-rows", "7", "--timeout-seconds", "9"])
    assert calls == [
        {"delete_rows": False, "max_rows": 100, "timeout_seconds": 30},
        {"delete_rows": True, "max_rows": 7, "timeout_seconds": 9},
    ]
    assert '"mode": "dry_run"' in capsys.readouterr().out

    async def timeout_run(**kwargs):
        raise TimeoutError

    monkeypatch.setattr(expiry_job, "_run_cli", timeout_run)
    with pytest.raises(SystemExit) as exited:
        expiry_main([])
    assert exited.value.code == 2
    assert '"status": "timed_out"' in capsys.readouterr().out


@pytest.mark.asyncio
async def test_request_creation_is_bounded_per_account(client: AsyncClient, make_user) -> None:
    owner: ApiUser = await make_user("Rate limited")
    responses = [
        await client.post("/me/data-requests", json={"kind": "export"}, headers=owner.headers)
        for _ in range(6)
    ]
    assert [response.status_code for response in responses] == [201, 201, 201, 201, 201, 429]
