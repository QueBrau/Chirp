"""Identity & org schemas: users, campuses, chapters, memberships, invites."""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import Field, field_validator

from app.core.invites import INVITE_DEFAULT_MAX_USES, INVITE_MAX_USES_CAP
from app.core.validation import validate_public_url
from app.schemas.base import _Schema

# SELF-DECLARED. DISPLAY AND ROUTING ONLY — NEVER AUTHORIZATION (board c242).
#
# The value arrives in the POST /auth/bootstrap body from the account-type screen
# (app/(auth)/account-type.tsx) and routers/auth.py writes it to users.account_type
# unchanged. Nothing verifies it and nothing can: it is the signup question "which
# of these are you", not a fact the system established. Picking "alumni" costs a
# tap.
#
# What it legitimately drives, and all it may ever drive: the label under the name
# on Profile (ACCOUNT_TYPE_LABELS) and whether the alumni info section renders
# there, plus an analytics dimension on user_signed_up. Presentation.
#
# It was also load-bearing once, and that is why this comment exists. POST /jobs
# read `user.account_type == "alumni"` as an eligibility branch, so any account
# could tick alumni at signup and post to the job board — whose rows carry an
# apply_url the mobile client opens with Linking.openURL, and which was served
# network-wide to every authenticated user. A phishing channel, keyed on a field
# the attacker fills in for themselves. Eligibility now comes from a memberships
# row (routers/alumni.py create_job_post) because that is the kind of fact a caller
# cannot assert about themselves.
#
# If you are about to gate something on this field: you want a membership role
# (core/permissions.py), users.is_platform_admin, or campus_verified_at — the
# things the server owns. Not this.
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

    # c184 sweep: avatar_url is client-supplied and written straight through to
    # users.avatar_url with no validation (routers/auth.py bootstrap_account),
    # the same shape of gap the card flagged in alumni.linkedin_url / apply_url.
    @field_validator("avatar_url")
    @classmethod
    def _validate_avatar_url(cls, value: str | None) -> str | None:
        return validate_public_url(value)


class UserUpdate(_Schema):
    """No campus_id here either, for the same reason (c85).

    This schema has no route today, which is exactly why it is worth cleaning now:
    an unused field is the one that gets wired up later by someone who assumes it
    was safe because it was already written.
    """

    display_name: str | None = None
    avatar_url: str | None = None

    @field_validator("avatar_url")
    @classmethod
    def _validate_avatar_url(cls, value: str | None) -> str | None:
        return validate_public_url(value)


class ProfileUpdate(_Schema):
    """Body for PATCH /auth/me: the caller editing their own profile (board c221).

    THE CLIENT SENDS AN OBJECT NAME, NOT A URL, and that is a deliberate narrowing of
    what UserUpdate above would have allowed. UserUpdate carries an `avatar_url` behind
    validate_public_url, which accepts any http(s) address - so wiring THAT up would let
    anyone point their avatar at a tracking pixel, at someone else's host, or at
    whatever they liked. This route instead takes the caller's OWN tmp/ object_name (the
    one POST /media/upload-url returned) and the server assigns the canonical url,
    exactly as post create already does with media_object_names.

    OMITTED VS EXPLICIT NULL is the contract, and pydantic v2 gives it for free through
    model_fields_set, the same way EventUpdate does (c202):

        {}                              -> change nothing
        {"avatar_object_name": null}    -> REMOVE the picture, back to initials
        {"avatar_object_name": "tmp/…"} -> set a new picture

    A field left out of the body never enters model_fields_set and is skipped; a field
    sent as null does enter it, with a value of None. So "no opinion" and "clear it" stay
    distinguishable without a sentinel.
    """

    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    avatar_object_name: str | None = None


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
    # c126: the caller's OWN suspension state. Safe to expose here specifically
    # because UserOut is used ONLY in self-facing responses (POST /auth/bootstrap,
    # GET /auth/me via MeOut) — never a view of another user, so this can never
    # leak a stranger's moderation history. Naming matches the platform-admin view
    # in schemas/moderation.py. NULL means never suspended; non-null is when. An
    # account is never auto-unsuspended — only a moderator clears this back to NULL.
    #
    # WORTH KNOWING BEFORE RELYING ON THIS: GET /auth/me does NOT go through
    # get_current_user (see auth.py's get_me docstring — it predates c76 and
    # resolves the user directly so an unregistered caller gets 404, not 401), so
    # this route was never suspension-gated and still 200s for a suspended caller.
    # Every OTHER authenticated route does 403 them via get_current_user. So this
    # field is reachable live specifically here — it is not, and was never meant to
    # be, a way to "find out why you got 403'd", since /auth/me never 403s on
    # suspension in the first place. Whether /auth/me SHOULD 403 too is a separate,
    # unresolved question — see board c126.
    suspended_at: datetime | None = None
    created_at: datetime


class UserSearchResultOut(_Schema):
    """GET /users/search row (board c322): id, display name, avatar only.

    Deliberately NOT UserOut. UserOut's own docstring says it is used "ONLY in
    self-facing responses" — it carries email, firebase_uid, is_platform_admin and
    the caller's own suspension state. Searching for someone ELSE to message must
    never leak any of that; the picker only needs enough to render a row and to
    name the conversation member the caller is about to create.
    """

    id: uuid.UUID
    display_name: str
    avatar_url: str | None = None


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


class RoleTermOut(_Schema):
    """Body for GET /chapters/{chapter_id}/members/{user_id}/role-terms — one dated
    span of a membership holding a role (board card c83: a chapter role is a DATED
    TERM, not a plain fact). Rows come back newest first; ended_at NULL marks the
    OPEN term, i.e. the role the member holds right now."""

    id: uuid.UUID
    membership_id: uuid.UUID
    role: RoleName
    started_at: datetime
    ended_at: datetime | None = None
    changed_by: uuid.UUID | None = None


class RoleMetaOut(_Schema):
    """Body for GET /chapters/{id}/role-meta — the role taxonomy, served so the app
    never hand-mirrors permissions.py (c44).

    `roles` is every role in canonical display order, `eboard` the officer subset,
    `invitable` the roles THIS caller may mint invites for (empty for non-eboard),
    ordered as a picker should show them: common roles first.

    `capabilities` (c80) is what THIS caller may DO, by name, so the app asks "may I
    see the dues tile" instead of "am I a treasurer or a president". It is derived
    from the same frozensets the routers gate on (permissions.CAPABILITIES), so a
    permission change moves the gate and the UI together. The client must never
    reconstruct it from `roles` - that is the hand-mirroring c44 and c80 both exist
    to stop.
    """

    roles: list[RoleName]
    eboard: list[RoleName]
    invitable: list[RoleName]
    capabilities: list[str] = []


class MeOut(_Schema):
    """Body for GET /auth/me: the caller's user row plus their active memberships."""

    user: UserOut
    memberships: list[MembershipOut]


# ---- invites ----


class ChapterInviteCreate(_Schema):
    """Body for minting an invite (c105).

    expires_at stays optional on the WIRE and is no longer optional in the SYSTEM:
    omit it and the router picks the default window. A caller cannot ask for a code
    that never expires, because there is no longer a value that means that.
    """

    role: RoleName = "member"
    expires_at: datetime | None = None
    max_uses: int = Field(default=INVITE_DEFAULT_MAX_USES, ge=1, le=INVITE_MAX_USES_CAP)


class ChapterInviteOut(_Schema):
    id: uuid.UUID
    chapter_id: uuid.UUID
    code: str
    role: RoleName
    expires_at: datetime
    max_uses: int
    uses: int
    revoked_at: datetime | None = None
    created_by: uuid.UUID


class ChapterInviteRevokeRequest(_Schema):
    """Body for POST /chapters/{id}/invites/revoke.

    By CODE, not by invite id, and that is the whole point: the thing that leaks is
    the string in a group chat. A president holding it should not first have to find
    the row it came from.
    """

    code: str = Field(min_length=1)


class ChapterJoinRequest(_Schema):
    """Body for POST /chapters/join — redeem an invite code."""

    code: str = Field(min_length=1)


ChapterJoin = ChapterJoinRequest


# ---- president overview (board card c171) ----


class RoleCount(_Schema):
    """How many ACTIVE members hold one role."""

    role: RoleName
    count: int


class RosterOverview(_Schema):
    """Who is on the roster right now.

    `by_role` counts active members only, so the counts sum to `active` and never to
    `active + inactive` — a breakdown that silently included inactive members would
    make the two numbers on screen disagree with no way to tell which was wrong.
    Roles nobody holds are omitted rather than reported as zero.
    """

    active: int
    inactive: int
    by_role: list[RoleCount]


class DuesOverview(_Schema):
    """The current dues cycle and how far through collecting it the chapter is.

    Every field is None/zero when the chapter has never opened a cycle, which is a
    real state for a new chapter and not an error.

    `paid_members` + `on_plan_members` + `outstanding_members` == RosterOverview.active,
    always: all three are spined on the current active roster (see the endpoint
    docstring for why that matters, and why `collected_cents` is deliberately NOT
    spined the same way).

    `on_plan_members` (board card c195) is a member with an ACTIVE payment plan who
    has not yet reached net >= the cycle total — reported separately rather than
    folded into `outstanding_members` because they are not being chased, they are on
    a schedule, and separately rather than folded into `paid_members` because they
    are not done yet either. `paid_members` is decided on NET ALONE, never on plan
    status: a completed plan reaches net >= the cycle total via its own installments
    in the ordinary case, so it reads as paid the same as a lump-sum payer — but if
    those installments are later corrected away, net drops and the member correctly
    falls back to outstanding rather than staying latched as paid by a stale
    'completed' status.
    """

    cycle_id: uuid.UUID | None = None
    cycle_name: str | None = None
    amount_cents: int | None = None
    due_date: date | None = None
    paid_members: int = 0
    on_plan_members: int = 0
    outstanding_members: int = 0
    collected_cents: int = 0


class AttendanceOverview(_Schema):
    """Meeting attendance over the same window the Secretary dashboard uses.

    `members_with_absence` counts active members with at least one recorded ABSENT in
    the window. Deliberately not "members below X%": there is no attendance policy in
    the schema, so any percentage would be this endpoint inventing a rule the chapter
    never agreed to.
    """

    meetings_in_window: int = 0
    members_with_absence: int = 0
    window_start: datetime | None = None
    window_end: datetime | None = None


class LineageOverview(_Schema):
    """Big/little pairs still waiting on the little to confirm (board c79)."""

    unconfirmed_edges: int = 0


class InviteOverview(_Schema):
    """Invite codes that could still be redeemed right now.

    Live means all three of: not revoked, not expired, and uses < max_uses — the same
    three conditions c105 made a code carry. `remaining_uses` is how many more people
    could walk in on codes already in circulation, which is the number that matters
    when deciding whether to revoke something.
    """

    live_codes: int = 0
    remaining_uses: int = 0


class ChapterOverview(_Schema):
    """One request's worth of chapter health, for the President dashboard (c171).

    Chapter-scoped throughout. Moderation is absent on purpose: content_reports
    carries campus_id, not chapter_id, so an "open reports" count here would be campus
    data wearing a chapter label.
    """

    chapter_id: uuid.UUID
    generated_at: datetime
    roster: RosterOverview
    dues: DuesOverview
    attendance: AttendanceOverview
    lineage: LineageOverview
    invites: InviteOverview


class DeputyOverview(_Schema):
    """Body for GET /chapters/{id}/deputy-overview — the Vice President's deputy-
    president dashboard (board card c163, Jose's product ruling).

    A trimmed sibling of ChapterOverview, not a client-side slice of it: attendance is
    the Secretary's domain and lineage is the Historian's/e-board's, and the Vice
    President holds neither minutes_admin nor lineage_admin from this card, so those
    two sections are absent from the RESPONSE, not merely hidden in the UI. Gated on
    the deputy_overview capability (permissions.py), which is read-only and holds no
    delegation - the ruling that named this a read view, not a stand-in with powers.
    """

    chapter_id: uuid.UUID
    generated_at: datetime
    roster: RosterOverview
    dues: DuesOverview
    invites: InviteOverview


class TreasurerOverview(_Schema):
    """Body for GET /chapters/{id}/treasurer-overview — the Treasurer's read of the
    chapter's authoritative dues picture (board card c278, Jose's product ruling).

    A trimmed sibling of ChapterOverview in the same mold as DeputyOverview above,
    one section narrower still: the treasurer holds dues_admin and nothing else from
    this card, so dues is the ONLY section — roster, invites, attendance and lineage
    are absent from the RESPONSE, not merely hidden in the UI. Exists because the
    gate-shaped alternative was worse both ways: widening the president-only
    chapter_overview would ship other officers' domains to dues_admin, and leaving
    the gap meant the officer who owns the dues number kept being refused the
    correctly-computed version of it — which is how a second, wrong client-side
    computation grew (found and corrected in c258).
    """

    chapter_id: uuid.UUID
    generated_at: datetime
    dues: DuesOverview
