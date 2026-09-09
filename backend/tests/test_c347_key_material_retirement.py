"""e2ee key-material retirement (board c347): consumed one-time prekeys, superseded
signed prekeys, and every prekey row of a long-revoked device are hard-deleted once a
grace window has passed. This is a phase INDEPENDENT of app.jobs.purge's user-content
retention: test_purge.py's PurgeResult/report["counts"] are untouched by any of this.

Rows are inserted directly via the ORM (not the API) so consumed_at/created_at/
revoked_at can be backdated precisely, mirroring test_purge.py's own reasoning.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app import models
from app.db import get_session_factory
from app.jobs.purge import preview_expired_key_material, retire_key_material_batch
from tests.conftest import MakeUser

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
