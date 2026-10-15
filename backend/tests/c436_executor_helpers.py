"""Explicit PostgreSQL setup for synthetic deletion-executor tests."""
from __future__ import annotations

import uuid

from app.db import get_session_factory
from app.services.account_deletion_hold import activate_deletion_hold


async def activate_executor_hold(*, user_id: uuid.UUID, request_id: uuid.UUID) -> None:
    """Commit the same active hold a reviewed executor run must require."""
    async with get_session_factory()() as session:
        await activate_deletion_hold(
            session, user_id=user_id, request_id=request_id,
        )
        await session.commit()
