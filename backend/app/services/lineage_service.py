"""Lineage domain logic: tree assembly, edge validation, one-big-per-little rule."""

from __future__ import annotations

import uuid
from collections import defaultdict, deque

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.core.errors import conflict, forbidden, not_found
from app.schemas.lineage import (
    FamilyOut,
    LineageEdgeCreate,
    LineageEdgeOut,
    LineageNodeOut,
    LineageTreeOut,
)


def _would_create_cycle(
    edges: list[models.LineageEdge],
    big_user_id: uuid.UUID,
    little_user_id: uuid.UUID,
) -> bool:
    """True if adding big→little would make `little` an ancestor of `big`."""
    children: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for edge in edges:
        children[edge.big_user_id].append(edge.little_user_id)
    # Walk descendants of the proposed little; if we reach the proposed big, it's a cycle.
    seen: set[uuid.UUID] = set()
    queue: deque[uuid.UUID] = deque([little_user_id])
    while queue:
        current = queue.popleft()
        if current == big_user_id:
            return True
        if current in seen:
            continue
        seen.add(current)
        queue.extend(children.get(current, []))
    return False


def _depths_and_families(
    node_ids: set[uuid.UUID],
    edges: list[models.LineageEdge],
) -> tuple[dict[uuid.UUID, int], dict[uuid.UUID, uuid.UUID | None]]:
    """Compute generation depth (roots=0) and propagate family_id along each chain."""
    littles = {edge.little_user_id for edge in edges}
    children: dict[uuid.UUID, list[models.LineageEdge]] = defaultdict(list)
    for edge in edges:
        children[edge.big_user_id].append(edge)

    depths: dict[uuid.UUID, int] = {}
    families: dict[uuid.UUID, uuid.UUID | None] = {uid: None for uid in node_ids}

    roots = [uid for uid in node_ids if uid not in littles]
    queue: deque[uuid.UUID] = deque()
    for root in roots:
        depths[root] = 0
        queue.append(root)

    seen_edge_ids: set[uuid.UUID] = set()
    while queue:
        big_id = queue.popleft()
        for edge in children.get(big_id, []):
            if edge.id in seen_edge_ids:
                continue
            seen_edge_ids.add(edge.id)
            little_id = edge.little_user_id
            depths[little_id] = depths[big_id] + 1
            if edge.family_id is not None:
                families[big_id] = families.get(big_id) or edge.family_id
                families[little_id] = edge.family_id
            queue.append(little_id)

    for edge in edges:
        if edge.family_id is not None:
            families[edge.big_user_id] = families.get(edge.big_user_id) or edge.family_id
            families[edge.little_user_id] = edge.family_id
        depths.setdefault(edge.little_user_id, depths.get(edge.big_user_id, 0) + 1)

    for uid in node_ids:
        depths.setdefault(uid, 0)
    return depths, families


async def build_lineage_tree(session: AsyncSession, chapter_id: uuid.UUID) -> LineageTreeOut:
    """Assemble nodes + edges + families for the interactive tree render."""
    members_result = await session.execute(
        select(models.Membership, models.User)
        .join(models.User, models.User.id == models.Membership.user_id)
        .where(
            models.Membership.chapter_id == chapter_id,
            models.Membership.status == "active",
        )
    )
    pledge_by_user: dict[uuid.UUID, str | None] = {}
    base_nodes: dict[uuid.UUID, models.User] = {}
    for membership, user in members_result.all():
        base_nodes[user.id] = user
        pledge_by_user[user.id] = membership.pledge_class

    edges_result = await session.execute(
        select(models.LineageEdge).where(models.LineageEdge.chapter_id == chapter_id)
    )
    edges = list(edges_result.scalars().all())

    referenced_ids: set[uuid.UUID] = set()
    for edge in edges:
        referenced_ids.add(edge.big_user_id)
        referenced_ids.add(edge.little_user_id)

    missing_ids = referenced_ids - set(base_nodes)
    if missing_ids:
        ghosts_result = await session.execute(
            select(models.User).where(
                models.User.id.in_(missing_ids),
                models.User.is_ghost.is_(True),
            )
        )
        for user in ghosts_result.scalars().all():
            base_nodes[user.id] = user
            little_edge = next((e for e in edges if e.little_user_id == user.id), None)
            pledge_by_user[user.id] = little_edge.pledge_class if little_edge else None

    depths, families = _depths_and_families(set(base_nodes), edges)

    nodes = [
        LineageNodeOut(
            user_id=user.id,
            display_name=user.display_name,
            avatar_url=user.avatar_url,
            is_ghost=user.is_ghost,
            family_id=families.get(user.id),
            pledge_class=pledge_by_user.get(user.id),
            depth=depths.get(user.id, 0),
        )
        for user in base_nodes.values()
    ]

    families_result = await session.execute(
        select(models.Family).where(models.Family.chapter_id == chapter_id)
    )
    return LineageTreeOut(
        nodes=nodes,
        edges=[LineageEdgeOut.model_validate(edge) for edge in edges],
        families=[FamilyOut.model_validate(f) for f in families_result.scalars().all()],
    )


async def create_lineage_edge(
    session: AsyncSession,
    *,
    chapter_id: uuid.UUID,
    body: LineageEdgeCreate,
    created_by: uuid.UUID,
) -> LineageEdgeOut:
    """Create a big→little edge. Each little may have only one big in a chapter."""
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
        # SPEC: UNIQUE (little_user_id, chapter_id) — one big per little.
        raise conflict("little_already_has_big")

    chapter_edges = await session.execute(
        select(models.LineageEdge).where(models.LineageEdge.chapter_id == chapter_id)
    )
    if _would_create_cycle(list(chapter_edges.scalars().all()), body.big_user_id, body.little_user_id):
        raise HTTPException(status_code=422, detail="lineage_cycle")

    edge = models.LineageEdge(
        chapter_id=chapter_id,
        big_user_id=body.big_user_id,
        little_user_id=body.little_user_id,
        family_id=body.family_id,
        pledge_class=body.pledge_class,
        created_by=created_by,
    )
    session.add(edge)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise conflict("little_already_has_big")
    await session.refresh(edge)
    return LineageEdgeOut.model_validate(edge)


async def confirm_lineage_edge(
    session: AsyncSession,
    *,
    chapter_id: uuid.UUID,
    edge_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> LineageEdgeOut:
    """Little confirms their own big/little edge (idempotent)."""
    edge = await session.get(models.LineageEdge, edge_id)
    if edge is None or edge.chapter_id != chapter_id:
        raise not_found("edge_not_found")
    if edge.little_user_id != actor_user_id:
        raise forbidden("only_little_can_confirm")
    if not edge.confirmed_by_little:
        edge.confirmed_by_little = True
        await session.commit()
        await session.refresh(edge)
    return LineageEdgeOut.model_validate(edge)
