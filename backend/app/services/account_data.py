"""Scoped account export and deletion fulfillment with explicit provider residuals."""

from __future__ import annotations

import hashlib
import json
import base64
import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models


async def purge_expired_export_artifacts(session: AsyncSession) -> int:
    """Delete expired export copies. Scheduling this helper is not yet implemented."""
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
    "organization authority declarations made by you, including the linked provider account identifier",
]
EXPORT_EXCLUDED = [
    "provider-held authentication, payment, email, storage, logging, and backup copies",
    "message plaintext that the server never stores",
    "prekey byte material and dormant client-only SQLite data",
    "installment details, delivery-outbox records, and role history changed by other officers",
]
RETENTION_REASONS = [
    "shared organization and safety records may need attribution or legal preservation",
    "append-only financial records cannot be deleted without breaking accounting history",
    "provider backups and logs require separate verified expiry or provider fulfillment",
    "organization authority attestations preserve who accepted payment or organization responsibility",
]

# Stable export allowlist. New columns must be reviewed and added deliberately;
# exporting every ORM column would eventually leak a newly-added credential or
# provider identifier into a personal artifact.
EXPORT_FIELDS: dict[str, tuple[str, ...]] = {
    "memberships": ("id", "chapter_id", "role", "status", "pledge_class", "joined_at"),
    "posts": ("id", "chapter_id", "campus_id", "body", "media_urls", "audience", "post_type", "duration_sec", "created_at", "deleted_at", "removed_reason"),
    "post_comments": ("id", "post_id", "body", "created_at", "deleted_at", "removed_reason"),
    "chirps": ("id", "campus_id", "body", "score", "created_at", "removed_at", "removed_reason"),
    "events": ("id", "chapter_id", "title", "description", "location", "visibility", "starts_at", "ends_at", "created_at", "canceled_at"),
    "event_invites": ("event_id", "created_at"),
    "event_rsvps": ("event_id", "status", "created_at"),
    "meeting_attendance": ("meeting_id", "status"),
    "alumni_profiles": ("user_id", "grad_year", "company", "title", "industry", "location", "linkedin_url", "open_to_mentoring"),
    "job_posts": ("id", "chapter_id", "title", "company", "location", "description", "apply_url", "expires_at", "created_at"),
    "lineage_edges": ("id", "chapter_id", "big_user_id", "little_user_id", "family_id", "pledge_class", "confirmed_by_little", "created_at"),
    "ledger_entries": ("id", "chapter_id", "entry_type", "amount_cents", "category", "description", "related_user_id", "dues_cycle_id", "corrects_entry_id", "created_at"),
    "dues_payment_intents": ("id", "chapter_id", "dues_cycle_id", "rail", "status", "amount_cents", "currency", "created_at", "updated_at"),
    "dues_payment_plans": ("id", "chapter_id", "dues_cycle_id", "total_cents", "installment_count", "status", "note", "created_at"),
    "chapter_stripe_customers": ("chapter_id", "created_at"),
    "devices": ("id", "device_label", "registration_id", "identity_key", "created_at", "revoked_at"),
    "campus_verifications": ("id", "campus_id", "edu_email", "sent_at", "expires_at", "consumed_at", "attempts"),
    "conversation_members": ("conversation_id", "joined_at", "left_at"),
    "house_ballots": ("campus_id", "week_start", "touse_chapter_id", "bouse_chapter_id", "created_at", "updated_at"),
    "poll_votes": ("poll_id", "option_id", "created_at"),
    "meetings": ("id", "chapter_id", "title", "meeting_date", "minutes_md", "created_at"),
    "polls": ("id", "chapter_id", "meeting_id", "question", "status", "created_at", "closed_at"),
    "spend_approvals": ("id", "chapter_id", "amount_cents", "description", "status", "decided_at", "created_at"),
    "content_reports": ("id", "target_type", "target_id", "reason", "status", "created_at"),
    "moderation_actions": ("id", "action", "target_type", "target_id", "reason", "created_at"),
    "post_likes": ("post_id", "created_at"),
    "chirp_votes": ("chirp_id", "value"),
    "role_terms": ("id", "membership_id", "role", "started_at", "ended_at"),
    "chapter_invites": ("id", "chapter_id", "role", "expires_at", "max_uses", "uses", "revoked_at"),
    "legal_acceptances": ("id", "user_id", "policy_id", "accepted_at", "age_declaration", "guardian_permission_confirmed", "source"),
    "legal_policies": ("id", "policy_key", "version", "effective_at", "is_current"),
    "organization_authority_acceptances": (
        "id", "user_id", "chapter_id", "membership_id", "role_term_id", "role",
        "purpose", "policy_version", "stripe_account_id", "accepted_at",
    ),
}


def _iso(value: object) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _safe_dict(value: object) -> object:
    """Convert ORM values without emitting credential or token-shaped fields."""
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
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
    fields = EXPORT_FIELDS.get(model.__tablename__)
    if fields is None:
        raise RuntimeError(f"unclassified_account_data_table:{model.__tablename__}")
    for field_name in fields:
        if getattr(model, field_name, None) is None:
            raise RuntimeError(f"unclassified_account_data_edge:{model.__tablename__}.{field_name}")
    for row in result.scalars().all():
        record: dict[str, object] = {}
        for field_name in fields:
            name = field_name.lower()
            if any(secret in name for secret in ("token", "secret", "password", "private_key", "signature", "hash", "code")):
                record[field_name] = "[redacted]"
            else:
                record[field_name] = _safe_dict(getattr(row, field_name))
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
        (models.LedgerEntry, "related_user_id", "ledger_entries"),
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
    legal_acceptance = models.LegalAcceptance
    legal_policy = models.LegalPolicy
    records["legal_acceptances"] = await _rows(session, legal_acceptance, "user_id", user.id)
    policy_ids = (await session.execute(select(legal_acceptance.policy_id).where(legal_acceptance.user_id == user.id))).scalars().all()
    records["legal_policies"] = await _rows_for_ids(session, legal_policy, "id", list(policy_ids)) if policy_ids else []
    records["organization_authority_acceptances"] = await _rows(
        session, models.OrganizationAuthorityAcceptance, "user_id", user.id,
    )
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
        own_receipts = (await session.execute(
            select(models.MessageReceipt).where(models.MessageReceipt.device_id.in_(device_ids))
        )).scalars().all()
        records["message_receipts"] = [
            {"message_id": str(receipt.message_id), "delivered_at": _iso(receipt.delivered_at)}
            for receipt in own_receipts
        ]
    else:
        records["messages_sent"] = []
        records["message_receipts"] = []
    return data


async def _safe_block_rows(session: AsyncSession, user_id: uuid.UUID) -> list[dict[str, object]]:
    result = await session.execute(
        select(models.UserBlock).where(models.UserBlock.blocker_id == user_id)
    )
    return [{"source": row.source, "created_at": _iso(row.created_at), "relationship": "account block"} for row in result.scalars().all()]


async def _rows_for_ids(session: AsyncSession, model: type, column: str, ids: list[uuid.UUID]) -> list[dict[str, object]]:
    """Export rows keyed by a caller-owned set of ids with the same redaction rules."""
    attribute = getattr(model, column, None)
    if attribute is None:
        raise RuntimeError(f"unclassified_account_data_edge:{model.__tablename__}.{column}")
    fields = EXPORT_FIELDS.get(model.__tablename__)
    if fields is None:
        raise RuntimeError(f"unclassified_account_data_table:{model.__tablename__}")
    for field_name in fields:
        if getattr(model, field_name, None) is None:
            raise RuntimeError(f"unclassified_account_data_edge:{model.__tablename__}.{field_name}")
    result = await session.execute(select(model).where(attribute.in_(ids)))
    output = []
    for row in result.scalars().all():
        output.append({
            field_name: ("[redacted]" if any(secret in field_name.lower() for secret in ("token", "secret", "password", "private_key", "signature", "hash", "code")) else _safe_dict(getattr(row, field_name)))
            for field_name in fields
        })
    return output


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
    # Destructive fulfillment is deliberately disabled at this boundary. Provider
    # console work, retry journals, and irreversible-step receipts must be durable
    # before this executor can be enabled; a boolean adapter is insufficient proof.
    request.provider_steps = {"manual_processing": "required"}
    request.scope = []
    request.excluded = [
        "account deletion requires verified Firebase/Auth provider removal",
        "Stripe customer and payment-provider deletion requires provider confirmation",
        "email, object-storage, logging, and backup copies require separate operator steps",
    ]
    request.retention_reasons = RETENTION_REASONS
    request.status = "blocked"
    request.failure_code = "manual_processing_required"
    request.updated_at = datetime.now(timezone.utc)
    request.completed_at = None
    return request
