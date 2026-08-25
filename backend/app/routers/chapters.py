"""Chapter CRUD, member management, invite creation, and invite-code join."""
import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.core.dues_status import dues_contributions_subquery
from app.core.errors import conflict, forbidden, not_found
from app.core.invites import clamp_invite_expiry
from app.core.permissions import (
    DEPUTY_OVERVIEW,
    EBOARD,
    MEMBERS_ADMIN,
    Role,
    capabilities_for,
    require_role,
)
from app.core.windows import meeting_window
from app.db import get_session
from app.middleware.auth import get_current_user
from app.middleware.org_scope import get_current_membership
from app.schemas.identity import (
    AttendanceOverview,
    ChapterCreate,
    ChapterInviteCreate,
    ChapterInviteOut,
    ChapterInviteRevokeRequest,
    ChapterJoinRequest,
    ChapterOut,
    ChapterOverview,
    ChapterUpdate,
    DeputyOverview,
    DuesOverview,
    InviteOverview,
    LineageOverview,
    MemberOut,
    MembershipOut,
    MembershipUpdate,
    RoleCount,
    RoleMetaOut,
    RoleTermOut,
    RosterOverview,
)
from app.services.role_term_service import apply_role_change, open_initial_term

router = APIRouter(tags=["chapters"])

_EBOARD_ROLE_VALUES: frozenset[str] = frozenset(role.value for role in EBOARD)


@router.post("/chapters", status_code=201)
async def create_chapter(
    body: ChapterCreate,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ChapterOut:
    """Create a chapter; the creator becomes its president via a new membership.

    Platform-admin only (SECURITY-REVIEW finding 1 / board card c28): self-serve
    chapter creation was the last privilege-escalation vector, since the
    creator auto-becomes president (full EBOARD powers). There is no API to
    grant is_platform_admin — it is flipped directly in the DB.
    """
    if not user.is_platform_admin:
        raise forbidden("platform_admin_required")
    chapter = models.Chapter(
        campus_id=body.campus_id,
        org_name=body.org_name,
        chapter_name=body.chapter_name,
    )
    session.add(chapter)
    await session.flush()
    membership = models.Membership(
        user_id=user.id,
        chapter_id=chapter.id,
        role=Role.president.value,
    )
    session.add(membership)
    await session.flush()
    await open_initial_term(session, membership=membership)
    # c96, same rule as join_chapter: the founding president belongs to the
    # campus they just created a chapter on. Safe here for the stronger reason
    # that this route is platform-admin-only.
    if user.campus_id is None:
        user.campus_id = chapter.campus_id
        session.add(user)
    await session.commit()
    await session.refresh(chapter)
    return ChapterOut.model_validate(chapter)


@router.get("/chapters/{chapter_id}")
async def get_chapter(
    chapter_id: uuid.UUID,
    _membership: models.Membership = Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
) -> ChapterOut:
    """Return the chapter; org-scoped to active members (§8.4)."""
    chapter = await session.get(models.Chapter, chapter_id)
    if chapter is None:
        raise not_found("chapter_not_found")
    return ChapterOut.model_validate(chapter)


@router.patch("/chapters/{chapter_id}")
async def update_chapter(
    chapter_id: uuid.UUID,
    body: ChapterUpdate,
    _actor: models.Membership = Depends(require_role(*MEMBERS_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> ChapterOut:
    """Update org_name/chapter_name; president only (board card c77).

    Client type, schema and a working call site all existed before this route did —
    ChapterUpdate and updateChapter() were built assuming a PATCH here that was never
    actually wired up, so every call was a 405. Found by driving the real UI rather
    than by reading the router file, which is exactly the kind of gap that reading
    code alone misses: the client-side pieces all looked complete.

    Same partial-update convention as update_member directly below: a field left
    None is left UNCHANGED, never cleared. That means chapter_name, despite being
    nullable on the model, cannot be reset to null through this route once it has a
    value — identical to how update_member already treats pledge_class. Not a new
    limitation introduced here; matching the one this codebase already shipped and
    accepted, rather than inventing an exclude-unset convention that would apply to
    exactly one endpoint.
    """
    chapter = await session.get(models.Chapter, chapter_id)
    if chapter is None:
        raise not_found("chapter_not_found")
    if body.org_name is not None:
        chapter.org_name = body.org_name
    if body.chapter_name is not None:
        chapter.chapter_name = body.chapter_name
    await session.commit()
    await session.refresh(chapter)
    return ChapterOut.model_validate(chapter)


@router.get("/chapters/{chapter_id}/members")
async def list_members(
    chapter_id: uuid.UUID,
    _membership: models.Membership = Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
) -> list[MemberOut]:
    """List the chapter's memberships with display identity; org-scoped (§8.4)."""
    result = await session.execute(
        select(models.Membership, models.User)
        .join(models.User, models.User.id == models.Membership.user_id)
        .where(models.Membership.chapter_id == chapter_id)
        .order_by(models.Membership.joined_at)
    )
    entries: list[MemberOut] = []
    for membership, member_user in result.all():
        entries.append(
            MemberOut(
                id=membership.id,
                user_id=membership.user_id,
                chapter_id=membership.chapter_id,
                role=membership.role,
                status=membership.status,
                pledge_class=membership.pledge_class,
                joined_at=membership.joined_at,
                display_name=member_user.display_name,
                avatar_url=member_user.avatar_url,
            )
        )
    return entries


@router.get("/chapters/{chapter_id}/role-meta")
async def get_role_meta(
    chapter_id: uuid.UUID,
    membership: models.Membership = Depends(get_current_membership),
) -> RoleMetaOut:
    """Role taxonomy for this chapter's UI; org-scoped (§8.4).

    Derived entirely from permissions.py so the app never hand-mirrors the
    eboard set or the invite rule (c44). `invitable` applies the create_invite
    rule for THIS caller: any e-board role may mint non-eboard invites, only a
    president may mint e-board invites, everyone else gets an empty list.
    """
    roles = [role.value for role in Role]
    eboard = [role.value for role in Role if role in EBOARD]
    non_eboard = [role.value for role in Role if role not in EBOARD]
    if membership.role == Role.president.value:
        invitable = non_eboard + eboard
    elif membership.role in _EBOARD_ROLE_VALUES:
        invitable = non_eboard
    else:
        invitable = []
    return RoleMetaOut(
        roles=roles,
        eboard=eboard,
        invitable=invitable,
        capabilities=capabilities_for(membership.role),
    )


async def _roster_overview(chapter_id: uuid.UUID, session: AsyncSession) -> RosterOverview:
    """Who is on the roster right now (1 statement).

    Grouped by (status, role) rather than counted per status: one pass gives both the
    active/inactive totals and the by-role breakdown, and they cannot disagree because
    they are folded from the same rows. "removed" is neither active nor inactive and is
    intentionally not reported — a removed member is not on the roster in any sense a
    president (or, per c163, the vice president's deputy view) is asking about.

    Shared by chapter_overview (president) and deputy_overview (VP/president, c163) so
    the roster panel cannot report two different numbers on the same chapter.
    """
    roster_rows = await session.execute(
        select(
            models.Membership.status,
            models.Membership.role,
            func.count().label("count"),
        )
        .where(models.Membership.chapter_id == chapter_id)
        .group_by(models.Membership.status, models.Membership.role)
    )
    active_by_role: dict[str, int] = {}
    active_total = 0
    inactive_total = 0
    for status, role, count in roster_rows:
        if status == "active":
            active_by_role[role] = active_by_role.get(role, 0) + count
            active_total += count
        elif status == "inactive":
            inactive_total += count
    return RosterOverview(
        active=active_total,
        inactive=inactive_total,
        # Biggest group first, then role name, so the order is stable between calls
        # when two roles tie — the same reason attendance_summary orders by id after
        # display_name.
        by_role=[
            RoleCount(role=role, count=count)  # type: ignore[arg-type]
            for role, count in sorted(active_by_role.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
    )


async def _dues_overview(chapter_id: uuid.UUID, session: AsyncSession) -> DuesOverview:
    """The current dues cycle and how far through collecting it the chapter is
    (3 statements: cycle, per-member net, chapter net).

    Shared by chapter_overview (president) and deputy_overview (VP/president, c163) —
    see that shared use for why this is a function rather than being inlined twice,
    which would risk the two dashboards silently disagreeing about the same cycle.

    "Current cycle" is the most recently CREATED one, matching list_dues_cycles'
    ordering and therefore treasurer.tsx's cycles[0]. Picking the nearest due_date
    here instead would be defensible in isolation and would put a different cycle name
    on the President and Treasurer screens on the same day — the exact class of silent
    disagreement _meeting_window exists to prevent.
    """
    cycle = await session.scalar(
        select(models.DuesCycle)
        .where(models.DuesCycle.chapter_id == chapter_id)
        .order_by(models.DuesCycle.created_at.desc())
        .limit(1)
    )
    if cycle is None:
        return DuesOverview()

    # A dues payment can be CORRECTED — ledger_entries is append-only, so a refund
    # or a mistake is a new entry_type="correction" row pointing at the original via
    # corrects_entry_id (SPEC 8.2). Reading only entry_type="dues_payment" would
    # report money the chapter gave back as money it collected.
    #
    # dues_contributions_subquery (app/core/dues_status.py) is the SAME netting
    # definition payments.py's create_dues_payment_intent guard reads (board c172)
    # — before that module existed this query and that guard's plain existence
    # check disagreed about whether a fully-refunded member had paid.
    contributions = dues_contributions_subquery(chapter_id, cycle.id)

    # PAID/OUTSTANDING ARE SPINED ON THE ACTIVE ROSTER, so they always sum to
    # roster.active. Counting DISTINCT payers instead would let a member who paid
    # and then went inactive make outstanding_members negative — the roster shrinks
    # under a payment count that cannot.
    paid_rows = await session.execute(
        select(func.coalesce(func.sum(contributions.c.amount_cents), 0).label("net"))
        .select_from(models.Membership)
        .outerjoin(contributions, contributions.c.user_id == models.Membership.user_id)
        .where(
            models.Membership.chapter_id == chapter_id,
            models.Membership.status == "active",
        )
        .group_by(models.Membership.user_id)
    )
    # Net rather than "has a payment row": a member refunded in full is someone the
    # president still has to chase, and a partial correction leaves them paid. Both
    # fall out of the sign of one number. See chapter_overview's original docstring
    # (git history) for the full c172 discussion this netting resolves.
    nets = [row.net for row in paid_rows]
    paid_members = sum(1 for net in nets if net > 0)
    return DuesOverview(
        cycle_id=cycle.id,
        cycle_name=cycle.name,
        amount_cents=cycle.amount_cents,
        due_date=cycle.due_date,
        paid_members=paid_members,
        outstanding_members=len(nets) - paid_members,
        # DELIBERATELY NOT roster-spined, unlike the two counts above. This is "how
        # much money came in for this cycle", which includes members who have since
        # gone inactive and payments whose related_user_id was never set. Filtering
        # it to the current roster would quietly under-report the bank balance to
        # make it agree with a headcount, and the two are not the same question.
        collected_cents=await session.scalar(
            select(func.coalesce(func.sum(contributions.c.amount_cents), 0))
        )
        or 0,
    )


async def _invite_overview(
    chapter_id: uuid.UUID, session: AsyncSession, now: datetime
) -> InviteOverview:
    """Invite codes that could still be redeemed right now (1 statement).

    All three conditions c105 gave a code, together: a code that is merely unexpired is
    not live if it was revoked or has been fully redeemed. remaining_uses answers "how
    many more people could walk in on codes already out there", which is the number
    that decides whether something needs revoking.

    Shared by chapter_overview (president) and deputy_overview (VP/president, c163).
    """
    live_invite = [
        models.ChapterInvite.chapter_id == chapter_id,
        models.ChapterInvite.revoked_at.is_(None),
        models.ChapterInvite.expires_at > now,
        models.ChapterInvite.uses < models.ChapterInvite.max_uses,
    ]
    invite_row = (
        await session.execute(
            select(
                func.count().label("live_codes"),
                func.coalesce(
                    func.sum(models.ChapterInvite.max_uses - models.ChapterInvite.uses), 0
                ).label("remaining_uses"),
            ).where(*live_invite)
        )
    ).one()
    return InviteOverview(
        live_codes=invite_row.live_codes,
        remaining_uses=invite_row.remaining_uses,
    )


@router.get("/chapters/{chapter_id}/overview")
async def chapter_overview(
    chapter_id: uuid.UUID,
    start: datetime | None = None,
    end: datetime | None = None,
    _membership: models.Membership = Depends(require_role(*MEMBERS_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> ChapterOverview:
    """Chapter health in one request, for the President dashboard (board card c171).

    The president is the only role holding every capability (dues_admin, minutes_admin,
    members_admin, moderation, lineage_admin) and theirs was the only e-board screen
    showing no state at all — president.tsx was two text inputs and a tappable roster,
    so "are dues collected" and "is anyone failing attendance" meant walking into other
    officers' screens one at a time. This is the single call that answers both.

    GATED ON MEMBERS_ADMIN (president-only), which is the tightest HONEST gate rather
    than a convenient one. The payload mixes dues, attendance and lineage; a treasurer
    must not read attendance and a secretary must not read dues, so the only role that
    may see all of it at once is the one that already holds all three capabilities. No
    new capability is introduced — a sixth name in CAPABILITIES that happened to mean
    "president" would be a second spelling of members_admin and would drift from it.

    MODERATION IS ABSENT ON PURPOSE. content_reports carries campus_id, NOT chapter_id,
    and list_reports scopes to campuses the caller moderates (SECURITY-REVIEW finding
    1). An "open reports" field inside a response shaped /chapters/{id}/overview would
    be campus data wearing a chapter label — a number that is wrong for any campus with
    two chapters on it, and wrong in the direction that makes a president think their
    own chapter is being reported. It needs its own campus-labelled call or nothing.

    STATEMENT BUDGET: seven, FIXED, none of them per-member. This endpoint has exactly
    the shape that produced the c82 and c156 N+1s, so the rule is that roster size
    changes the numbers and never the number of queries.

    The counts are deliberately NOT folded into one another as subqueries, for the
    reason c82's docstring already records: a roster-spined query returns no rows for
    an empty roster, so any denominator riding along inside it disappears exactly when
    a brand-new chapter loads this screen. Every panel here must render zeroes rather
    than nothing.

    THE WINDOW is _meeting_window's, shared with attendance_summary (c82) and
    list_meetings_with_attendance (c156) so the meeting count on this screen cannot
    disagree with the Secretary dashboard's by a boundary meeting. Callers pass the
    same [start, end] the Secretary screen computes; omitting both means all time.
    """
    now = datetime.now(timezone.utc)

    # Roster/dues/invites are shared with deputy_overview below (c163) — see each
    # helper's docstring for why they are functions rather than inlined twice.
    roster = await _roster_overview(chapter_id, session)
    dues = await _dues_overview(chapter_id, session)

    # ---- attendance (2 statements) ----
    window = meeting_window(chapter_id, start, end)
    meetings_in_window = await session.scalar(
        select(func.count()).select_from(models.Meeting).where(*window)
    )
    # THE JOIN IS THE CORRECTNESS ARGUMENT, and it is c82's argument verbatim: attendance
    # is joined against meeting ids ALREADY filtered to this chapter and window, never
    # joined out to `meetings` afterwards. The natural-looking version leaves the meetings
    # side NULL on a LEFT JOIN while `status` stays non-null, so a dual-chapter member's
    # absences from their OTHER chapter get counted here.
    windowed_meeting_ids = select(models.Meeting.id).where(*window)
    members_with_absence = await session.scalar(
        select(func.count(func.distinct(models.Membership.user_id)))
        .select_from(models.Membership)
        .join(
            models.MeetingAttendance,
            and_(
                models.MeetingAttendance.user_id == models.Membership.user_id,
                models.MeetingAttendance.meeting_id.in_(windowed_meeting_ids),
            ),
        )
        .where(
            models.Membership.chapter_id == chapter_id,
            models.Membership.status == "active",
            models.MeetingAttendance.status == "absent",
        )
    )

    # ---- lineage (1 statement) ----
    unconfirmed_edges = await session.scalar(
        select(func.count())
        .select_from(models.LineageEdge)
        .where(
            models.LineageEdge.chapter_id == chapter_id,
            models.LineageEdge.confirmed_by_little.is_(False),
        )
    )

    invites = await _invite_overview(chapter_id, session, now)

    return ChapterOverview(
        chapter_id=chapter_id,
        generated_at=now,
        roster=roster,
        dues=dues,
        attendance=AttendanceOverview(
            meetings_in_window=meetings_in_window or 0,
            members_with_absence=members_with_absence or 0,
            window_start=start,
            window_end=end,
        ),
        lineage=LineageOverview(unconfirmed_edges=unconfirmed_edges or 0),
        invites=invites,
    )


@router.get("/chapters/{chapter_id}/deputy-overview")
async def deputy_overview(
    chapter_id: uuid.UUID,
    _membership: models.Membership = Depends(require_role(*DEPUTY_OVERVIEW)),
    session: AsyncSession = Depends(get_session),
) -> DeputyOverview:
    """Roster, dues status, and open invites for the Vice President's deputy-president
    dashboard (board card c163).

    Jose's product ruling (board decisions log): the VP dashboard is DEPUTY PRESIDENT —
    a READ view of president-admin data (roster, open invites, dues status) framed as a
    stand-in, with delegation (acting on any of it) explicitly out of the alpha build.

    GATED ON deputy_overview, a capability of its own rather than members_admin.
    chapter_overview above is deliberately gated on members_admin BECAUSE its payload
    mixes dues, attendance and lineage, and no role short of president may read all
    three - reusing that gate here (or reusing that endpoint's response and trimming
    fields client-side) would ship attendance and lineage over the wire to a role
    that holds neither minutes_admin nor lineage_admin. This endpoint computes only
    the sections deputy_overview actually grants.

    Same roster/dues/invites helpers chapter_overview uses, so the two dashboards
    cannot report different numbers for the same chapter.
    """
    now = datetime.now(timezone.utc)
    roster = await _roster_overview(chapter_id, session)
    dues = await _dues_overview(chapter_id, session)
    invites = await _invite_overview(chapter_id, session, now)
    return DeputyOverview(
        chapter_id=chapter_id,
        generated_at=now,
        roster=roster,
        dues=dues,
        invites=invites,
    )


@router.patch("/chapters/{chapter_id}/members")
async def update_member(
    chapter_id: uuid.UUID,
    body: MembershipUpdate,
    actor: models.Membership = Depends(require_role(*MEMBERS_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> MembershipOut:
    """Update a member's role/status/pledge_class; president only.

    A role change (board card c83) closes the member's open role_terms row and
    opens a new one via apply_role_change — a no-op if the requested role equals
    the current one. memberships.role stays the current-role source of truth;
    role_terms is the dated history layered on top of it.
    """
    result = await session.execute(
        select(models.Membership).where(
            models.Membership.chapter_id == chapter_id,
            models.Membership.user_id == body.user_id,
        )
    )
    target = result.scalar_one_or_none()
    if target is None:
        raise not_found("membership_not_found")
    if body.role is not None:
        await apply_role_change(
            session, membership=target, new_role=body.role, changed_by=actor.user_id
        )
    if body.status is not None:
        target.status = body.status
    if body.pledge_class is not None:
        target.pledge_class = body.pledge_class
    await session.commit()
    return MembershipOut.model_validate(target)


@router.get("/chapters/{chapter_id}/members/{user_id}/role-terms")
async def list_role_terms(
    chapter_id: uuid.UUID,
    user_id: uuid.UUID,
    _membership: models.Membership = Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
) -> list[RoleTermOut]:
    """Role history for one member of the chapter, newest first (board card c83).

    Gated the same way the roster itself is (GET /chapters/{chapter_id}/members):
    any active member of the chapter may read it, via get_current_membership —
    reusing that capability pattern rather than inventing a tighter one, since this
    is more history alongside data (current role, status) the roster already shows
    every member. org-scoped the same way that lookup is: the membership row below
    is matched on BOTH chapter_id and user_id, so a caller can never walk another
    chapter's user_id in through this route and a non-member of chapter_id never
    gets past get_current_membership's 403 to try.
    """
    result = await session.execute(
        select(models.Membership.id).where(
            models.Membership.chapter_id == chapter_id,
            models.Membership.user_id == user_id,
        )
    )
    membership_id = result.scalar_one_or_none()
    if membership_id is None:
        raise not_found("membership_not_found")
    result = await session.execute(
        select(models.RoleTerm)
        .where(models.RoleTerm.membership_id == membership_id)
        .order_by(models.RoleTerm.started_at.desc(), models.RoleTerm.id.desc())
    )
    return [RoleTermOut.model_validate(term) for term in result.scalars().all()]


@router.post("/chapters/{chapter_id}/invites", status_code=201)
async def create_invite(
    chapter_id: uuid.UUID,
    body: ChapterInviteCreate,
    actor: models.Membership = Depends(require_role(*EBOARD)),
    session: AsyncSession = Depends(get_session),
) -> ChapterInviteOut:
    """Create a deep-link invite code; e-board only. Bounded expiry, bounded uses.

    SECURITY-REVIEW finding 2: minting an EBOARD-role invite (e.g. a historian
    inviting a future president) requires the creator to already be president —
    any e-board role may still mint non-eboard invites (member/pledge/alumni).

    c105: this endpoint used to hand back a bearer token. `expires_at` was optional
    and passed straight through, so the documented, default way to call it produced
    a code that worked forever for anyone who ever saw the string. Every code now
    gets an expiry whether or not one was asked for, and a redemption budget.
    """
    if body.role in _EBOARD_ROLE_VALUES and actor.role != Role.president.value:
        raise forbidden("insufficient_role")
    now = datetime.now(timezone.utc)
    if body.expires_at is not None:
        requested = body.expires_at
        if requested.tzinfo is None:
            requested = requested.replace(tzinfo=timezone.utc)
        if requested <= now:
            raise HTTPException(status_code=422, detail="invite_expiry_in_past")
    invite = models.ChapterInvite(
        chapter_id=chapter_id,
        code=secrets.token_urlsafe(9),
        role=body.role,
        expires_at=clamp_invite_expiry(body.expires_at, now),
        max_uses=body.max_uses,
        created_by=actor.user_id,
    )
    session.add(invite)
    await session.commit()
    await session.refresh(invite)
    return ChapterInviteOut.model_validate(invite)


@router.get("/chapters/{chapter_id}/invites")
async def list_invites(
    chapter_id: uuid.UUID,
    actor: models.Membership = Depends(require_role(*EBOARD)),
    session: AsyncSession = Depends(get_session),
) -> list[ChapterInviteOut]:
    """Every invite this chapter has minted; e-board only.

    c111: c105 shipped revocation that took the CODE, which covers a president who
    still has the string in front of them and nobody else. Minting was the only
    place a code was ever returned, so a code posted three weeks ago and forwarded
    twice could not be revoked at all — the route existed and could not be reached.

    Returns revoked and expired codes too, deliberately. "Which of my codes are
    still live" is answerable from this list, and hiding the dead ones would make a
    leaked-but-expired code look like it was never minted, which is the question a
    president is actually asking when they come here.

    Ordered by expires_at descending, which is FURTHEST-FROM-EXPIRY first and not
    the same thing as newest first. `chapter_invites` has no created_at column, so
    minting order is not recoverable from this table at all — a code minted today
    with the 7-day default sorts below one minted last week for 30 days. It is the
    closest proxy available without a migration, and it does put live codes above
    dead ones, which is the question the screen is for. A created_at is worth
    adding the next time this table is touched.
    """
    result = await session.execute(
        select(models.ChapterInvite)
        .where(models.ChapterInvite.chapter_id == chapter_id)
        .order_by(models.ChapterInvite.expires_at.desc())
    )
    return [ChapterInviteOut.model_validate(row) for row in result.scalars().all()]


@router.post("/chapters/{chapter_id}/invites/revoke", status_code=200)
async def revoke_invite(
    chapter_id: uuid.UUID,
    body: ChapterInviteRevokeRequest,
    actor: models.Membership = Depends(require_role(*EBOARD)),
    session: AsyncSession = Depends(get_session),
) -> ChapterInviteOut:
    """Kill a leaked invite code. E-board only, and only for their own chapter.

    c105: before this there was NO way to withdraw a code short of an edit against
    the prod database. Revoking is idempotent — a second call on an already-revoked
    code returns the same row rather than 409, because the caller's intent ("this
    code must not work") is already satisfied and an error would only push them to
    go looking for something else to do.

    Scoped by chapter_id in the WHERE clause, not just by the role dependency: the
    dependency proves you are e-board SOMEWHERE, and looking the code up globally
    would let one chapter's president revoke another chapter's invites.
    """
    result = await session.execute(
        select(models.ChapterInvite).where(
            models.ChapterInvite.code == body.code,
            models.ChapterInvite.chapter_id == chapter_id,
        )
    )
    invite = result.scalar_one_or_none()
    if invite is None:
        raise not_found("invite_not_found")
    if invite.role in _EBOARD_ROLE_VALUES and actor.role != Role.president.value:
        raise forbidden("insufficient_role")
    if invite.revoked_at is None:
        invite.revoked_at = datetime.now(timezone.utc)
        session.add(invite)
        await session.commit()
        await session.refresh(invite)
    return ChapterInviteOut.model_validate(invite)


@router.post("/chapters/join", status_code=201)
async def join_chapter(
    body: ChapterJoinRequest,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MembershipOut:
    """Redeem an invite code: validates expiry, revocation and the redemption
    budget, 409 if already a member.

    c105: this used to be the whole check — does the row exist, has it expired —
    which made a code an unlimited-use bearer token. It now also has to be
    un-revoked and have a seat left, and the seat is CLAIMED rather than counted.
    """
    result = await session.execute(
        select(models.ChapterInvite).where(models.ChapterInvite.code == body.code)
    )
    invite = result.scalar_one_or_none()
    if invite is None:
        raise not_found("invite_not_found")
    now = datetime.now(timezone.utc)
    # These three read the row we just loaded and exist to give the caller a
    # specific reason. They are NOT the enforcement — the conditional UPDATE below
    # is, because between this read and that write another redemption can land.
    if invite.revoked_at is not None:
        raise forbidden("invite_revoked")
    if invite.expires_at <= now:
        raise forbidden("invite_expired")
    if invite.uses >= invite.max_uses:
        raise forbidden("invite_exhausted")
    existing = await session.execute(
        select(models.Membership.id).where(
            models.Membership.user_id == user.id,
            models.Membership.chapter_id == invite.chapter_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise conflict("already_member")
    # c105 — CLAIM a seat, do not count one.
    #
    # Read-check-then-write on `uses` is the dues double-charge bug wearing a
    # different hat: two people redeeming the last seat both read uses=24, both
    # decide there is room, and both write 25. A single conditional UPDATE makes
    # the database do the deciding, and it re-tests expiry and revocation in the
    # same statement so a code revoked one millisecond ago cannot slip through the
    # gap between the SELECT above and this write.
    claimed = await session.execute(
        update(models.ChapterInvite)
        .where(
            models.ChapterInvite.id == invite.id,
            models.ChapterInvite.uses < models.ChapterInvite.max_uses,
            models.ChapterInvite.revoked_at.is_(None),
            models.ChapterInvite.expires_at > now,
        )
        .values(uses=models.ChapterInvite.uses + 1)
        .returning(models.ChapterInvite.id)
        .execution_options(synchronize_session=False)
    )
    if claimed.scalar_one_or_none() is None:
        raise forbidden("invite_exhausted")

    membership = models.Membership(
        user_id=user.id,
        chapter_id=invite.chapter_id,
        role=invite.role,
    )
    session.add(membership)
    await session.flush()
    await open_initial_term(session, membership=membership)

    # c96 — a chapter you were INVITED to is proof of a campus, so inherit it.
    #
    # Before this, nothing anywhere wrote users.campus_id: c85 correctly stopped
    # trusting the client to assert one at bootstrap, and named c86's .edu
    # redemption as the only writer. But c86 is deferred, so every user sat at
    # campus_id NULL forever, which dead-ends Home's Campus tab AND the whole Chirp
    # tab and makes board gate c71 unreachable.
    #
    # This is NOT a rollback of c85. The value is read off the CHAPTER, which
    # only a platform admin can create and which carries a server-set campus_id;
    # the client supplies an invite code and nothing else. An e-board member
    # deliberately minting a code for you is a human vouching for you, which is
    # the same kind of evidence .edu verification gathers, arriving earlier.
    #
    # Only fills a NULL. Once c86 ships, a verified .edu is the stronger claim
    # and must win, so this must never overwrite a campus the user already has.
    if user.campus_id is None:
        chapter = await session.get(models.Chapter, invite.chapter_id)
        if chapter is not None:
            user.campus_id = chapter.campus_id
            session.add(user)

    try:
        await session.commit()
    except IntegrityError:
        # Concurrent double-tap/retry race on the (user_id, chapter_id) unique
        # constraint — surface the same graceful 409 (SECURITY-REVIEW finding 6).
        await session.rollback()
        raise conflict("already_member") from None
    await session.refresh(membership)
    return MembershipOut.model_validate(membership)


@router.get("/me/memberships")
async def list_my_memberships(
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[MembershipOut]:
    """List the caller's ACTIVE memberships, each with its chapter's name joined in.

    Uses get_current_user, not get_current_membership — get_current_membership needs
    a chapter_id already in the path, which is exactly the chicken-and-egg this route
    solves: it's how the client first learns its own chapter_id/role.
    """
    result = await session.execute(
        select(models.Membership, models.Chapter.org_name, models.Chapter.chapter_name)
        .join(models.Chapter, models.Chapter.id == models.Membership.chapter_id)
        .where(
            models.Membership.user_id == user.id,
            models.Membership.status == "active",
        )
        .order_by(models.Membership.joined_at)
    )
    memberships: list[MembershipOut] = []
    for membership, org_name, chapter_name in result.all():
        out = MembershipOut.model_validate(membership)
        out.org_name = org_name
        out.chapter_name = chapter_name
        memberships.append(out)
    return memberships
