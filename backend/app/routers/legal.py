"""Server-side policy status and acceptance endpoints (c438)."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app import models
from app.db import get_session
from app.middleware.auth import get_current_user_without_legal_gate
from sqlalchemy.dialects.postgresql import insert
from app.schemas.legal import LegalAcceptanceCreate, LegalStatusOut, PolicyOut

router = APIRouter(tags=["legal"])

async def _status(session: AsyncSession, user: models.User | None) -> LegalStatusOut:
    policies = (await session.execute(select(models.LegalPolicy).where(models.LegalPolicy.is_current.is_(True)).order_by(models.LegalPolicy.policy_key))).scalars().all()
    if {p.policy_key for p in policies} != {"terms", "privacy"} or len(policies) != 2:
        raise HTTPException(status_code=503, detail="legal_policy_unavailable")
    accepted: set = set()
    acceptance_count = 0
    if user is not None:
        acceptance_rows = (await session.execute(select(models.LegalAcceptance.policy_id).where(models.LegalAcceptance.user_id == user.id))).scalars().all()
        accepted = set(acceptance_rows)
        acceptance_count = len(acceptance_rows)
    required = any(p.id not in accepted for p in policies)
    return LegalStatusOut(
        required=required,
        material_change=required and acceptance_count > 0,
        policies=[PolicyOut(key=p.policy_key, version=p.version, effective_at=p.effective_at) for p in policies],
        accepted_policy_ids=list(accepted),
    )

@router.get("/legal/policies", response_model=LegalStatusOut)
async def current_policies(session: AsyncSession = Depends(get_session)) -> LegalStatusOut:
    return await _status(session, None)

@router.get("/auth/legal-status", response_model=LegalStatusOut)
async def legal_status(user: models.User = Depends(get_current_user_without_legal_gate), session: AsyncSession = Depends(get_session)) -> LegalStatusOut:
    return await _status(session, user)

@router.post("/auth/legal-acceptance", response_model=LegalStatusOut)
async def accept_legal(body: LegalAcceptanceCreate, user: models.User = Depends(get_current_user_without_legal_gate), session: AsyncSession = Depends(get_session)) -> LegalStatusOut:
    current = (await session.execute(select(models.LegalPolicy).where(models.LegalPolicy.is_current.is_(True)))).scalars().all()
    if {p.policy_key for p in current} != {"terms", "privacy"} or len(current) != 2:
        raise HTTPException(status_code=503, detail="legal_policy_unavailable")
    versions = {p.policy_key: p.version for p in current}
    if body.terms_version != versions.get("terms") or body.privacy_version != versions.get("privacy"):
        raise HTTPException(status_code=409, detail="legal_policy_changed")
    rows = [
        {"user_id": user.id, "policy_id": policy.id, "age_declaration": body.age_declaration,
         "guardian_permission_confirmed": body.guardian_permission_confirmed, "source": body.source}
        for policy in current
    ]
    await session.execute(insert(models.LegalAcceptance).values(rows).on_conflict_do_nothing(constraint="uq_legal_acceptance_user_policy"))
    await session.commit()
    return await _status(session, user)
