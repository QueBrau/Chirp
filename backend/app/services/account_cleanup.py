"""Local account cleanup with an explicit, conservative retention boundary."""
from datetime import datetime, timezone
import uuid

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import models

RETENTION_REASONS = {
    "organization_history": "membership, role, lineage, and shared organization history remains attributed to Deleted account",
    "financial_history": "append-only ledger and payment history remains for accounting and disputes",
    "safety_legal": "reports, moderation, legal acceptance, and safety records remain for audit and holds",
    "message_history": "encrypted message history remains for other participants; server cannot decrypt it",
    "provider_retention": "provider, backup, log, and unresolved media retention requires provider confirmation",
}
PRIVATE_COVERAGE = ("alumni_profiles", "campus_verifications", "account_data_artifacts", "job_posts", "post_likes", "chirp_votes", "poll_votes", "own_user_blocks", "conversation_memberships_left", "devices_revoke")
RETAINED_COVERAGE = (
    "memberships", "role_terms", "lineage_edges", "ledger_entries", "dues_payment_plans",
    "dues_plan_installments", "chapter_stripe_customers", "legal_acceptances",
    "organization_authority_acceptances", "moderation_actions", "content_reports",
    "messages_and_conversation_history", "incoming_user_blocks", "provider_backups_logs",
)


async def cleanup_local_account(session: AsyncSession, user_id: uuid.UUID) -> dict[str, object]:
    """Apply only caller-owned cleanup; shared/history rows are deliberately retained."""
    now = datetime.now(timezone.utc)
    device_ids = (await session.scalars(
        models.Device.__table__.select().with_only_columns(models.Device.id).where(models.Device.user_id == user_id)
    )).all()
    await session.execute(delete(models.AlumniProfile).where(models.AlumniProfile.user_id == user_id))
    await session.execute(delete(models.CampusVerification).where(models.CampusVerification.user_id == user_id))
    await session.execute(delete(models.AccountDataArtifact).where(
        models.AccountDataArtifact.request_id.in_(models.AccountDataRequest.__table__.select().with_only_columns(models.AccountDataRequest.id).where(models.AccountDataRequest.user_id == user_id))
    ))
    await session.execute(update(models.Membership).where(models.Membership.user_id == user_id, models.Membership.status == "active").values(status="removed"))
    await session.execute(update(models.RoleTerm).where(models.RoleTerm.membership_id.in_(models.Membership.__table__.select().with_only_columns(models.Membership.id).where(models.Membership.user_id == user_id)), models.RoleTerm.ended_at.is_(None)).values(ended_at=now))
    await session.execute(update(models.ChapterInvite).where(models.ChapterInvite.created_by == user_id).values(revoked_at=now))
    await session.execute(update(models.Post).where(models.Post.author_id == user_id).values(body="[deleted]", media_urls=[], deleted_at=now, removed_reason="account_deleted"))
    await session.execute(update(models.PostComment).where(models.PostComment.author_id == user_id).values(body="[deleted]", deleted_at=now, removed_reason="account_deleted"))
    await session.execute(update(models.Chirp).where(models.Chirp.author_id == user_id).values(body="[deleted]", removed_at=now, removed_reason="account_deleted"))
    await session.execute(delete(models.JobPost).where(models.JobPost.posted_by == user_id))
    await session.execute(delete(models.PostLike).where(models.PostLike.user_id == user_id))
    await session.execute(delete(models.ChirpVote).where(models.ChirpVote.user_id == user_id))
    await session.execute(delete(models.PollVote).where(models.PollVote.user_id == user_id))
    await session.execute(delete(models.UserBlock).where(models.UserBlock.blocker_id == user_id))
    await session.execute(update(models.ConversationMember).where(
        models.ConversationMember.user_id == user_id,
        models.ConversationMember.left_at.is_(None),
    ).values(left_at=now))
    if device_ids:
        await session.execute(update(models.Device).where(models.Device.id.in_(device_ids)).values(revoked_at=now))
    return {"deleted_private": list(PRIVATE_COVERAGE), "redacted_authored": ["posts", "post_comments", "chirps"], "revoked": ["devices"], "retained": RETENTION_REASONS, "coverage": {"private": list(PRIVATE_COVERAGE), "retained": list(RETAINED_COVERAGE)}}
