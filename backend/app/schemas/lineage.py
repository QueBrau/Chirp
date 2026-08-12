"""Lineage (family tree) schemas: families, big/little edges, full tree payload."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class _Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


# ---- families ----


class FamilyCreate(_Schema):
    name: str = Field(min_length=1)
    color: str = "#6366f1"


class FamilyOut(_Schema):
    id: uuid.UUID
    chapter_id: uuid.UUID
    name: str
    color: str


# ---- edges ----


class LineageEdgeCreate(_Schema):
    big_user_id: uuid.UUID
    little_user_id: uuid.UUID
    family_id: uuid.UUID | None = None
    pledge_class: str | None = None


class LineageEdgeOut(_Schema):
    id: uuid.UUID
    chapter_id: uuid.UUID
    big_user_id: uuid.UUID
    little_user_id: uuid.UUID
    family_id: uuid.UUID | None = None
    pledge_class: str | None = None
    confirmed_by_little: bool
    created_by: uuid.UUID
    created_at: datetime


# ---- tree payload (GET /chapters/{chapter_id}/lineage) ----


class LineageNodeOut(_Schema):
    """One member node for the tree render — user info + family placement."""

    user_id: uuid.UUID
    display_name: str
    avatar_url: str | None = None
    is_ghost: bool = False
    family_id: uuid.UUID | None = None
    pledge_class: str | None = None


class LineageTreeOut(_Schema):
    """Full adjacency for a chapter: nodes + edges + families (SPEC §4)."""

    nodes: list[LineageNodeOut]
    edges: list[LineageEdgeOut]
    families: list[FamilyOut]
