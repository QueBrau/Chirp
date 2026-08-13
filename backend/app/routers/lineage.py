"""Lineage: families, big/little edges, and the full tree payload for a chapter."""
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.core.permissions import EBOARD, require_role
from app.db import get_session
from app.middleware.org_scope import get_current_membership
from app.schemas.lineage import (
    FamilyCreate,
    FamilyOut,
    LineageEdgeCreate,
    LineageEdgeOut,
    LineageTreeOut,
)
from app.services import lineage_service

router = APIRouter(tags=["lineage"])


@router.get("/chapters/{chapter_id}/lineage")
async def get_lineage_tree(
    chapter_id: uuid.UUID,
    _membership: models.Membership = Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
) -> LineageTreeOut:
    """Full adjacency for the tree render: nodes + edges + families (+ depth)."""
    return await lineage_service.build_lineage_tree(session, chapter_id)


@router.post("/chapters/{chapter_id}/lineage/families", status_code=201)
async def create_family(
    chapter_id: uuid.UUID,
    body: FamilyCreate,
    _membership: models.Membership = Depends(require_role(*EBOARD)),
    session: AsyncSession = Depends(get_session),
) -> FamilyOut:
    """Create a family (name + color) in the chapter; e-board only."""
    family = models.Family(chapter_id=chapter_id, name=body.name, color=body.color)
    session.add(family)
    await session.commit()
    await session.refresh(family)
    return FamilyOut.model_validate(family)


@router.post("/chapters/{chapter_id}/lineage/edges", status_code=201)
async def create_edge(
    chapter_id: uuid.UUID,
    body: LineageEdgeCreate,
    membership: models.Membership = Depends(require_role(*EBOARD)),
    session: AsyncSession = Depends(get_session),
) -> LineageEdgeOut:
    """Create a big/little edge; e-board (incl. historian). One big per little → 409."""
    return await lineage_service.create_lineage_edge(
        session,
        chapter_id=chapter_id,
        body=body,
        created_by=membership.user_id,
    )


@router.post("/chapters/{chapter_id}/lineage/edges/{edge_id}/confirm")
async def confirm_edge(
    chapter_id: uuid.UUID,
    edge_id: uuid.UUID,
    membership: models.Membership = Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
) -> LineageEdgeOut:
    """Little confirms their own big/little edge (idempotent)."""
    return await lineage_service.confirm_lineage_edge(
        session,
        chapter_id=chapter_id,
        edge_id=edge_id,
        actor_user_id=membership.user_id,
    )
