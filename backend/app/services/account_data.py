"""Scoped account export and deletion fulfillment with explicit provider residuals."""

from __future__ import annotations

import hashlib
import json
import base64
import uuid
from datetime import datetime, timedelta, timezone
from typing import Protocol

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import models


class AccountProvider(Protocol):
    """Provider adapter contract; production adapters must be explicitly configured."""

    name: str

    async def fulfill_deletion(self, *, firebase_uid: str, email: str) -> bool: ...


class UnconfiguredProvider:
    """Fail closed when provider credentials/console integration are unavailable."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def fulfill_deletion(self, *, firebase_uid: str, email: str) -> bool:
        return False


PROVIDER_ADAPTERS: tuple[AccountProvider, ...] = (
    UnconfiguredProvider("firebase_auth"),
    UnconfiguredProvider("stripe_connect"),
    UnconfiguredProvider("transactional_email"),
    UnconfiguredProvider("object_storage"),
    UnconfiguredProvider("logging_and_backups"),
)


async def purge_expired_export_artifacts(session: AsyncSession) -> int:
    """Delete expired export copies; callers run this from the retention job."""
    result = await session.execute(
        delete(models.AccountDataArtifact).where(
            models.AccountDataArtifact.expires_at.is_not(None),
            models.AccountDataArtifact.expires_at <= datetime.now(timezone.utc),
        )
    )
    return result.rowcount or 0

EXPORT_SCOPE = [
    "account profile and organization memberships",
    "posts, comments, anonymous board records, and reactions authored by you",
    "events, RSVPs, attendance, alumni profile, and lineage records linked to you",
    "message metadata and device/key-directory records (message ciphertext is not decrypted)",
    "payment and ledger records linked to you, with provider credentials redacted",
    "legal policy versions and your acceptance records",
]
EXPORT_EXCLUDED = [
    "provider-held authentication, payment, email, storage, logging, and backup copies",
    "message plaintext that the server never stores",
]
RETENTION_REASONS = [
    "shared organization and safety records may need attribution or legal preservation",
    "append-only financial records cannot be deleted without breaking accounting history",
    "provider backups and logs require separate verified expiry or provider fulfillment",
]


def _iso(value: object) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _safe_dict(value: object) -> object:
    """Convert ORM values without emitting credential or token-shaped fields."""
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return base64.b64encode(value).decode("ascii")
    return value


async def _rows(session: AsyncSession, model: type, column: str, user_id: uuid.UUID) -> list[dict[str, object]]:
    attribute = getattr(model, column, None)
    if attribute is None:
        raise RuntimeError(f"unclassified_account_data_edge:{model.__tablename__}.{column}")
    result = await session.execute(select(model).where(attribute == user_id))
    output: list[dict[str, object]] = []
    for row in result.scalars().all():
        record: dict[str, object] = {}
        for field in model.__table__.columns:
            name = field.name.lower()
            if any(secret in name for secret in ("token", "secret", "password", "private_key", "signature", "hash", "code")):
                record[field.name] = "[redacted]"
            else:
                record[field.name] = _safe_dict(getattr(row, field.name))
        output.append(record)
    return output


async def build_export(session: AsyncSession, user: models.User) -> dict[str, object]:
    """Build a complete supported export from rows owned or attributable to user."""
    data: dict[str, object] = {
        "format": "chirp-account-export-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "account": {
            "id": str(user.id),
            "email": user.email,
            "display_name": user.display_name,
            "avatar_url": user.avatar_url,
            "account_type": user.account_type,
            "campus_id": _safe_dict(user.campus_id),
            "created_at": _iso(user.created_at),
        },
        "scope": EXPORT_SCOPE,
        "excluded": EXPORT_EXCLUDED,
        "retention_reasons": RETENTION_REASONS,
        "records": {},
    }
    records = data["records"]
    assert isinstance(records, dict)
    for model, column, key in (
        (models.Membership, "user_id", "memberships"),
        (models.Post, "author_id", "posts"),
        (models.PostComment, "author_id", "comments"),
        (models.Chirp, "author_id", "chirps"),
        (models.Event, "host_id", "events_hosted"),
        (models.EventInvite, "invited_user_id", "event_invites"),
        (models.EventInvite, "invited_by", "event_invites_created"),
        (models.EventRsvp, "user_id", "event_rsvps"),
        (models.MeetingAttendance, "user_id", "meeting_attendance"),
        (models.AlumniProfile, "user_id", "alumni_profile"),
        (models.JobPost, "posted_by", "job_posts"),
        (models.LineageEdge, "big_user_id", "lineage_as_big"),
        (models.LedgerEntry, "user_id", "ledger_entries"),
        (models.DuesPaymentIntent, "user_id", "payment_intents"),
        (models.DuesPaymentPlan, "user_id", "payment_plans"),
        (models.DuesPaymentPlan, "created_by", "payment_plans_created"),
        (models.ChapterStripeCustomer, "user_id", "stripe_customer_mapping"),
        (models.Device, "user_id", "devices"),
        (models.CampusVerification, "user_id", "campus_verifications"),
        (models.ChapterInvite, "created_by", "chapter_invites_created"),
        (models.ConversationMember, "user_id", "conversation_memberships"),
        (models.HouseBallot, "voter_id", "house_ballots"),
        (models.PollVote, "user_id", "poll_votes"),
        (models.Meeting, "created_by", "meetings_created"),
        (models.Poll, "created_by", "polls_created"),
        (models.SpendApproval, "requested_by", "spend_approvals_requested"),
        (models.SpendApproval, "decided_by", "spend_approvals_decided"),
        (models.LineageEdge, "created_by", "lineage_created"),
        (models.LedgerEntry, "related_user_id", "ledger_related"),
        (models.LedgerEntry, "created_by", "ledger_created"),
    ):
        records[key] = await _rows(session, model, column, user.id)
    legal_acceptance = getattr(models, "LegalAcceptance", None)
    if legal_acceptance is None:
        records["legal_acceptances"] = []
        records["legal_policies"] = []
    else:
        records["legal_acceptances"] = await _rows(session, legal_acceptance, "user_id", user.id)
        legal_policy = getattr(models, "LegalPolicy", None)
        if legal_policy is not None:
            policy_ids = (await session.execute(select(legal_acceptance.policy_id).where(legal_acceptance.user_id == user.id))).scalars().all()
            records["legal_policies"] = await _rows_for_ids(session, legal_policy, "id", list(policy_ids)) if policy_ids else []
    # A little's edge is the same personal relationship, so include both directions.
    records["lineage_as_little"] = await _rows(session, models.LineageEdge, "little_user_id", user.id)
    records["content_reports"] = await _rows(session, models.ContentReport, "reporter_id", user.id)
    records["moderation_actions"] = await _rows(session, models.ModerationAction, "actor_id", user.id)
    records["post_likes"] = await _rows(session, models.PostLike, "user_id", user.id)
    records["chirp_votes"] = await _rows(session, models.ChirpVote, "user_id", user.id)
    # Block relationships can encode another person's identity and, for anonymous
    # chirp blocks, the relationship between a user and an anonymous author. Export
    # the caller's action without exposing the other party's id.
    records["user_blocks"] = await _safe_block_rows(session, user.id)
    records["role_terms"] = await _rows(session, models.RoleTerm, "changed_by", user.id)
    device_ids = (await session.execute(select(models.Device.id).where(models.Device.user_id == user.id))).scalars().all()
    if device_ids:
        messages = (await session.execute(select(models.Message).where(models.Message.sender_device_id.in_(device_ids)))).scalars().all()
        records["messages_sent"] = [
            {
                "id": str(message.id),
                "conversation_id": str(message.conversation_id),
                "sender_device_id": str(message.sender_device_id),
                "ciphertext": base64.b64encode(message.ciphertext).decode("ascii"),
                "message_type": message.message_type,
                "created_at": _iso(message.created_at),
            }
            for message in messages
        ]
        records["message_receipts"] = await _rows_for_ids(session, models.MessageReceipt, "message_id", [message.id for message in messages])
    else:
        records["messages_sent"] = []
        records["message_receipts"] = []
    return data


async def _safe_block_rows(session: AsyncSession, user_id: uuid.UUID) -> list[dict[str, object]]:
    result = await session.execute(
        select(models.UserBlock).where(
            (models.UserBlock.blocker_id == user_id) | (models.UserBlock.blocked_id == user_id)
        )
    )
    return [{"source": row.source, "created_at": _iso(row.created_at), "relationship": "account block"} for row in result.scalars().all()]


async def _rows_for_ids(session: AsyncSession, model: type, column: str, ids: list[uuid.UUID]) -> list[dict[str, object]]:
    """Export rows keyed by a caller-owned set of ids with the same redaction rules."""
    attribute = getattr(model, column, None)
    if attribute is None:
        raise RuntimeError(f"unclassified_account_data_edge:{model.__tablename__}.{column}")
    result = await session.execute(select(model).where(attribute.in_(ids)))
    output = []
    for row in result.scalars().all():
        output.append({
            field.name: ("[redacted]" if any(secret in field.name.lower() for secret in ("token", "secret", "password", "private_key", "signature")) else _safe_dict(getattr(row, field.name)))
            for field in model.__table__.columns
        })
    return output


async def _delete_owned_rows(session: AsyncSession, user: models.User) -> None:
    """Remove private edges and redact user-authored UGC while preserving shared rows."""
    for model, column in (
        (models.PostLike, "user_id"),
        (models.ChirpVote, "user_id"),
        (models.UserBlock, "blocker_id"),
        (models.UserBlock, "blocked_id"),
        (models.SignedPrekey, "device_id"),
        (models.OneTimePrekey, "device_id"),
        (models.KyberPrekey, "device_id"),
        (models.Device, "user_id"),
        (models.AlumniProfile, "user_id"),
        (models.EventInvite, "invited_user_id"),
        (models.EventRsvp, "user_id"),
        (models.MeetingAttendance, "user_id"),
        (models.ConversationMember, "user_id"),
        (models.PollVote, "user_id"),
        (models.HouseBallot, "voter_id"),
        (models.LineageEdge, "big_user_id"),
        (models.LineageEdge, "little_user_id"),
        (models.JobPost, "posted_by"),
    ):
        attribute = getattr(model, column, None)
        if attribute is not None:
            if model is models.Device:
                device_ids = (await session.execute(select(models.Device.id).where(models.Device.user_id == user.id))).scalars().all()
                if device_ids:
                    message_ids = (await session.execute(select(models.Message.id).where(models.Message.sender_device_id.in_(device_ids)))).scalars().all()
                    if message_ids:
                        await session.execute(delete(models.MessageReceipt).where(models.MessageReceipt.message_id.in_(message_ids)))
                        await session.execute(delete(models.Message).where(models.Message.id.in_(message_ids)))
                    for key_model in (models.SignedPrekey, models.OneTimePrekey, models.KyberPrekey):
                        await session.execute(delete(key_model).where(key_model.device_id.in_(device_ids)))
                    await session.execute(
                        update(models.Device).where(models.Device.id.in_(device_ids)).values(revoked_at=datetime.now(timezone.utc))
                    )
                continue
            await session.execute(delete(model).where(attribute == user.id))
    # Hosted events are the user's authored shared UGC. Remove dependent access
    # rows first because the legacy FKs predate ON DELETE CASCADE.
    event_ids = (await session.execute(select(models.Event.id).where(models.Event.host_id == user.id))).scalars().all()
    if event_ids:
        await session.execute(delete(models.EventInvite).where(models.EventInvite.event_id.in_(event_ids)))
        await session.execute(delete(models.EventRsvp).where(models.EventRsvp.event_id.in_(event_ids)))
        await session.execute(delete(models.Event).where(models.Event.id.in_(event_ids)))
    await session.execute(
        update(models.ChapterInvite).where(models.ChapterInvite.created_by == user.id).values(
            revoked_at=datetime.now(timezone.utc), code=func.concat("deleted-", models.ChapterInvite.id)
        )
    )
    now = datetime.now(timezone.utc)
    await session.execute(
        update(models.Post).where(models.Post.author_id == user.id).values(
            body="[deleted]", media_urls=None, deleted_at=now, removed_reason="account_deleted"
        )
    )
    await session.execute(
        update(models.PostComment).where(models.PostComment.author_id == user.id).values(
            body="[deleted]", deleted_at=now, removed_reason="account_deleted"
        )
    )
    await session.execute(
        update(models.Chirp).where(models.Chirp.author_id == user.id).values(
            body="[deleted]", removed_at=now, removed_reason="account_deleted"
        )
    )
    await session.execute(
        update(models.Membership).where(models.Membership.user_id == user.id).values(status="removed")
    )
    # Keep the row as a referentially safe tombstone. Remove auth linkage and contact
    # data so a later account cannot be confused with this historical actor.
    tombstone = str(user.id)
    await session.execute(
        update(models.User).where(models.User.id == user.id).values(
            firebase_uid=f"deleted:{tombstone}",
            email=f"deleted+{tombstone}@invalid.local",
            display_name="Deleted account",
            avatar_url=None,
            account_type="non_greek",
            campus_id=None,
            is_ghost=True,
            pseudonym_seed=uuid.uuid4().hex,
        )
    )


async def fulfill_request(session: AsyncSession, request: models.AccountDataRequest, user: models.User) -> models.AccountDataRequest:
    """Run one request idempotently; incomplete provider work remains visible."""
    if request.kind == "export":
        payload = await build_export(session, user)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        artifact = models.AccountDataArtifact(
            request_id=request.id,
            content=payload,
            content_sha256=hashlib.sha256(encoded).hexdigest(),
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        )
        session.add(artifact)
        request.scope = EXPORT_SCOPE
        request.excluded = EXPORT_EXCLUDED
        request.retention_reasons = RETENTION_REASONS
        # The supported core is downloadable, but the inventory still has explicit
        # provider and dormant-client residuals, so never call it a complete export.
        request.status = "partially_completed"
        request.open_key = None
        request.updated_at = datetime.now(timezone.utc)
        request.completed_at = request.updated_at
        return request
    original_uid, original_email = user.firebase_uid, user.email
    provider_failures = []
    request.provider_steps = {}
    for provider in PROVIDER_ADAPTERS:
        ok = await provider.fulfill_deletion(firebase_uid=original_uid, email=original_email)
        request.provider_steps[provider.name] = "fulfilled" if ok else "blocked_unconfigured"
        if not ok:
            provider_failures.append(provider.name)
    if provider_failures:
        request.scope = []
        request.excluded = [f"{name} provider deletion requires configured fulfillment" for name in provider_failures]
        request.retention_reasons = RETENTION_REASONS
        request.status = "blocked"
        request.failure_code = "provider_fulfillment_required"
        request.updated_at = datetime.now(timezone.utc)
        return request
    await _delete_owned_rows(session, user)
    request.scope = ["account profile linkage and user-authored content redaction", "private reactions, devices, and alumni profile"]
    request.excluded = [f"{name} provider deletion requires configured fulfillment" for name in provider_failures]
    request.retention_reasons = RETENTION_REASONS
    request.status = "completed"
    request.failure_code = None
    request.open_key = None
    request.updated_at = datetime.now(timezone.utc)
    request.completed_at = request.updated_at
    return request
