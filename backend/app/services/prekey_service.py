"""Atomic one-time-prekey handout: consume-on-read with FOR UPDATE SKIP LOCKED."""
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import models


async def consume_one_time_prekey(
    session: AsyncSession, device_id: uuid.UUID
) -> models.OneTimePrekey | None:
    """Atomically claim one unconsumed prekey for the device, or return None.

    UPDATE ... SET consumed_at = now() WHERE id = (SELECT ... WHERE consumed_at IS NULL
    LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING ... — concurrent fetchers never hand out
    the same key twice.
    """
    candidate_id = (
        select(models.OneTimePrekey.id)
        .where(
            models.OneTimePrekey.device_id == device_id,
            models.OneTimePrekey.consumed_at.is_(None),
        )
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    stmt = (
        update(models.OneTimePrekey)
        .where(models.OneTimePrekey.id == candidate_id)
        .values(consumed_at=func.now())
        .returning(models.OneTimePrekey)
    )
    result = await session.execute(stmt)
    return result.scalars().first()


async def consume_one_time_kyber_prekey(
    session: AsyncSession, device_id: uuid.UUID
) -> models.KyberPrekey | None:
    """Atomically claim one unconsumed one-time Kyber prekey for the device, or return None.

    Same UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING ... pattern as
    `consume_one_time_prekey`, scoped to `is_last_resort IS FALSE` so the last-resort row is
    never a candidate here.
    """
    candidate_id = (
        select(models.KyberPrekey.id)
        .where(
            models.KyberPrekey.device_id == device_id,
            models.KyberPrekey.consumed_at.is_(None),
            models.KyberPrekey.is_last_resort.is_(False),
        )
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    stmt = (
        update(models.KyberPrekey)
        .where(models.KyberPrekey.id == candidate_id)
        .values(consumed_at=func.now())
        .returning(models.KyberPrekey)
    )
    result = await session.execute(stmt)
    return result.scalars().first()


async def get_last_resort_kyber_prekey(
    session: AsyncSession, device_id: uuid.UUID
) -> models.KyberPrekey | None:
    """Return the device's current last-resort Kyber prekey, if any — never marks it consumed."""
    result = await session.execute(
        select(models.KyberPrekey)
        .where(
            models.KyberPrekey.device_id == device_id,
            models.KyberPrekey.is_last_resort.is_(True),
        )
        .order_by(models.KyberPrekey.created_at.desc())
        .limit(1)
    )
    return result.scalars().first()
