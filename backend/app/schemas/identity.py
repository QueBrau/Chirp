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
    """Body for POST /auth/bootstrap — firebase_uid comes from the verified identity, not the body.

    NO campus_id (board c85). It used to be here and was written straight through to
    users.campus_id with no check against anything, while the campus feed's guard is
    `user.campus_id != campus_id -> 403` — a comparison against a value the caller
    supplied, which enforces consistency and not identity. Campus is now SERVER-OWNED
    and the .edu verification flow (c86) is its only writer.

    Removing the field rather than validating it is deliberate: _Schema does not set
    extra="forbid", so a client still sending campus_id has it ignored instead of
    getting a 422, and nothing in the app sends it today anyway
    (app/(auth)/account-type.tsx passes email, display_name and account_type only).
    """

    email: str = Field(min_length=3)
    display_name: str = Field(min_length=1)
    avatar_url: str | None = None
    account_type: AccountType


class UserUpdate(_Schema):
    """No campus_id here either, for the same reason (c85).

    This schema has no route today, which is exactly why it is worth cleaning now:
    an unused field is the one that gets wired up later by someone who assumes it
    was safe because it was already written.
    """

    display_name: str | None = None
    avatar_url: str | None = None


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


class MemberOut(_Schema):
    """MembershipOut plus the joined user's display identity, for roster views."""

    id: uuid.UUID
    user_id: uuid.UUID
    chapter_id: uuid.UUID
    role: RoleName
    status: MembershipStatus
    pledge_class: str | None = None
    joined_at: datetime
    display_name: str
    avatar_url: str | None = None


class RoleMetaOut(_Schema):
    """Body for GET /chapters/{id}/role-meta — the role taxonomy, served so the app
    never hand-mirrors permissions.py (c44).

    `roles` is every role in canonical display order, `eboard` the officer subset,
    `invitable` the roles THIS caller may mint invites for (empty for non-eboard),
    ordered as a picker should show them: common roles first.
    """

    roles: list[RoleName]
    eboard: list[RoleName]
    invitable: list[RoleName]


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
