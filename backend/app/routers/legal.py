"""Server-side policy status and acceptance endpoints (c438)."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app import models
from app.db import get_session
from app.middleware.auth import get_current_user
from app.schemas.legal import LegalAcceptanceCreate, LegalStatusOut, PolicyOut

router = APIRouter(tags=["legal"])

async def _status(session: AsyncSession, user: models.User | None) -> LegalStatusOut:
    policies = (await session.execute(select(models.LegalPolicy).where(models.LegalPolicy.is_current.is_(True)).order_by(models.LegalPolicy.policy_key))).scalars().all()
    accepted: set = set()
    if user is not None:
        accepted = set((await session.execute(select(models.LegalAcceptance.policy_id).where(models.LegalAcceptance.user_id == user.id))).scalars().all())
    return LegalStatusOut(
        required=any(p.id not in accepted for p in policies),
        policies=[PolicyOut(key=p.policy_key, version=p.version, effective_at=p.effective_at) for p in policies],
        accepted_policy_ids=list(accepted),
    )

@router.get("/legal/policies", response_model=LegalStatusOut)
async def current_policies(session: AsyncSession = Depends(get_session)) -> LegalStatusOut:
    return await _status(session, None)

@router.get("/auth/legal-status", response_model=LegalStatusOut)
async def legal_status(user: models.User = Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> LegalStatusOut:
    return await _status(session, user)

@router.post("/auth/legal-acceptance", response_model=LegalStatusOut)
async def accept_legal(body: LegalAcceptanceCreate, user: models.User = Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> LegalStatusOut:
    current = (await session.execute(select(models.LegalPolicy).where(models.LegalPolicy.is_current.is_(True)))).scalars().all()
    versions = {p.policy_key: p.version for p in current}
    if body.terms_version != versions.get("terms") or body.privacy_version != versions.get("privacy"):
        raise HTTPException(status_code=409, detail="legal_policy_changed")
    for policy in current:
        existing = await session.scalar(select(models.LegalAcceptance).where(models.LegalAcceptance.user_id == user.id, models.LegalAcceptance.policy_id == policy.id))
        if existing is None:
            session.add(models.LegalAcceptance(user_id=user.id, policy_id=policy.id, age_declaration=body.age_declaration, guardian_permission_confirmed=body.guardian_permission_confirmed, source=body.source))
    await session.commit()
    return await _status(session, user)
