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
