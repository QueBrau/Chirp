"""Lineage: families, big/little edges, and the full tree payload for a chapter."""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.core.errors import conflict, forbidden, not_found
from app.core.permissions import EBOARD, require_role
from app.db import get_session
from app.middleware.org_scope import get_current_membership
from app.schemas.lineage import (
    FamilyCreate,
    FamilyOut,
    LineageEdgeCreate,
    LineageEdgeOut,
    LineageNodeOut,
    LineageTreeOut,
)

router = APIRouter(tags=["lineage"])


@router.get("/chapters/{chapter_id}/lineage")
async def get_lineage_tree(
    chapter_id: uuid.UUID,
    _membership: models.Membership = Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
) -> LineageTreeOut:
    """Full adjacency for the tree render: nodes + edges + families in one payload.

    Nodes = the chapter's active members plus any ghost users referenced by edges
    (placeholder alumni/historical members without real accounts).
    """
    members_result = await session.execute(
        select(models.Membership, models.User)
        .join(models.User, models.User.id == models.Membership.user_id)
        .where(
            models.Membership.chapter_id == chapter_id,
            models.Membership.status == "active",
        )
    )
    nodes: dict[uuid.UUID, LineageNodeOut] = {}
    for membership, user in members_result.all():
        nodes[user.id] = LineageNodeOut(
            user_id=user.id,
            display_name=user.display_name,
            avatar_url=user.avatar_url,
            is_ghost=user.is_ghost,
            family_id=None,
            pledge_class=membership.pledge_class,
        )

    edges_result = await session.execute(
        select(models.LineageEdge).where(models.LineageEdge.chapter_id == chapter_id)
    )
    edges = list(edges_result.scalars().all())

    # Family placement comes from the edge where the user is the little.
    little_edge: dict[uuid.UUID, models.LineageEdge] = {
        edge.little_user_id: edge for edge in edges
    }
    for user_id, edge in little_edge.items():
        if user_id in nodes:
            nodes[user_id].family_id = edge.family_id

    # Ghost users referenced by edges but not active members (historical lineage).
    referenced_ids: set[uuid.UUID] = set()
    for edge in edges:
        referenced_ids.add(edge.big_user_id)
        referenced_ids.add(edge.little_user_id)
    missing_ids = referenced_ids - set(nodes)
    if missing_ids:
        ghosts_result = await session.execute(
            select(models.User).where(
                models.User.id.in_(missing_ids),
                models.User.is_ghost.is_(True),
            )
        )
        for user in ghosts_result.scalars().all():
            edge = little_edge.get(user.id)
            nodes[user.id] = LineageNodeOut(
                user_id=user.id,
                display_name=user.display_name,
                avatar_url=user.avatar_url,
                is_ghost=True,
                family_id=edge.family_id if edge is not None else None,
                pledge_class=edge.pledge_class if edge is not None else None,
            )

    families_result = await session.execute(
        select(models.Family).where(models.Family.chapter_id == chapter_id)
    )
    return LineageTreeOut(
        nodes=list(nodes.values()),
        edges=[LineageEdgeOut.model_validate(edge) for edge in edges],
        families=[FamilyOut.model_validate(f) for f in families_result.scalars().all()],
    )


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
    if body.big_user_id == body.little_user_id:
        raise HTTPException(status_code=422, detail="big_and_little_identical")
    if body.family_id is not None:
        family = await session.get(models.Family, body.family_id)
        if family is None or family.chapter_id != chapter_id:
            raise HTTPException(status_code=422, detail="family_not_in_chapter")

    existing = await session.execute(
        select(models.LineageEdge).where(
            models.LineageEdge.chapter_id == chapter_id,
            models.LineageEdge.little_user_id == body.little_user_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise conflict("little_already_has_big")

    edge = models.LineageEdge(
        chapter_id=chapter_id,
        big_user_id=body.big_user_id,
        little_user_id=body.little_user_id,
        family_id=body.family_id,
        pledge_class=body.pledge_class,
        created_by=membership.user_id,
    )
    session.add(edge)
    try:
        await session.commit()
    except IntegrityError:
        # Unique (little_user_id, chapter_id) race — surface the same graceful 409.
        await session.rollback()
        raise conflict("little_already_has_big")
    await session.refresh(edge)
    return LineageEdgeOut.model_validate(edge)


@router.post("/chapters/{chapter_id}/lineage/edges/{edge_id}/confirm")
async def confirm_edge(
    chapter_id: uuid.UUID,
    edge_id: uuid.UUID,
    membership: models.Membership = Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
) -> LineageEdgeOut:
    """Little confirms their own big/little edge (idempotent)."""
    edge = await session.get(models.LineageEdge, edge_id)
    if edge is None or edge.chapter_id != chapter_id:
        raise not_found("edge_not_found")
    if edge.little_user_id != membership.user_id:
        raise forbidden("only_little_can_confirm")
    if not edge.confirmed_by_little:
        edge.confirmed_by_little = True
        await session.commit()
    return LineageEdgeOut.model_validate(edge)
