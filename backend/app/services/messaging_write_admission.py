"""Deletion-hold admission for message writes.

The preflight uses a short-lived session so that the request session can be bound to
the complete conversation participant set before it performs any request SQL.  The
conversation row is then locked again immediately before a new message is built;
membership changes therefore either happen before admission or are rejected by the
guard's exact-scope check.  This module deliberately does not import a router.
"""
from __future__ import annotations

import uuid

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.config import get_settings
from app.core.errors import forbidden
from app.core.blocks import blockers_of
from app.db import get_session, get_session_factory
from app.middleware.auth import get_current_user, get_verified_uid
from app.schemas.e2ee_v2 import MessageCreateV2
from app.services.account_write_guard import (
    assert_account_write_targets,
    bind_account_write_guard,
)


async def _active_member_ids(
    session: AsyncSession, conversation_id: uuid.UUID
) -> list[uuid.UUID]:
    result = await session.execute(
        select(models.ConversationMember.user_id)
        .where(
            models.ConversationMember.conversation_id == conversation_id,
            models.ConversationMember.left_at.is_(None),
        )
        .order_by(models.ConversationMember.user_id)
    )
    return list(result.scalars())


async def _admit_message_write(
    conversation_id: uuid.UUID,
    body: MessageCreateV2 | None = None,
    uid: str = Depends(get_verified_uid),
    session: AsyncSession = Depends(get_session),
) -> models.User:
    """Authenticate the actor and bind the active conversation roster when enabled."""
    if not get_settings().account_deletion_http_admission_enabled:
        return await get_current_user(uid=uid, session=session)

    # This session is never used for the request mutation.  In particular, it does not
    # hold a transaction while the request session waits for the deletion gates.
    async with get_session_factory()() as preflight:
        actor = await get_current_user(uid=uid, session=preflight)
        members = await _active_member_ids(preflight, conversation_id)
        if actor.id not in members:
            # Do not inspect or report holds for a conversation the caller cannot enter.
            raise forbidden("not_a_member")

        # Validate the caller's sender device before using the idempotency key.  A
        # previously accepted message can be retried even after a recipient enters a
        # deletion hold or becomes unreachable; the route still compares every leg.
        existing = None
        if body is not None:
            sender = await preflight.scalar(
                select(models.Device).where(models.Device.id == body.sender_device_id)
            )
            if (
                sender is not None
                and sender.user_id == actor.id
                and sender.revoked_at is None
                and sender.crypto_suite is not None
                and sender.approved_at is not None
            ):
                existing = await preflight.scalar(
                    select(models.Message).where(
                        models.Message.sender_device_id == body.sender_device_id,
                        models.Message.client_message_id == body.client_message_id,
                    )
                )
        if existing is None:
            others = {member_id for member_id in members if member_id != actor.id}
            if others and others <= await blockers_of(
                preflight, subject_id=actor.id, candidate_ids=members
            ):
                raise forbidden("recipient_not_reachable")

    bind_account_write_guard(
        session,
        user_id=actor.id,
        firebase_uid=uid,
        target_user_ids=() if existing is not None else members,
    )
    # Resolve through the request session as well.  The guard's first transaction now
    # rechecks both the actor's UID and every bound participant before any route SQL.
    return await get_current_user(uid=uid, session=session)


async def get_current_user_for_message_write(
    conversation_id: uuid.UUID,
    uid: str = Depends(get_verified_uid),
    session: AsyncSession = Depends(get_session),
) -> models.User:
    """Bodyless dependency for legacy message sends."""
    return await _admit_message_write(
        conversation_id, body=None, uid=uid, session=session
    )


async def get_current_user_for_message_v2_write(
    conversation_id: uuid.UUID,
    body: MessageCreateV2,
    uid: str = Depends(get_verified_uid),
    session: AsyncSession = Depends(get_session),
) -> models.User:
    """V2 dependency that can preserve safe, read-only idempotent retries."""
    return await _admit_message_write(
        conversation_id, body=body, uid=uid, session=session
    )


async def lock_message_participants(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
) -> list[uuid.UUID]:
    """Return the current active roster, locking and validating it when enabled."""
    settings = get_settings()
    if settings.account_deletion_http_admission_enabled:
        conversation = await session.scalar(
            select(models.Conversation.id)
            .where(models.Conversation.id == conversation_id)
            .with_for_update()
        )
        if conversation is None:
            raise forbidden("not_a_member")

    members = await _active_member_ids(session, conversation_id)
    if user_id not in members:
        raise forbidden("not_a_member")
    if settings.account_deletion_http_admission_enabled:
        assert_account_write_targets(
            session,
            user_id=user_id,
            target_user_ids=members,
        )
    return members
