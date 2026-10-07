"""Record declarations without granting roles or replacing Stripe's agreement."""
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.schemas.authority import OrganizationAuthorityInput, PaymentAuthorityInput

from app import models
from app.config import get_settings
from app.schemas.authority import PaymentAuthorityContext

ORGANIZATION_POLICY_VERSION = "2026-10-06"
ORGANIZATION_POLICY_URL = "https://chirpsocials.com/payments"


def validate_declaration(declaration: OrganizationAuthorityInput | None) -> None:
    if declaration is None:
        if get_settings().legal_enforcement_enabled:
            raise HTTPException(428, "organization_authority_required")
        return
    if declaration.policy_version != ORGANIZATION_POLICY_VERSION:
        raise HTTPException(409, "organization_policy_changed")


async def payment_context(session: AsyncSession, user: models.User, chapter: models.Chapter) -> PaymentAuthorityContext:
    # Refresh and lock the binding only inside this short SQL transaction.
    # The onboarding route commits before every provider call.
    await session.refresh(chapter, with_for_update=True)
    membership = (await session.execute(select(models.Membership).where(
        models.Membership.user_id == user.id,
        models.Membership.chapter_id == chapter.id,
        models.Membership.status == "active",
    ).with_for_update())).scalar_one_or_none()
    if membership is None or membership.role not in {"president", "treasurer"}:
        raise HTTPException(403, "insufficient_role")
    term = (await session.execute(select(models.RoleTerm).where(
        models.RoleTerm.membership_id == membership.id,
        models.RoleTerm.ended_at.is_(None),
        models.RoleTerm.role == membership.role,
    ))).scalar_one_or_none()
    if term is None:
        raise HTTPException(503, "authority_context_unavailable")
    return PaymentAuthorityContext(
        policy_version=ORGANIZATION_POLICY_VERSION, chapter_id=chapter.id,
        org_name=chapter.org_name, membership_id=membership.id,
        role_term_id=term.id, role=membership.role,
        stripe_account_id=chapter.stripe_account_id,
    )


async def check_payment_declaration(
    session: AsyncSession, user: models.User, chapter: models.Chapter,
    declaration: PaymentAuthorityInput | None,
) -> PaymentAuthorityContext | None:
    validate_declaration(declaration)
    if declaration is None:
        return None
    context = await payment_context(session, user, chapter)
    for field in ("membership_id", "role_term_id", "role", "stripe_account_id"):
        if getattr(context, field) != getattr(declaration, field):
            raise HTTPException(409, "organization_authority_changed")
    return context


async def record_authority(session, *, user_id, chapter_id, membership_id,
                           role_term_id, role, purpose, stripe_account_id=None):
    session.add(models.OrganizationAuthorityAcceptance(
        user_id=user_id, chapter_id=chapter_id, membership_id=membership_id,
        role_term_id=role_term_id, role=role, purpose=purpose,
        policy_version=ORGANIZATION_POLICY_VERSION, stripe_account_id=stripe_account_id,
    ))
    await session.flush()
