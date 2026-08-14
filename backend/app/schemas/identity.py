"""Identity & org schemas: users, campuses, chapters, memberships, invites."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AccountType = Literal["greek", "non_greek", "alumni"]
RoleName = Literal[
    "president",
    "vice_president",
    "treasurer",
    "secretary",
    "historian",
    "member",
    "pledge",
    "alumni",
]
MembershipStatus = Literal["active", "inactive", "removed"]


class _Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


# ---- users ----


class UserCreate(_Schema):
    """Body for POST /auth/bootstrap — firebase_uid comes from the verified identity, not the body."""

    email: str = Field(min_length=3)
    display_name: str = Field(min_length=1)
    avatar_url: str | None = None
    account_type: AccountType
    campus_id: uuid.UUID | None = None


class UserUpdate(_Schema):
    display_name: str | None = None
    avatar_url: str | None = None
    campus_id: uuid.UUID | None = None


class UserOut(_Schema):
    id: uuid.UUID
    firebase_uid: str
    email: str
    display_name: str
    avatar_url: str | None = None
    account_type: AccountType
    campus_id: uuid.UUID | None = None
    is_ghost: bool
    is_platform_admin: bool
    created_at: datetime


# ---- campuses ----


class CampusOut(_Schema):
    id: uuid.UUID
    name: str
    slug: str


# ---- chapters ----


class ChapterCreate(_Schema):
    campus_id: uuid.UUID
    org_name: str = Field(min_length=1)
    chapter_name: str | None = None


class ChapterUpdate(_Schema):
    org_name: str | None = None
    chapter_name: str | None = None


class ChapterOut(_Schema):
    id: uuid.UUID
    campus_id: uuid.UUID
    org_name: str
    chapter_name: str | None = None
    stripe_account_id: str | None = None
    created_at: datetime


# ---- memberships ----


class MembershipUpdate(_Schema):
    """Body for PATCH /chapters/{chapter_id}/members — targets one member by user_id."""

    user_id: uuid.UUID
    role: RoleName | None = None
    status: MembershipStatus | None = None
    pledge_class: str | None = None


class MembershipOut(_Schema):
    id: uuid.UUID
    user_id: uuid.UUID
    chapter_id: uuid.UUID
    role: RoleName
    status: MembershipStatus
    pledge_class: str | None = None
    joined_at: datetime
    org_name: str | None = None  # joined from chapters for GET /me/memberships
    chapter_name: str | None = None  # joined from chapters for GET /me/memberships
    # Joined from users for GET /chapters/{id}/members: without it a roster is a list
    # of bare UUIDs, which makes secretary attendance and treasurer approval views
    # unusable on real data. Members already see each other by name (§8.4 org scope).
    display_name: str | None = None


class MeOut(_Schema):
    """Body for GET /auth/me: the caller's user row plus their active memberships."""

    user: UserOut
    memberships: list[MembershipOut]


# ---- invites ----


class ChapterInviteCreate(_Schema):
    role: RoleName = "member"
    expires_at: datetime | None = None


class ChapterInviteOut(_Schema):
    id: uuid.UUID
    chapter_id: uuid.UUID
    code: str
    role: RoleName
    expires_at: datetime | None = None
    created_by: uuid.UUID


class ChapterJoinRequest(_Schema):
    """Body for POST /chapters/join — redeem an invite code."""

    code: str = Field(min_length=1)


ChapterJoin = ChapterJoinRequest
