"""e2ee key-material retirement (board c347): consumed one-time prekeys, superseded
signed prekeys, and every prekey row of a long-revoked device are hard-deleted once a
grace window has passed. This is a phase INDEPENDENT of app.jobs.purge's user-content
retention: test_purge.py's PurgeResult/report["counts"] are untouched by any of this.

Rows are inserted directly via the ORM (not the API) so consumed_at/created_at/
revoked_at can be backdated precisely, mirroring test_purge.py's own reasoning.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

from app import models
from app.jobs import purge
from app.db import get_session_factory
from app.jobs.purge import preview_expired_key_material, retire_key_material_batch
from tests.conftest import MakeUser
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

GRACE_DAYS = 7
NOW = datetime.now(timezone.utc)
CUTOFF = NOW - timedelta(days=GRACE_DAYS)
WITHIN_GRACE = NOW - timedelta(days=GRACE_DAYS - 1)  # newer than cutoff -> survives
PAST_GRACE = NOW - timedelta(days=GRACE_DAYS + 1)  # older than cutoff -> eligible


async def _device(owner_id: str, *, revoked_at: datetime | None = None) -> uuid.UUID:
    async with get_session_factory()() as session:
        device = models.Device(
            user_id=uuid.UUID(owner_id), registration_id=1, identity_key=b"identity",
            revoked_at=revoked_at,
        )
        session.add(device)
        await session.commit()
        await session.refresh(device)
        return device.id


async def _otk(
    device_id: uuid.UUID, *, consumed_at: datetime | None, key_id: int = 1,
) -> uuid.UUID:
    async with get_session_factory()() as session:
        row = models.OneTimePrekey(
            device_id=device_id, key_id=key_id, public_key=b"otk", consumed_at=consumed_at,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.id


async def _kyber(
    device_id: uuid.UUID, *, consumed_at: datetime | None,
    is_last_resort: bool = False, key_id: int = 1,
) -> uuid.UUID:
    async with get_session_factory()() as session:
        row = models.KyberPrekey(
            device_id=device_id, key_id=key_id, public_key=b"kyber", signature=b"sig",
            is_last_resort=is_last_resort, consumed_at=consumed_at,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.id


async def _signed(device_id: uuid.UUID, *, created_at: datetime, key_id: int = 1) -> uuid.UUID:
    async with get_session_factory()() as session:
        row = models.SignedPrekey(
            device_id=device_id, key_id=key_id, public_key=b"spk", signature=b"sig",
            created_at=created_at,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.id


async def _exists(model: type, row_id: uuid.UUID) -> bool:
    async with get_session_factory()() as session:
        return await session.get(model, row_id) is not None


async def test_key_material_retirement_respects_grace_window_and_retains_newest_signed_prekey(
    make_user: MakeUser,
) -> None:
    owner = await make_user("Key Retirement Owner")

    # (a) consumed EC one-time prekeys straddling the grace window, same device.
    device_a = await _device(owner.id)
    otk_within = await _otk(device_a, consumed_at=WITHIN_GRACE)
    otk_past = await _otk(device_a, consumed_at=PAST_GRACE, key_id=2)

    # (b) consumed one-time Kyber prekeys straddling the grace window, same device.
    kyber_within = await _kyber(device_a, consumed_at=WITHIN_GRACE)
    kyber_past = await _kyber(device_a, consumed_at=PAST_GRACE, key_id=2)

    # (c) two signed prekeys, BOTH past grace by created_at - the newer one must
    # survive because nothing newer exists for IT, not because it is within grace.
    device_c = await _device(owner.id)
    signed_old = await _signed(device_c, created_at=PAST_GRACE - timedelta(days=1))
    signed_new = await _signed(device_c, created_at=PAST_GRACE)

    # (d) a device revoked past the grace window: ALL its prekey rows are wiped
    # regardless of their own consumed/created state; the Device row itself survives.
    device_d = await _device(owner.id, revoked_at=PAST_GRACE)
    otk_d = await _otk(device_d, consumed_at=None)
    kyber_d = await _kyber(device_d, consumed_at=None, is_last_resort=True)
    signed_d = await _signed(device_d, created_at=NOW)

    # (e) negative control: unconsumed, unrevoked - nothing here is eligible.
    device_e = await _device(owner.id)
    otk_e = await _otk(device_e, consumed_at=None)

    async with get_session_factory()() as session:
        preview, capped = await preview_expired_key_material(
            session, cutoff=CUTOFF, count_limit=1000,
        )
        await session.rollback()
    assert capped == []

    async with get_session_factory()() as session:
        batch = await retire_key_material_batch(session, cutoff=CUTOFF, batch_size=1000)
        await session.commit()

    # Preview and apply must agree (not just on totals - re-derive the full result).
    assert preview == batch, (preview, batch)
    assert batch.retired_one_time_prekeys == 1
    assert batch.retired_kyber_one_time_prekeys == 1
    assert batch.retired_signed_prekeys == 1
    assert batch.retired_revoked_device_prekeys == 3

    assert await _exists(models.OneTimePrekey, otk_within) is True
    assert await _exists(models.OneTimePrekey, otk_past) is False
    assert await _exists(models.KyberPrekey, kyber_within) is True
    assert await _exists(models.KyberPrekey, kyber_past) is False
    assert await _exists(models.SignedPrekey, signed_old) is False
    assert await _exists(models.SignedPrekey, signed_new) is True
    assert await _exists(models.OneTimePrekey, otk_d) is False
    assert await _exists(models.KyberPrekey, kyber_d) is False
    assert await _exists(models.SignedPrekey, signed_d) is False
    assert await _exists(models.OneTimePrekey, otk_e) is True

    async with get_session_factory()() as session:
        device_row = await session.get(models.Device, device_d)
        assert device_row is not None
        assert device_row.revoked_at is not None


async def test_key_only_budget_reports_backlog_then_completes_and_repeats(make_user):
    owner = await make_user("Bounded key retirement")
    device = await _device(owner.id)
    expired = [await _otk(device, consumed_at=PAST_GRACE, key_id=i) for i in (1, 2)]
    survivor = await _otk(device, consumed_at=None, key_id=3)

    preview = await purge.run_purge_job(now=NOW, batch_size=1, max_batches=1)
    assert preview["status"] == "preview" and preview["remaining"] is False
    assert preview["key_retirement_status"] == "preview"
    assert preview["key_retirement_remaining"] is True
    assert preview["key_retirement_capped_counts"] == ["retired_one_time_prekeys"]
    assert preview["key_retirement_batches_committed"] == 0
    assert preview["key_retirement_counts"]["retired_one_time_prekeys"] == 1
    assert all([await _exists(models.OneTimePrekey, row) for row in expired])

    first = await purge.run_purge_job(apply=True, now=NOW, batch_size=1, max_batches=1)
    # Existing content fields keep their original meaning even with key backlog.
    assert first["status"] == "complete" and first["remaining"] is False
    assert first["physical_rows"] == 0 and first["batches_committed"] == 1
    assert first["key_retirement_status"] == "incomplete"
    assert first["key_retirement_remaining"] is True
    assert first["key_retirement_batches_committed"] == 1
    assert first["key_retirement_counts"]["retired_one_time_prekeys"] == 1
    assert sum([await _exists(models.OneTimePrekey, row) for row in expired]) == 1

    second = await purge.run_purge_job(apply=True, now=NOW, batch_size=1, max_batches=1)
    assert second["key_retirement_status"] == "complete"
    assert second["key_retirement_remaining"] is False
    assert second["key_retirement_counts"]["retired_one_time_prekeys"] == 1
    assert not any([await _exists(models.OneTimePrekey, row) for row in expired])
    repeated = await purge.run_purge_job(apply=True, now=NOW, batch_size=1, max_batches=1)
    assert repeated["key_retirement_status"] == "complete"
    assert repeated["key_retirement_remaining"] is False
    assert sum(repeated["key_retirement_counts"].values()) == 0
    assert await _exists(models.OneTimePrekey, survivor)


async def test_revoked_key_preview_reports_capped_aggregate_without_deleting(make_user):
    owner = await make_user("Revoked key preview")
    device = await _device(owner.id, revoked_at=PAST_GRACE)
    rows = []
    for i in (1, 2):
        rows.extend([
            (models.OneTimePrekey, await _otk(device, consumed_at=None, key_id=i)),
            (models.KyberPrekey, await _kyber(device, consumed_at=None, key_id=i)),
            (models.SignedPrekey, await _signed(device, created_at=NOW, key_id=i)),
        ])
    report = await purge.run_purge_job(now=NOW, batch_size=1, max_batches=1)
    assert report["key_retirement_status"] == "preview"
    assert report["key_retirement_remaining"] is True
    assert report["key_retirement_counts"]["retired_revoked_device_prekeys"] == 3
    assert report["key_retirement_capped_counts"] == ["retired_revoked_device_prekeys"]
    assert all([await _exists(model, row) for model, row in rows])


async def test_locked_key_is_blocked_until_real_lock_releases(make_user):
    owner = await make_user("Locked key retirement")
    device = await _device(owner.id)
    row = await _otk(device, consumed_at=PAST_GRACE)
    async with get_session_factory()() as writer:
        await writer.execute(text(
            "SELECT id FROM one_time_prekeys WHERE id=:id FOR UPDATE"
        ), {"id": row})
        report = await purge.run_purge_job(apply=True, now=NOW, batch_size=1, max_batches=2)
        assert report["status"] == "complete"
        assert report["key_retirement_status"] == "blocked"
        assert report["key_retirement_remaining"] is True
        assert report["key_retirement_batches_committed"] == 1
        assert sum(report["key_retirement_counts"].values()) == 0
        assert await _exists(models.OneTimePrekey, row)
        await writer.rollback()
    retry = await purge.run_purge_job(apply=True, now=NOW, batch_size=1, max_batches=2)
    assert retry["key_retirement_status"] == "complete"
    assert retry["key_retirement_remaining"] is False
    assert not await _exists(models.OneTimePrekey, row)


@pytest.mark.parametrize("fault", ["failure", "deadline", "unconfirmed_commit"])
async def test_key_phase_failure_keeps_only_acknowledged_commits(make_user, monkeypatch, fault):
    owner = await make_user("Partial key retirement")
    device = await _device(owner.id)
    rows = [await _otk(device, consumed_at=PAST_GRACE, key_id=i) for i in (1, 2, 3)]
    real_batch = purge.retire_key_material_batch
    real_commit = AsyncSession.commit
    batches = commits = 0

    async def interrupted_batch(*args, **kwargs):
        nonlocal batches
        batches += 1
        if batches == 2:
            if fault == "failure":
                raise RuntimeError("private key SQL parameter must not be logged")
            if fault == "deadline":
                await asyncio.sleep(5)
        return await real_batch(*args, **kwargs)

    async def delayed_acknowledgement(session):
        nonlocal commits
        commits += 1
        await real_commit(session)
        # Commit1 is the empty content phase, commit2 the first key batch.
        if fault == "unconfirmed_commit" and commits == 3:
            await asyncio.sleep(5)

    monkeypatch.setattr(purge, "retire_key_material_batch", interrupted_batch)
    monkeypatch.setattr(AsyncSession, "commit", delayed_acknowledgement)
    report = await purge.run_purge_job(
        apply=True, now=NOW, batch_size=1, max_batches=3, max_seconds=1,
    )
    expected = "failed" if fault == "failure" else "timed_out"
    assert report["status"] == report["key_retirement_status"] == expected
    assert report["key_retirement_remaining"] is None
    assert report["key_retirement_batches_committed"] == 1
    assert report["key_retirement_counts"]["retired_one_time_prekeys"] == 1
    assert report["commit_outcome_unknown"] is (fault == "unconfirmed_commit")
    assert report["physical_rows"] == 0 and report["batches_committed"] == 1
    assert "private key SQL parameter" not in json.dumps(report)
    assert sum([await _exists(models.OneTimePrekey, row) for row in rows]) == (
        1 if fault == "unconfirmed_commit" else 2
    )
