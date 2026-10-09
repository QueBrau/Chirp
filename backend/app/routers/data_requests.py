"""Authenticated account export and deletion intake/status routes."""

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app import models
from app.config import get_settings
from app.db import get_session
from app.middleware.auth import get_current_user_for_privacy, get_current_user_for_privacy_status
from app.schemas.data_requests import DataRequestCreate, DataRequestOut
from app.services.account_data import fulfill_request
from app.services.account_fulfillment import prepare_deletion_plan

router = APIRouter(tags=["account-data"])


def _out(row: models.AccountDataRequest, artifact: models.AccountDataArtifact | datetime | None = None) -> DataRequestOut:
    expires_at = artifact.expires_at if isinstance(artifact, models.AccountDataArtifact) else artifact
    return DataRequestOut(
        id=row.id,
        kind=row.kind,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
        # Deliberately null: downloads use the authenticated route below, so a URL
        # copied from a response cannot grant access to another account.
        download_url=None,
        expires_at=expires_at,
        scope=row.scope or [],
        excluded=row.excluded or [],
        retention_reasons=row.retention_reasons or [],
        provider_steps=row.provider_steps or {},
        failure_code=row.failure_code,
    )


async def _owned(
    request_id: uuid.UUID, user: models.User, session: AsyncSession,
) -> tuple[models.AccountDataRequest, models.AccountDataArtifact | None]:
    row = (await session.execute(
        select(models.AccountDataRequest).where(
            models.AccountDataRequest.id == request_id,
            models.AccountDataRequest.user_id == user.id,
        )
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="data_request_not_found")
    artifact = (await session.execute(
        select(models.AccountDataArtifact)
        .options(load_only(models.AccountDataArtifact.id, models.AccountDataArtifact.request_id, models.AccountDataArtifact.expires_at))
        .where(models.AccountDataArtifact.request_id == row.id)
    )).scalar_one_or_none()
    return row, artifact


@router.get("/me/data-requests", response_model=list[DataRequestOut])
async def list_data_requests(
    user: models.User = Depends(get_current_user_for_privacy_status),
    session: AsyncSession = Depends(get_session),
) -> list[DataRequestOut]:
    rows = (await session.execute(
        select(models.AccountDataRequest)
        .where(models.AccountDataRequest.user_id == user.id)
        .order_by(models.AccountDataRequest.created_at.desc())
        .limit(100)
    )).scalars().all()
    artifacts = (await session.execute(
        select(models.AccountDataArtifact.request_id, models.AccountDataArtifact.expires_at).join(
            models.AccountDataRequest,
            models.AccountDataArtifact.request_id == models.AccountDataRequest.id,
        ).where(models.AccountDataRequest.user_id == user.id)
    )).all()
    by_request = {request_id: expires_at for request_id, expires_at in artifacts}
    return [_out(row, by_request.get(row.id)) for row in rows]


@router.post("/me/data-requests", response_model=DataRequestOut, status_code=201)
async def create_data_request(
    body: DataRequestCreate,
    user: models.User = Depends(get_current_user_for_privacy),
    session: AsyncSession = Depends(get_session),
) -> DataRequestOut:
    # Serialize the caller's request budget and pending-request reuse across tabs/devices.
    user = (await session.execute(
        select(models.User).where(models.User.id == user.id).with_for_update()
    )).scalar_one()
    existing = (await session.execute(
        select(models.AccountDataRequest).where(
            models.AccountDataRequest.user_id == user.id,
            models.AccountDataRequest.kind == body.kind,
            models.AccountDataRequest.open_key == "open",
        ).order_by(models.AccountDataRequest.created_at.desc())
    )).scalars().first()
    if existing is not None:
        artifact = (await session.execute(
            select(models.AccountDataArtifact).where(models.AccountDataArtifact.request_id == existing.id)
        )).scalar_one_or_none()
        return _out(existing, artifact)
    daily_limit = get_settings().account_data_request_daily_limit
    recent_count = (await session.execute(
        select(func.count()).select_from(models.AccountDataRequest).where(
            models.AccountDataRequest.user_id == user.id,
            models.AccountDataRequest.created_at >= datetime.now(timezone.utc) - timedelta(days=1),
        )
    )).scalar_one()
    if recent_count >= daily_limit:
        raise HTTPException(status_code=429, detail="data_request_rate_limited")
    row = models.AccountDataRequest(user_id=user.id, kind=body.kind, open_key="open")
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        existing = (await session.execute(
            select(models.AccountDataRequest).where(
                models.AccountDataRequest.user_id == user.id,
                models.AccountDataRequest.kind == body.kind,
                models.AccountDataRequest.open_key == "open",
            ).order_by(models.AccountDataRequest.created_at.desc())
        )).scalars().first()
        if existing is None:
            raise HTTPException(status_code=409, detail="request_already_open")
        return _out(existing)
    if row.kind == "deletion":
        await prepare_deletion_plan(session, row)
    await fulfill_request(session, row, user)
    await session.commit()
    artifact = (await session.execute(
        select(models.AccountDataArtifact).where(models.AccountDataArtifact.request_id == row.id)
    )).scalar_one_or_none()
    return _out(row, artifact)


@router.get("/me/data-requests/{request_id}", response_model=DataRequestOut)
async def get_data_request(
    request_id: uuid.UUID,
    user: models.User = Depends(get_current_user_for_privacy_status),
    session: AsyncSession = Depends(get_session),
) -> DataRequestOut:
    row, artifact = await _owned(request_id, user, session)
    return _out(row, artifact)


@router.get("/me/data-requests/{request_id}/download")
async def download_data_request(
    request_id: uuid.UUID,
    user: models.User = Depends(get_current_user_for_privacy),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    row, _artifact = await _owned(request_id, user, session)
    artifact = (await session.execute(
        select(models.AccountDataArtifact).where(models.AccountDataArtifact.request_id == row.id)
    )).scalar_one_or_none()
    if row.kind != "export" or row.status not in {"ready", "partially_completed"} or artifact is None:
        raise HTTPException(status_code=409, detail="export_not_ready")
    if artifact.expires_at is not None and artifact.expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="export_expired")
    return JSONResponse(
        content=artifact.content,
        headers={
            "Content-Disposition": f'attachment; filename="chirp-account-export-{row.id}.json"',
            "Cache-Control": "private, no-store",
        },
    )
