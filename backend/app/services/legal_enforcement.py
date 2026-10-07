"""Shared server-side legal acceptance gate for HTTP and WebSocket sessions."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models


async def current_legal_status(session: AsyncSession, user_id) -> str:
    """Return whether a user accepted exactly the current Terms and Privacy rows.

    The query is deliberately fail-closed: missing, malformed, duplicate, or
    incomplete current policy configuration returns ``False``.  Callers map
    that result to their transport-specific rejection without retaining the
    database session.
    """
    policies = (await session.execute(
        select(models.LegalPolicy).where(models.LegalPolicy.is_current.is_(True))
    )).scalars().all()
    if len(policies) != 2 or {policy.policy_key for policy in policies} != {"terms", "privacy"}:
        return "unavailable"
    accepted = set((await session.execute(
        select(models.LegalAcceptance.policy_id).where(models.LegalAcceptance.user_id == user_id)
    )).scalars().all())
    return "accepted" if all(policy.id in accepted for policy in policies) else "required"


async def has_current_legal_acceptance(session: AsyncSession, user_id) -> bool:
    return await current_legal_status(session, user_id) == "accepted"
