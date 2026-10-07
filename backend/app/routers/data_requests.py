"""Authenticated account export and deletion intake/status routes."""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.db import get_session
from app.middleware.auth import get_current_user_for_privacy, get_current_user_for_privacy_status
from app.schemas.data_requests import DataRequestCreate, DataRequestOut
from app.services.account_data import fulfill_request

router = APIRouter(tags=["account-data"])


def _out(row: models.AccountDataRequest, artifact: models.AccountDataArtifact | None = None) -> DataRequestOut:
    expires_at = artifact.expires_at if artifact else None
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
        select(models.AccountDataArtifact).where(models.AccountDataArtifact.request_id == row.id)
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
    )).scalars().all()
    artifacts = (await session.execute(
        select(models.AccountDataArtifact).join(
            models.AccountDataRequest,
            models.AccountDataArtifact.request_id == models.AccountDataRequest.id,
        ).where(models.AccountDataRequest.user_id == user.id)
    )).scalars().all()
    by_request = {artifact.request_id: artifact for artifact in artifacts}
    return [_out(row, by_request.get(row.id)) for row in rows]


@router.post("/me/data-requests", response_model=DataRequestOut, status_code=201)
async def create_data_request(
    body: DataRequestCreate,
    user: models.User = Depends(get_current_user_for_privacy),
    session: AsyncSession = Depends(get_session),
) -> DataRequestOut:
    row = models.AccountDataRequest(user_id=user.id, kind=body.kind, open_key="open")
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="request_already_open")
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
    row, artifact = await _owned(request_id, user, session)
    if row.kind != "export" or row.status not in {"ready", "partially_completed"} or artifact is None:
        raise HTTPException(status_code=409, detail="export_not_ready")
    if artifact.expires_at is not None and artifact.expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="export_expired")
    return JSONResponse(
        content=artifact.content,
        headers={"Content-Disposition": f'attachment; filename="chirp-account-export-{row.id}.json"'},
    )


@router.post("/me/data-requests/{request_id}/retry", response_model=DataRequestOut)
async def retry_data_request(
    request_id: uuid.UUID,
    user: models.User = Depends(get_current_user_for_privacy),
    session: AsyncSession = Depends(get_session),
) -> DataRequestOut:
    """Retry only a blocked/failed request; ownership and fresh auth are rechecked."""
    row, _artifact = await _owned(request_id, user, session)
    if row.status not in {"blocked", "failed"}:
        raise HTTPException(status_code=409, detail="request_not_retryable")
    await fulfill_request(session, row, user)
    await session.commit()
    artifact = (await session.execute(
        select(models.AccountDataArtifact).where(models.AccountDataArtifact.request_id == row.id)
    )).scalar_one_or_none()
    return _out(row, artifact)
