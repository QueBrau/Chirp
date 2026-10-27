"""Authenticated account export and deletion intake/status routes."""

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app import models
from app.config import get_settings
from app.db import get_session
from app.middleware.auth import (
    get_current_user_for_privacy,
    get_current_user_for_privacy_status,
    get_current_user_for_privacy_write,
    get_fresh_verified_uid,
)
from app.schemas.data_requests import (
    DataRequestCreate, DataRequestOut, PostDeletionCleanupOut,
    DeletionCompletionCheck, DeletionCompletionOut, DeletionCompletionTokenOut,
)
from app.services.account_data import fulfill_request
from app.services.account_fulfillment import prepare_deletion_plan
from app.services.account_deletion_completion import (
    CompletionTokenError, is_deletion_completed, mint_completion_token, verify_completion_token,
)

_COMPLETION_HEADERS = {"Cache-Control": "private, no-store"}


class _CompletionRoute(APIRoute):
    """Keep capability errors private without changing ordinary request routes."""

    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        handler = super().get_route_handler()
        if self.name not in {"issue_deletion_completion_token", "check_deletion_completion"}:
            return handler

        async def private_handler(request: Request) -> Response:
            try:
                response = await handler(request)
            except RequestValidationError:
                # FastAPI's default 422 includes the rejected input even when a
                # Pydantic model hides it in str(error). Never echo a capability.
                return JSONResponse(
                    {"detail": "invalid_deletion_completion_request"}, status_code=422,
                    headers=_COMPLETION_HEADERS,
                )
            except HTTPException as exc:
                exc.headers = {**(exc.headers or {}), **_COMPLETION_HEADERS}
                raise
            response.headers.update(_COMPLETION_HEADERS)
            return response

        return private_handler


router = APIRouter(tags=["account-data"], route_class=_CompletionRoute)


@router.post("/me/data-requests/{request_id}/completion-token", response_model=DeletionCompletionTokenOut)
async def issue_deletion_completion_token(
    request_id: uuid.UUID,
    response: Response,
    user: models.User = Depends(get_current_user_for_privacy),
    session: AsyncSession = Depends(get_session),
) -> DeletionCompletionTokenOut:
    """Give the owner a narrow status capability before Firebase deletion."""
    row, _artifact = await _owned(request_id, user, session)
    if row.kind != "deletion" or row.status in {"canceled", "failed", "completed"}:
        raise HTTPException(409, "deletion_completion_unavailable", headers=_COMPLETION_HEADERS)
    try:
        token, claims = mint_completion_token(row.id, user.id)
    except CompletionTokenError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=_COMPLETION_HEADERS) from None
    response.headers.update(_COMPLETION_HEADERS)
    return DeletionCompletionTokenOut(
        token=token, user_id=claims.user_id, request_id=claims.request_id,
        expires_at=claims.expires_at,
    )


@router.post("/account-deletion/completion", response_model=DeletionCompletionOut)
async def check_deletion_completion(
    body: DeletionCompletionCheck,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> DeletionCompletionOut:
    """Check only a signed request capability; Firebase may no longer exist."""
    try:
        claims = verify_completion_token(body.token)
    except CompletionTokenError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=_COMPLETION_HEADERS) from None
    completed = await is_deletion_completed(session, claims)
    response.headers.update(_COMPLETION_HEADERS)
    return DeletionCompletionOut(
        request_id=claims.request_id, user_id=claims.user_id, completed=completed,
    )


async def get_data_request_user(
    body: DataRequestCreate,
    uid: str = Depends(get_fresh_verified_uid),
    session: AsyncSession = Depends(get_session),
) -> models.User:
    """Hold-gate exports while keeping deletion intake available as control plane."""
    if body.kind == "export":
        return await get_current_user_for_privacy_write(uid=uid, session=session)
    return await get_current_user_for_privacy(uid=uid, session=session)


def _out(
    row: models.AccountDataRequest,
    artifact: models.AccountDataArtifact | datetime | None = None,
    post_deletion_cleanup: list[PostDeletionCleanupOut] | None = None,
) -> DataRequestOut:
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
        post_deletion_cleanup=post_deletion_cleanup or [],
        failure_code=row.failure_code,
    )


async def _cleanup_statuses(
    session: AsyncSession, request_ids: list[uuid.UUID],
) -> dict[uuid.UUID, list[PostDeletionCleanupOut]]:
    if not get_settings().account_deletion_deferred_cleanup_enabled or not request_ids:
        return {}
    rows = (await session.execute(
        select(models.AccountPostDeletionCleanup.request_id,
               models.AccountPostDeletionCleanup.category,
               models.AccountPostDeletionCleanup.state)
        .where(models.AccountPostDeletionCleanup.request_id.in_(request_ids))
        .order_by(models.AccountPostDeletionCleanup.category)
    )).all()
    result: dict[uuid.UUID, list[PostDeletionCleanupOut]] = {}
    for request_id, category, state in rows:
        result.setdefault(request_id, []).append(
            PostDeletionCleanupOut(category=category, state=state)
        )
    return result


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
    cleanup = await _cleanup_statuses(session, [row.id for row in rows])
    return [_out(row, by_request.get(row.id), cleanup.get(row.id)) for row in rows]


@router.post("/me/data-requests", response_model=DataRequestOut, status_code=201)
async def create_data_request(
    body: DataRequestCreate,
    user: models.User = Depends(get_data_request_user),
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
    await session.flush()
    artifact = (await session.execute(
        select(models.AccountDataArtifact).where(models.AccountDataArtifact.request_id == row.id)
    )).scalar_one_or_none()
    response = _out(row, artifact)
    await session.commit()
    return response


@router.get("/me/data-requests/{request_id}", response_model=DataRequestOut)
async def get_data_request(
    request_id: uuid.UUID,
    user: models.User = Depends(get_current_user_for_privacy_status),
    session: AsyncSession = Depends(get_session),
) -> DataRequestOut:
    row, artifact = await _owned(request_id, user, session)
    cleanup = await _cleanup_statuses(session, [row.id])
    return _out(row, artifact, cleanup.get(row.id))


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
