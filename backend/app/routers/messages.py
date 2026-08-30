"""Messaging router: conversations, ciphertext messages with WS/push fan-out, receipts."""
import base64
import binascii
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.core.analytics import emit
from app.core.blocks import blockers_of
from app.core.campus_access import is_campus_verified
from app.core.errors import forbidden, not_found
from app.db import get_session
from app.middleware.auth import get_current_user
from app.schemas.messaging import (
    ConversationCreate,
    ConversationMemberOut,
    ConversationOut,
    MessageCreate,
    MessageOut,
    MessageReceiptCreate,
    MessageReceiptOut,
)
from app.services.fcm_service import send_content_free_push
from app.ws.pubsub import publish_to_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["messages"])


def _b64_to_bytes(value: str) -> bytes:
    """Decode the opaque base64 ciphertext field, raising 422 on malformed input."""
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=422, detail="invalid_base64") from None


async def _require_active_member(
    session: AsyncSession, conversation_id: uuid.UUID, user_id: uuid.UUID
) -> models.ConversationMember:
    """Return the caller's active membership row or raise 403 not_a_member."""
    member = await session.get(models.ConversationMember, (conversation_id, user_id))
    if member is None or member.left_at is not None:
        raise forbidden("not_a_member")
    return member


async def _require_reachable_off_chapter(
    session: AsyncSession, caller: models.User, member_ids: set[uuid.UUID]
) -> None:
    """Eligibility rule for a conversation with NO chapter_id (board card c243).

    This path used to have no check whatsoever — the membership test below it was inside
    `if body.chapter_id is not None`, so omitting chapter_id let any authenticated user
    open a conversation naming ANY user ids in the system, campus and chapter irrelevant.
    That is the cold-DM channel the app never intended to ship.

    WHO IS A LEGITIMATE RECIPIENT. `conversations.chapter_id` is documented as "NULL for
    cross-chapter DMs" (SPEC §3), so this path exists to let a Sigma Chi member DM a
    Delta Gamma member — people in DIFFERENT chapters on the SAME campus, which is the
    exact social graph the campus feed and Chirp already serve. Cross-CHAPTER is the
    documented feature; cross-CAMPUS is not, and nothing in SPEC, the schema, or the
    mobile app asks for it. So a recipient is reachable when either:

      1. they share an ACTIVE chapter membership with the caller — chapter content, and
         per Jose's Aug 16 ruling (core/campus_access.py) chapter membership stands on
         its own without an .edu; or
      2. they are on the caller's campus and the CALLER is currently campus-verified —
         the same gate campus-wide reach goes through everywhere else.

    Anything else is refused. A NULL campus on either side is not a match: `None == None`
    would otherwise make every campus-less account mutually reachable, which is the
    `campus_id is not None` shortcut core/campus_access.py explicitly forbids.

    The RECIPIENT is deliberately not required to be verified. Requiring it would make
    every not-yet-verified account unreachable, which during onboarding week is most of
    them; the abuse this rule stops needs a verified .edu on the SENDER's side, and that
    is where the bar belongs.
    """
    others = member_ids - {caller.id}
    if not others:
        return

    shared_chapters = select(models.Membership.chapter_id).where(
        models.Membership.user_id == caller.id,
        models.Membership.status == "active",
    )
    chapter_mates = await session.execute(
        select(models.Membership.user_id).where(
            models.Membership.user_id.in_(others),
            models.Membership.status == "active",
            models.Membership.chapter_id.in_(shared_chapters),
        )
    )
    unreachable = others - set(chapter_mates.scalars())

    if unreachable and caller.campus_id is not None and is_campus_verified(caller):
        campus_mates = await session.execute(
            select(models.User.id).where(
                models.User.id.in_(unreachable),
                models.User.campus_id == caller.campus_id,
            )
        )
        unreachable -= set(campus_mates.scalars())

    if unreachable:
        raise forbidden("recipient_not_reachable")


def _conversation_out(
    conversation: models.Conversation, members: list[models.ConversationMember]
) -> ConversationOut:
    """Serialize a conversation together with its member rows."""
    out = ConversationOut.model_validate(conversation)
    out.members = [ConversationMemberOut.model_validate(m) for m in members]
    return out


@router.post(
    "/conversations", response_model=ConversationOut, status_code=status.HTTP_201_CREATED
)
async def create_conversation(
    body: ConversationCreate,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ConversationOut:
    """Create a dm/group conversation; the creator is always added as a member."""
    member_ids = {user.id, *body.member_user_ids}
    users_result = await session.execute(
        select(models.User.id, models.User.is_ghost).where(models.User.id.in_(member_ids))
    )
    users = {user_id: is_ghost for user_id, is_ghost in users_result.all()}
    if len(users) != len(member_ids):
        raise not_found("user_not_found")
    if body.chapter_id is not None:
        if await session.get(models.Chapter, body.chapter_id) is None:
            raise not_found("chapter_not_found")
        active_memberships = await session.execute(
            select(models.Membership.user_id).where(
                models.Membership.chapter_id == body.chapter_id,
                models.Membership.status == "active",
                models.Membership.user_id.in_(member_ids),
            )
        )
        active_member_ids = set(active_memberships.scalars())
        if user.id not in active_member_ids:
            raise forbidden("not_a_member")
        # Ghost users are historical placeholders, not live chapter accounts. They
        # are the explicit exception that lets a chapter conversation retain them
        # alongside its active members without creating a membership row.
        non_ghost_ids = {
            member_id
            for member_id in member_ids
            if member_id != user.id and not users[member_id]
        }
        if not non_ghost_ids.issubset(active_member_ids):
            raise forbidden("not_a_member")
    else:
        await _require_reachable_off_chapter(session, user, member_ids)

    # Blocks are checked on BOTH paths, because a chapter_id does not make contact
    # consensual: someone you blocked is still on the roster and could otherwise name
    # you into a chapter conversation. Same refusal string as the eligibility failure
    # above so "they blocked me" and "they are not reachable" are one indistinguishable
    # response — the chapter branch already collapses four distinct causes into a single
    # not_a_member for the same reason.
    if await blockers_of(session, subject_id=user.id, candidate_ids=member_ids):
        raise forbidden("recipient_not_reachable")

    conversation = models.Conversation(
        chapter_id=body.chapter_id, kind=body.kind, title=body.title
    )
    session.add(conversation)
    await session.flush()
    await session.refresh(conversation)

    joined_at = datetime.now(timezone.utc)
    members = [
        models.ConversationMember(
            conversation_id=conversation.id, user_id=member_id, joined_at=joined_at
        )
        for member_id in sorted(member_ids, key=str)
    ]
    session.add_all(members)
    await session.commit()
    return _conversation_out(conversation, members)


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ConversationOut]:
    """List the caller's active conversations, each with its member rows."""
    result = await session.execute(
        select(models.Conversation)
        .join(
            models.ConversationMember,
            models.ConversationMember.conversation_id == models.Conversation.id,
        )
        .where(
            models.ConversationMember.user_id == user.id,
            models.ConversationMember.left_at.is_(None),
        )
        .order_by(models.Conversation.created_at.desc())
    )
    conversations = list(result.scalars().all())

    members_by_conversation: dict[uuid.UUID, list[models.ConversationMember]] = {}
    if conversations:
        member_rows = await session.execute(
            select(models.ConversationMember).where(
                models.ConversationMember.conversation_id.in_(
                    [c.id for c in conversations]
                )
            )
        )
        for member in member_rows.scalars():
            members_by_conversation.setdefault(member.conversation_id, []).append(member)

    return [
        _conversation_out(c, members_by_conversation.get(c.id, []))
        for c in conversations
    ]


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=MessageOut,
    status_code=status.HTTP_201_CREATED,
)
async def send_message(
    conversation_id: uuid.UUID,
    body: MessageCreate,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MessageOut:
    """Store ciphertext (never parsed) and fan out to active members via Redis + push."""
    await _require_active_member(session, conversation_id, user.id)
    device = await session.get(models.Device, body.sender_device_id)
    if device is None or device.user_id != user.id:
        raise forbidden("not_your_device")
    if device.revoked_at is not None:
        raise forbidden("device_revoked")

    recipients_result = await session.execute(
        select(models.ConversationMember.user_id).where(
            models.ConversationMember.conversation_id == conversation_id,
            models.ConversationMember.left_at.is_(None),
        )
    )
    member_ids = list(recipients_result.scalars().all())

    # Block enforcement, resolved BEFORE the insert so a refused send never leaves
    # ciphertext behind (board card c243). A conversation that already exists is not a
    # standing licence to keep talking — the block may well have been created because of
    # what was said in this very thread.
    blockers = await blockers_of(session, subject_id=user.id, candidate_ids=member_ids)
    others = {member_id for member_id in member_ids if member_id != user.id}
    if others and others <= blockers:
        # Everyone left to hear this has blocked the sender, so there is no one to
        # deliver to — the 1:1 DM case, and a group everybody has shut them out of.
        raise forbidden("recipient_not_reachable")
    # In a group where only SOME members blocked the sender, the send succeeds and the
    # blockers are simply dropped from the fan-out below. Refusing the whole send would
    # hand any single member a veto over everyone else's group, which is its own abuse.
    recipient_ids = [member_id for member_id in member_ids if member_id not in blockers]

    message = models.Message(
        conversation_id=conversation_id,
        sender_device_id=body.sender_device_id,
        ciphertext=_b64_to_bytes(body.ciphertext_b64),
        message_type=body.message_type,
    )
    session.add(message)
    await session.flush()
    await session.refresh(message)
    await session.commit()
    emit(
        "message_sent",
        user_id=user.id,
        conversation_id=conversation_id,
        message_type=body.message_type,
        recipient_count=len(recipient_ids),
    )

    event = {
        "type": "message",
        "conversation_id": str(conversation_id),
        "message_id": str(message.id),
        "sender_device_id": str(body.sender_device_id),
        "ciphertext": body.ciphertext_b64,
        "created_at": message.created_at.isoformat(),
    }
    for recipient_id in recipient_ids:
        try:
            await publish_to_user(str(recipient_id), event)
        except Exception:
            # Ids only — never ciphertext (SPEC §8.1).
            logger.warning(
                "ws fan-out failed message_id=%s user_id=%s", message.id, recipient_id
            )
        if recipient_id != user.id:
            await send_content_free_push(str(recipient_id), "New message")

    return MessageOut.model_validate(message)


@router.get(
    "/conversations/{conversation_id}/messages", response_model=list[MessageOut]
)
async def list_messages(
    conversation_id: uuid.UUID,
    before: datetime | None = None,
    before_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[MessageOut]:
    """Ciphertext history, newest first, cursor-paginated on (created_at, id). Members only.

    Pass both `before` and `before_id` (the last row's values from the previous
    page) for an exact tie-break so rows sharing a timestamp at a page boundary
    are never skipped. `before` alone still works (legacy clients) but does not
    guarantee tied-timestamp rows won't be dropped at the boundary.
    """
    await _require_active_member(session, conversation_id, user.id)
    stmt = select(models.Message).where(
        models.Message.conversation_id == conversation_id
    )
    if before is not None and before_id is not None:
        stmt = stmt.where(
            tuple_(models.Message.created_at, models.Message.id) < (before, before_id)
        )
    elif before is not None:
        stmt = stmt.where(models.Message.created_at < before)
    stmt = stmt.order_by(
        models.Message.created_at.desc(), models.Message.id.desc()
    ).limit(limit)
    result = await session.execute(stmt)
    return [MessageOut.model_validate(m) for m in result.scalars().all()]


@router.post(
    "/conversations/{conversation_id}/leave", response_model=ConversationMemberOut
)
async def leave_conversation(
    conversation_id: uuid.UUID,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ConversationMemberOut:
    """Set left_at for the caller — remaining clients rotate the sender key (SPEC §6.4)."""
    member = await session.get(
        models.ConversationMember, (conversation_id, user.id)
    )
    if member is None:
        raise forbidden("not_a_member")
    if member.left_at is None:
        member.left_at = datetime.now(timezone.utc)
        await session.commit()
    return ConversationMemberOut.model_validate(member)


@router.post("/messages/{message_id}/receipts", response_model=MessageReceiptOut)
async def upsert_receipt(
    message_id: uuid.UUID,
    body: MessageReceiptCreate,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MessageReceiptOut:
    """Record delivered_at for one of the caller's devices. Members only."""
    message = await session.get(models.Message, message_id)
    if message is None:
        raise not_found("message_not_found")
    await _require_active_member(session, message.conversation_id, user.id)
    device = await session.get(models.Device, body.device_id)
    if device is None or device.user_id != user.id:
        raise forbidden("not_your_device")

    delivered_at = body.delivered_at or datetime.now(timezone.utc)
    receipt = await session.get(
        models.MessageReceipt, (message_id, body.device_id)
    )
    if receipt is None:
        receipt = models.MessageReceipt(
            message_id=message_id, device_id=body.device_id, delivered_at=delivered_at
        )
        session.add(receipt)
        try:
            await session.commit()
        except IntegrityError:
            # Concurrent receipt insert for the same (message_id, device_id) —
            # rollback and treat as already-recorded instead of a 500.
            await session.rollback()
            receipt = await session.get(
                models.MessageReceipt, (message_id, body.device_id)
            )
            if receipt is None:
                raise
    else:
        receipt.delivered_at = delivered_at
        await session.commit()
    return MessageReceiptOut.model_validate(receipt)
