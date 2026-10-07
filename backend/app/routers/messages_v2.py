"""E2EE v2 message transport: one ciphertext leg per recipient device (board card c444).

One logical message is ONE `messages` row (no message-level ciphertext) plus one
`message_legs` row per recipient DEVICE: every approved device of the other member, plus the
sender's own other approved devices so the sender's history stays in sync across phones
(E2EE-DESIGN.md section 5). The v1 endpoints in routers/messages.py are unchanged for legacy
rows; v1 history never returns a v2 message and a v2 device cannot post a v1 one.

THE EXACT-DEVICE-SET RULE. The set of leg recipients must EQUAL the current approved,
unrevoked v2 devices of the conversation's active members, minus the sending device. A
missing device would be a phone that silently never gets the message; an extra one is either
a revoked/pending device or a guess. Either way the answer is 409 `device_list_mismatch`
carrying the exact required id list, and NOTHING is stored: the client refreshes its
directory, re-encrypts for the right set and retries (design section 7). The check reads the
directory without locking it, so a device approved a millisecond after the check misses that
one message - the same outcome as a device that was approved a millisecond later, and exactly
what "messages that predate a device have no leg for it" already means.

DM ONLY IN c444. Groups use Megolm and need their own spec (design section 9). A pairwise-leg
message to a 256-member group would be up to 1,280 legs of 64 KiB each in one request, so
the transport refuses non-DM conversations with `e2ee_v2_requires_dm` rather than inventing a
bound for them. For a DM the request is at most MAX_V2_LEGS_PER_MESSAGE (10) legs.

WS HINTS. The live event and the sweeper's rebuilt event (services/outbox.py) carry NO
ciphertext: it is published verbatim to every recipient USER, and each device's leg is that
device's alone. The receiving device fetches its own leg over HTTP.
"""
from __future__ import annotations

import base64
import binascii
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.core.blocks import blockers_of
from app.core.errors import forbidden, not_found
from app.core.rate_limits import MESSAGE_SEND_LIMIT, limit_per_user
from app.db import get_session
from app.middleware.auth import get_current_user
from app.routers.keys import MAX_ACTIVE_DEVICES
from app.routers.messages import _require_active_member, _visible_message_query
from app.schemas.e2ee_v2 import (
    ENVELOPE_VERSION_1,
    MessageCreateV2,
    MessageHistoryV2Out,
    MessageLegOut,
    MessageV2Out,
    b64,
)
from app.services import outbox
from app.services.e2ee_v2_service import approved_v2_devices_for_users, conflict_response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v2", tags=["messages-v2"])

# message_type stored on a v2 row. v1 uses "signal" / "sender_key_distribution"; the v1
# MessageOut never reads a v2 row, so this is bookkeeping, not a wire value.
V2_MESSAGE_TYPE = "olm"


def _decode_leg(ciphertext_b64: str) -> bytes:
    """Strict base64 for a leg body; the server never parses the bytes (SPEC 8.1)."""
    try:
        return base64.b64decode(ciphertext_b64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=422, detail="invalid_base64") from None


def _require_usable_device(device: models.Device | None, user: models.User) -> models.Device:
    """The caller must own `device`, and it must be a v2 device that is approved and live.

    Distinct fixed codes per cause: the caller owns this device, so there is nothing about
    ANOTHER account to protect here (unlike the approver check in keys_v2).
    """
    if device is None or device.user_id != user.id:
        raise forbidden("not_your_device")
    if device.revoked_at is not None:
        raise forbidden("device_revoked")
    if device.crypto_suite is None:
        raise forbidden("device_suite_unsupported")
    if device.approved_at is None:
        raise forbidden("device_not_approved")
    return device


async def _same_legs(
    session: AsyncSession,
    existing: models.Message,
    conversation_id: uuid.UUID,
    legs: dict[uuid.UUID, tuple[int, bytes]],
) -> bool:
    """Whether a retry carries exactly the stored message's conversation and legs."""
    if existing.conversation_id != conversation_id:
        return False
    rows = await session.execute(
        select(models.MessageLeg).where(models.MessageLeg.message_id == existing.id)
    )
    stored = {leg.recipient_device_id: (leg.olm_type, bytes(leg.ciphertext)) for leg in rows.scalars()}
    return stored == legs


async def _existing_message(
    session: AsyncSession, sender_device_id: uuid.UUID, client_message_id: uuid.UUID
) -> models.Message | None:
    return await session.scalar(
        select(models.Message).where(
            models.Message.sender_device_id == sender_device_id,
            models.Message.client_message_id == client_message_id,
        )
    )


def _message_out(message: models.Message) -> MessageV2Out:
    assert message.client_message_id is not None and message.envelope_version is not None
    return MessageV2Out(
        id=message.id,
        conversation_id=message.conversation_id,
        sender_device_id=message.sender_device_id,
        client_message_id=message.client_message_id,
        envelope_version=message.envelope_version,
        created_at=message.created_at,
    )


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=MessageV2Out,
    status_code=status.HTTP_201_CREATED,
    # The SAME scope as v1 send_message on purpose: one budget per account for "messages
    # sent", whichever API carried them, so moving to v2 cannot double a caller's allowance.
    dependencies=[Depends(limit_per_user("message_send", MESSAGE_SEND_LIMIT))],
    responses={
        200: {"description": "Idempotent retry: the stored message, nothing re-stored"},
        409: {"description": "device_list_mismatch (body carries device_ids), "
                             "client_message_id_reused, no_recipient_devices, e2ee_v2_requires_dm"},
    },
)
async def send_message_v2(
    conversation_id: uuid.UUID,
    body: MessageCreateV2,
    response: Response,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MessageV2Out | JSONResponse:
    """Store one message with one ciphertext leg per recipient device, atomically.

    Order of refusals, cheapest and least informative first: membership, conversation
    kind, sender device, envelope version, leg shape, idempotent replay, blocks, exact
    device set. The replay check runs BEFORE the device-set check on purpose: a retry of an
    accepted message must keep succeeding even if a device was approved or revoked since,
    otherwise a lost response would turn into a spurious device_list_mismatch.
    """
    await _require_active_member(session, conversation_id, user.id)
    conversation = await session.get(models.Conversation, conversation_id)
    if conversation is None:
        raise not_found("conversation_not_found")
    if conversation.kind != "dm":
        raise HTTPException(status_code=409, detail="e2ee_v2_requires_dm")

    sender = _require_usable_device(await session.get(models.Device, body.sender_device_id), user)
    # Plain values, captured now: after the rollback in the IntegrityError path below every
    # ORM instance on this session (sender, user) is expired, and touching an expired
    # attribute from async code raises MissingGreenlet instead of reloading.
    sender_id = sender.id
    if body.envelope_version != ENVELOPE_VERSION_1:
        raise HTTPException(status_code=422, detail="unsupported_envelope_version")

    legs: dict[uuid.UUID, tuple[int, bytes]] = {}
    for leg in body.legs:
        if leg.recipient_device_id in legs:
            raise HTTPException(status_code=422, detail="duplicate_leg_recipient")
        legs[leg.recipient_device_id] = (leg.olm_type, _decode_leg(leg.ciphertext_b64))

    existing = await _existing_message(session, sender_id, body.client_message_id)
    if existing is not None:
        if await _same_legs(session, existing, conversation_id, legs):
            response.status_code = status.HTTP_200_OK
            return _message_out(existing)
        return conflict_response("client_message_id_reused")

    member_ids = list(
        (
            await session.execute(
                select(models.ConversationMember.user_id).where(
                    models.ConversationMember.conversation_id == conversation_id,
                    models.ConversationMember.left_at.is_(None),
                )
            )
        ).scalars()
    )
    # Same block handling as v1 send_message: resolved BEFORE the insert so a refused
    # send leaves nothing behind. The required leg set below does NOT depend on blocks, so
    # a sender cannot learn who blocked them from a device_list_mismatch; blockers are
    # only dropped from the fan-out, and c348's history filter hides the sender's messages
    # from a reader who named-blocked them.
    blockers = await blockers_of(session, subject_id=user.id, candidate_ids=member_ids)
    others = {member_id for member_id in member_ids if member_id != user.id}
    if others and others <= blockers:
        raise forbidden("recipient_not_reachable")
    recipient_ids = [member_id for member_id in member_ids if member_id not in blockers]

    ceiling = len(member_ids) * MAX_ACTIVE_DEVICES
    directory = await approved_v2_devices_for_users(session, member_ids, limit=ceiling)
    if len(directory) > ceiling:
        raise HTTPException(status_code=409, detail="active_device_limit_reached")
    required = {device.id: device for device in directory if device.id != sender_id}
    if not any(device.user_id != user.id for device in required.values()):
        # Nobody but the sender's own phones could ever read this: the other member has
        # no approved v2 device yet. Storing it would be an unreadable message.
        return conflict_response("no_recipient_devices", device_ids=sorted(str(i) for i in required))
    if set(legs) != set(required):
        return conflict_response("device_list_mismatch", device_ids=sorted(str(i) for i in required))

    message = models.Message(
        conversation_id=conversation_id,
        sender_device_id=sender_id,
        ciphertext=None,
        message_type=V2_MESSAGE_TYPE,
        client_message_id=body.client_message_id,
        envelope_version=body.envelope_version,
    )
    session.add(message)
    try:
        await session.flush()
    except IntegrityError:
        # A concurrent request with the same (sender_device_id, client_message_id) won
        # the unique index. Treat it exactly like a sequential retry.
        await session.rollback()
        existing = await _existing_message(session, sender_id, body.client_message_id)
        if existing is None:
            raise
        if await _same_legs(session, existing, conversation_id, legs):
            response.status_code = status.HTTP_200_OK
            return _message_out(existing)
        return conflict_response("client_message_id_reused")
    await session.refresh(message)
    session.add_all(
        models.MessageLeg(
            message_id=message.id,
            recipient_device_id=device_id,
            olm_type=olm_type,
            ciphertext=ciphertext,
        )
        for device_id, (olm_type, ciphertext) in legs.items()
    )
    # Board c356: the delivery intent is written in THIS transaction, before commit, so the
    # message, its legs and the record that it is owed to recipient_ids commit or roll
    # back together. No ciphertext in the payload, ever.
    outbox_row_id = await outbox.enqueue(
        session,
        kind="message",
        recipient_ids=recipient_ids,
        payload={
            "conversation_id": str(conversation_id),
            "message_id": str(message.id),
            "sender_id": str(user.id),
            "sender_device_id": str(sender_id),
            "created_at": message.created_at.isoformat(),
        },
    )
    await session.commit()

    event = {
        "type": "message",
        "conversation_id": str(conversation_id),
        "message_id": str(message.id),
        "sender_device_id": str(sender_id),
        "envelope_version": body.envelope_version,
        "created_at": message.created_at.isoformat(),
    }
    try:
        # Best-effort immediate delivery; a failure leaves the outbox row for the sweeper.
        await outbox.dispatch_now(
            session,
            outbox_row_id,
            kind="message",
            recipient_ids=recipient_ids,
            event=event,
            sender_id=str(user.id),
        )
    except Exception:
        logger.warning("outbox live dispatch follow-up failed message_id=%s", message.id)
    return _message_out(message)


@router.get(
    "/conversations/{conversation_id}/messages", response_model=list[MessageHistoryV2Out]
)
async def list_messages_v2(
    conversation_id: uuid.UUID,
    device_id: uuid.UUID,
    before: datetime | None = None,
    before_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[MessageHistoryV2Out]:
    """History for ONE device: each message carries only that device's leg, newest first.

    The caller must be a member and own `device_id`, which must be approved and unrevoked,
    so a pending or revoked phone cannot read legs even though they were stored for it.
    A message with no leg for this device is omitted, not returned empty: it predates the
    device (or the device is the sender, whose copy lives in its own local store). Same
    (created_at, id) cursor as v1 and the same c348 named-block visibility rule.
    """
    await _require_active_member(session, conversation_id, user.id)
    _require_usable_device(await session.get(models.Device, device_id), user)

    stmt = (
        _visible_message_query(conversation_id, user.id, v2=True)
        .add_columns(models.MessageLeg)
        .join_from(
            models.Message,
            models.MessageLeg,
            (models.MessageLeg.message_id == models.Message.id)
            & (models.MessageLeg.recipient_device_id == device_id),
        )
    )
    if before is not None and before_id is not None:
        stmt = stmt.where(
            tuple_(models.Message.created_at, models.Message.id) < (before, before_id)
        )
    elif before is not None:
        stmt = stmt.where(models.Message.created_at < before)
    stmt = stmt.order_by(models.Message.created_at.desc(), models.Message.id.desc()).limit(limit)

    rows = (await session.execute(stmt)).all()
    return [
        MessageHistoryV2Out(
            **_message_out(message).model_dump(),
            leg=MessageLegOut(olm_type=leg.olm_type, ciphertext_b64=b64(leg.ciphertext) or ""),
        )
        for message, leg in rows
    ]
