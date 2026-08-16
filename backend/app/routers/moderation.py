"""Moderation: content reports, user blocks, account suspension, and admin content
removal (yaks, posts, comments) — all with an audit trail (board card c76)."""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.core.campus_access import require_verified_campus
from app.core.errors import conflict, forbidden, not_found
from app.core.permissions import EBOARD, require_platform_admin
from app.db import get_session
from app.middleware.auth import get_current_user
from app.schemas.moderation import (
    ContentRemoveRequest,
    ReportResolveRequest,
    SuspendUserRequest,
    SuspensionStateOut,
)
from app.schemas.yak import (
    ContentReportCreate,
    ContentReportOut,
    UserBlockCreate,
    UserBlockOut,
    YakRemoveRequest,
)

router = APIRouter(tags=["moderation"])

_EBOARD_ROLES: list[str] = [role.value for role in EBOARD]


async def _require_any_eboard(
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> models.User:
    """v1 scaffolding simplification: moderator = e-board member of ANY active chapter.

    This only proves "is a moderator somewhere" — it is NOT sufficient authorization
    on its own. Callers MUST additionally scope to the specific campus/chapter of the
    target (see list_reports, remove_yak) so an e-board member of chapter A cannot see
    or act on chapter B's reports/yaks (SECURITY-REVIEW finding 1). Gating chapter
    creation itself (self-serve presidency) is a separate product decision, carded
    on the board — out of scope here.
    """
    result = await session.execute(
        select(models.Membership.id)
        .where(
            models.Membership.user_id == user.id,
            models.Membership.status == "active",
            models.Membership.role.in_(_EBOARD_ROLES),
        )
        .limit(1)
    )
    if result.scalar_one_or_none() is None:
        raise forbidden("insufficient_role")
    return user


async def _resolve_report_campus_id(
    session: AsyncSession,
    target_type: str,
    target_id: uuid.UUID | None,
    reporter: models.User,
) -> uuid.UUID | None:
    """Resolve the campus a report belongs to, server-side (SECURITY-REVIEW finding 1).

    yak -> yak.campus_id; post/comment -> the post's chapter -> chapter.campus_id;
    anything else (message_forward, user, or a missing/unresolvable target) falls back
    to the reporter's own users.campus_id, best-effort. Never trusts the client.
    """
    if target_id is not None and target_type == "yak":
        yak = await session.get(models.Yak, target_id)
        if yak is not None:
            return yak.campus_id
    elif target_id is not None and target_type == "post":
        post = await session.get(models.Post, target_id)
        if post is not None:
            chapter = await session.get(models.Chapter, post.chapter_id)
            if chapter is not None:
                return chapter.campus_id
    elif target_id is not None and target_type == "comment":
        comment = await session.get(models.PostComment, target_id)
        if comment is not None:
            post = await session.get(models.Post, comment.post_id)
            if post is not None:
                chapter = await session.get(models.Chapter, post.chapter_id)
                if chapter is not None:
                    return chapter.campus_id
    return reporter.campus_id


async def _require_eboard_for_campus(
    session: AsyncSession,
    moderator: models.User,
    campus_id: uuid.UUID | None,
    *,
    campus_content: bool,
) -> None:
    """403 unless moderator is active e-board in a chapter of campus_id AND verified there.

    Factored out of remove_yak (SECURITY-REVIEW finding 1's fix) so remove_content
    enforces the identical per-campus scoping instead of a second hand-rolled check.
    campus_id is None only when a target's campus could not be resolved (e.g. a
    dangling chapter reference) — treated as "no campus matches" rather than falling
    back to some platform-wide allowance.

    c108, Jose's call Aug 16: MODERATING CAMPUS content requires a verified .edu, not
    merely an officer role. He went stricter than the recommendation on that card, which
    argued the officer role was sufficient because other officers had vouched. His
    reasoning is the one already in c88 — the campus-wide surface is where a stranger
    costs something, and removing someone else's post is a more powerful act than
    reading it.

    `campus_content` IS REQUIRED AND HAS NO DEFAULT, because getting it wrong is silent
    in both directions and this function serves BOTH tiers. It gates yaks, which are
    campus-wide, AND chapter posts and comments, which are org content. The first
    implementation of c108 put the verification check here unconditionally, which would
    have locked an unverified officer out of moderating their OWN CHAPTER'S posts — the
    exact opposite of the two-tier ruling that chapter membership grants chapter content
    in full with no email at all. Passing it explicitly at every call site makes the
    tier a decision someone had to make rather than a default they inherited.

    So: a president who never verified keeps every chapter power, including removing a
    member's post, and cannot touch the campus Yak board.
    """
    if campus_id is None:
        raise forbidden("insufficient_role")
    campus_match = await session.execute(
        select(models.Membership.id)
        .join(models.Chapter, models.Chapter.id == models.Membership.chapter_id)
        .where(
            models.Membership.user_id == moderator.id,
            models.Membership.status == "active",
            models.Membership.role.in_(_EBOARD_ROLES),
            models.Chapter.campus_id == campus_id,
        )
        .limit(1)
    )
    if campus_match.scalar_one_or_none() is None:
        raise forbidden("insufficient_role")
    if campus_content:
        # Officer role established; now the .edu (c108). Ordered this way on purpose so
        # a non-officer gets insufficient_role and never learns that verification was
        # the only thing standing between them and moderator powers.
        require_verified_campus(moderator, campus_id)


@router.post("/moderation/reports", status_code=201)
async def create_report(
    body: ContentReportCreate,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ContentReportOut:
    """File a content report; forwarded_plaintext supports E2EE message reports (SPEC §6.7).

    campus_id is resolved server-side from the target (SECURITY-REVIEW finding 1) so
    list_reports/remove_yak can scope moderation to the right campus.
    """
    campus_id = await _resolve_report_campus_id(session, body.target_type, body.target_id, user)
    report = models.ContentReport(
        reporter_id=user.id,
        campus_id=campus_id,
        target_type=body.target_type,
        target_id=body.target_id,
        forwarded_plaintext=body.forwarded_plaintext,
        reason=body.reason,
    )
    session.add(report)
    await session.commit()
    await session.refresh(report)
    return ContentReportOut.model_validate(report)


@router.get("/moderation/reports")
async def list_reports(
    moderator: models.User = Depends(_require_any_eboard),
    session: AsyncSession = Depends(get_session),
) -> list[ContentReportOut]:
    """List reports newest first, scoped to campuses where the caller is active e-board.

    SECURITY-REVIEW finding 1: previously returned every report platform-wide
    (including forwarded_plaintext of reported E2EE messages) to any e-board member
    of any chapter. Now restricted to reports whose campus_id is one the caller
    actually moderates.
    """
    campus_ids_result = await session.execute(
        select(models.Chapter.campus_id)
        .join(models.Membership, models.Membership.chapter_id == models.Chapter.id)
        .where(
            models.Membership.user_id == moderator.id,
            models.Membership.status == "active",
            models.Membership.role.in_(_EBOARD_ROLES),
        )
        .distinct()
    )
    campus_ids = list(campus_ids_result.scalars().all())
    if not campus_ids:
        return []
    result = await session.execute(
        select(models.ContentReport)
        .where(models.ContentReport.campus_id.in_(campus_ids))
        .order_by(models.ContentReport.created_at.desc())
    )
    return [ContentReportOut.model_validate(r) for r in result.scalars().all()]


@router.patch("/moderation/reports/{report_id}")
async def resolve_report(
    report_id: uuid.UUID,
    body: ReportResolveRequest,
    moderator: models.User = Depends(_require_any_eboard),
    session: AsyncSession = Depends(get_session),
) -> ContentReportOut:
    """Close a report as actioned or dismissed (board card c91).

    content_reports has carried a status column since it was created, and
    GET /moderation/reports has always returned it, but nothing could ever CHANGE it.
    So the moderation queue shipped in c35 could remove a reported yak and still not
    mark the report handled — it faked the transition client-side for the session and
    every handled item came back as open on the next reload. A queue that cannot be
    emptied is not a workflow, and this is the route that makes it one.

    SCOPING IS THE WHOLE RISK HERE, so it reuses _require_eboard_for_campus rather than
    rolling its own: reports carry campus_id, and SECURITY-REVIEW finding 1 was exactly
    an e-board member of one chapter being able to see reports from every campus. Being
    able to CLOSE another campus's report is the same defect with a write attached —
    worse, because dismissing a report is how a bad actor makes a complaint disappear.
    _require_any_eboard only proves the caller is e-board SOMEWHERE; the per-campus
    check below is what proves they moderate THIS report's campus.

    Idempotency: re-resolving an already-closed report is a 409, not a silent success.
    Two moderators working the same queue is the normal case, not an edge case, and the
    second one needs to know their decision did not land rather than believing it did.
    """
    report = await session.get(models.ContentReport, report_id)
    if report is None:
        raise not_found("report_not_found")
    # A report's tier follows its TARGET: a yak is campus-wide, a post or comment is
    # chapter content (c108). Dismissing a yak report is a campus moderation act;
    # dismissing a post report is not, so an unverified officer may still clear their
    # own chapter's queue.
    #
    # Scoping reads the loaded row, which is fine unlocked: a report's campus_id and
    # target_type are set once when it is filed and never change, so there is nothing
    # to race on here.
    await _require_eboard_for_campus(
        session, moderator, report.campus_id, campus_content=report.target_type == "yak"
    )

    # The STATUS transition is a different matter, and read-check-then-write would have
    # been wrong in exactly the case this route's docstring promises to handle. Two
    # moderators clicking at once would both read "open", both pass the check, and both
    # write — two audit rows, last-write-wins on status, and a 409 for neither. One
    # actions while the other dismisses and the queue silently keeps whichever committed
    # last. Sequentially correct, concurrently false, which is the harder half to see.
    #
    # So the guard IS the write: UPDATE ... WHERE status = 'open' lets the database
    # decide the winner, and a rowcount of 0 means someone else got there first. Same
    # shape as the dues double-charge reservation (c51) and c105's invite seat claim —
    # this is the third time this pattern has been the right answer in this codebase.
    claimed = await session.execute(
        update(models.ContentReport)
        .where(
            models.ContentReport.id == report_id,
            models.ContentReport.status == "open",
        )
        .values(status=body.status)
        .returning(models.ContentReport.id)
    )
    if claimed.scalar_one_or_none() is None:
        raise conflict("report_already_resolved")

    # Mutable state lives on the row, history lives in the append-only log — the same
    # split c76 chose for suspensions, and the reason "who dismissed this, and why" can
    # still be answered after a later moderator actions the same target.
    session.add(
        models.ModerationAction(
            actor_id=moderator.id,
            action="resolve_report",
            target_type="report",
            target_id=report.id,
            reason=body.reason,
        )
    )
    await session.commit()
    await session.refresh(report)
    return ContentReportOut.model_validate(report)


@router.post("/moderation/blocks", status_code=201)
async def create_block(
    body: UserBlockCreate,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> UserBlockOut:
    """Block another user; 409 if the block already exists (incl. a concurrent double-tap)."""
    existing = await session.get(models.UserBlock, (user.id, body.blocked_id))
    if existing is not None:
        raise conflict("already_blocked")
    block = models.UserBlock(blocker_id=user.id, blocked_id=body.blocked_id)
    session.add(block)
    try:
        await session.commit()
    except IntegrityError:
        # Concurrent duplicate insert race on the (blocker_id, blocked_id) PK.
        await session.rollback()
        raise conflict("already_blocked") from None
    await session.refresh(block)
    return UserBlockOut.model_validate(block)


@router.post("/moderation/blocks/by-yak/{yak_id}", status_code=204)
async def block_yak_author(
    yak_id: uuid.UUID,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Block the (anonymous) author of a yak without ever revealing who that author is.

    Resolves author_id server-side and returns no body — unlike POST /moderation/blocks,
    which echoes UserBlockOut(blocked_id=...). Echoing blocked_id here would let a caller
    block-by-yak on two different yaks and diff the response to learn whether they share
    an author, breaking the SPEC §8.3 anonymity invariant. For the same reason, an
    already-existing block (including one lost to a concurrent insert race) is treated
    as success (204) rather than 409: a 409-vs-204 split is itself a one-bit oracle for
    "have I already blocked this yak's author", which also leaks author identity across
    yaks. So this endpoint is idempotent by design, not just by convenience.
    """
    yak = await session.get(models.Yak, yak_id)
    if yak is None or yak.removed_at is not None:
        raise not_found("yak_not_found")
    # A FOURTH copy of the old `user.campus_id != yak.campus_id` comparison lived here
    # (c88). It was invisible to the dependency swap that fixed feed.py and yaks.py,
    # because it is hand-rolled inside the handler — which is precisely the failure mode
    # the shared module exists to end. Routing it through the same check keeps this
    # endpoint honest when the rule changes again.
    require_verified_campus(user, yak.campus_id)
    if yak.author_id == user.id:
        raise forbidden("cannot_block_self")

    # Unconditional idempotent upsert, deliberately: the earlier read-then-maybe-insert
    # returned the same 204 either way, but an already-blocked author short-circuited on
    # the read while a new block paid for an INSERT plus a commit. That latency gap is
    # itself the one-bit oracle the 204 was chosen to close — timing the response still
    # answered "do these two yaks share an author". Every call now does the same work.
    await session.execute(
        pg_insert(models.UserBlock)
        .values(blocker_id=user.id, blocked_id=yak.author_id)
        .on_conflict_do_nothing(index_elements=["blocker_id", "blocked_id"])
    )
    await session.commit()
    return None


@router.delete("/moderation/blocks", status_code=204)
async def delete_block(
    blocked_id: uuid.UUID,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Unblock a user (identified by ?blocked_id=); 404 if no such block."""
    block = await session.get(models.UserBlock, (user.id, blocked_id))
    if block is None:
        raise not_found("block_not_found")
    await session.delete(block)
    await session.commit()


@router.post("/moderation/yaks/{yak_id}/remove", status_code=204)
async def remove_yak(
    yak_id: uuid.UUID,
    body: YakRemoveRequest,
    moderator: models.User = Depends(_require_any_eboard),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Admin removal: sets removed_at + removed_reason, and writes a moderation_actions
    audit row (board card c76: who removed it, not just that it was removed).

    SECURITY-REVIEW finding 1: previously any e-board member of any chapter could
    remove any campus's yaks. Now requires the caller to be active e-board in some
    chapter whose campus matches the yak's campus.
    """
    yak = await session.get(models.Yak, yak_id)
    if yak is None:
        raise not_found("yak_not_found")
    if yak.removed_at is not None:
        raise conflict("already_removed")
    # Yaks are the campus-wide surface by definition (c108).
    await _require_eboard_for_campus(session, moderator, yak.campus_id, campus_content=True)
    yak.removed_at = datetime.now(timezone.utc)
    yak.removed_reason = body.reason
    session.add(
        models.ModerationAction(
            actor_id=moderator.id,
            action="remove_content",
            target_type="yak",
            target_id=yak.id,
            reason=body.reason,
        )
    )
    await session.commit()


@router.post("/moderation/content/remove", status_code=204)
async def remove_content(
    body: ContentRemoveRequest,
    moderator: models.User = Depends(_require_any_eboard),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Admin removal of a post or comment (board card c76: the Terms claims we can
    "remove content that breaks these rules" — this is what makes that true for named-
    author content; yaks have their own dedicated route above, anonymous-content shaped).

    Marks removed via the SAME deleted_at column the author/president self-delete in
    routers/feed.py already sets — that is what makes a moderator's removal actually
    disappear from feed.py's queries without this router being able to touch feed.py.
    removed_reason is what distinguishes the two paths: NULL means self/president
    delete, set means a moderator removed it. Writes a moderation_actions audit row
    either way (who, when, why).
    """
    chapter: models.Chapter | None
    if body.target_type == "post":
        post = await session.get(models.Post, body.target_id)
        if post is None:
            raise not_found("post_not_found")
        if post.deleted_at is not None:
            raise conflict("already_removed")
        chapter = await session.get(models.Chapter, post.chapter_id)
        target: models.Post | models.PostComment = post
    else:
        comment = await session.get(models.PostComment, body.target_id)
        if comment is None:
            raise not_found("comment_not_found")
        if comment.deleted_at is not None:
            raise conflict("already_removed")
        parent_post = await session.get(models.Post, comment.post_id)
        chapter = (
            await session.get(models.Chapter, parent_post.chapter_id)
            if parent_post is not None
            else None
        )
        target = comment

    campus_id = chapter.campus_id if chapter is not None else None
    # This route only accepts posts and comments (RemovableContentType) - both chapter
    # content, so officer role alone is the right bar. Yaks have their own route above,
    # which passes campus_content=True.
    await _require_eboard_for_campus(session, moderator, campus_id, campus_content=False)

    target.deleted_at = datetime.now(timezone.utc)
    target.removed_reason = body.reason
    session.add(
        models.ModerationAction(
            actor_id=moderator.id,
            action="remove_content",
            target_type=body.target_type,
            target_id=body.target_id,
            reason=body.reason,
        )
    )
    await session.commit()


@router.post("/moderation/users/{user_id}/suspend")
async def suspend_user(
    user_id: uuid.UUID,
    body: SuspendUserRequest,
    admin: models.User = Depends(require_platform_admin),
    session: AsyncSession = Depends(get_session),
) -> SuspensionStateOut:
    """Suspend an account platform-wide (board card c76, Terms: "we can suspend
    accounts that break these rules" / "we can suspend or close an account").

    Platform-admin only, same gate as chapter creation (SECURITY-REVIEW finding 1 /
    board card c28) — this is a platform-level power, not a chapter one, so EBOARD
    roles (scoped to a single chapter) are not sufficient here.

    Enforcement is NOT this endpoint's job: middleware/auth.get_current_user rejects
    every request from a suspended account from here on, so this just flips the state
    and records why.
    """
    target = await session.get(models.User, user_id)
    if target is None:
        raise not_found("user_not_found")
    if target.suspended_at is not None:
        raise conflict("already_suspended")
    target.suspended_at = datetime.now(timezone.utc)
    target.suspension_reason = body.reason
    target.suspended_by = admin.id
    session.add(
        models.ModerationAction(
            actor_id=admin.id,
            action="suspend_user",
            target_type="user",
            target_id=target.id,
            reason=body.reason,
        )
    )
    await session.commit()
    await session.refresh(target)
    return SuspensionStateOut.model_validate(target)


@router.post("/moderation/users/{user_id}/unsuspend")
async def unsuspend_user(
    user_id: uuid.UUID,
    body: SuspendUserRequest,
    admin: models.User = Depends(require_platform_admin),
    session: AsyncSession = Depends(get_session),
) -> SuspensionStateOut:
    """Restore a suspended account's access. Clears the users.suspended_* columns back
    to NULL — the moderation_actions row already written by suspend_user (and the one
    this call adds) is what keeps the history past that clear.
    """
    target = await session.get(models.User, user_id)
    if target is None:
        raise not_found("user_not_found")
    if target.suspended_at is None:
        raise conflict("not_suspended")
    target.suspended_at = None
    target.suspension_reason = None
    target.suspended_by = None
    session.add(
        models.ModerationAction(
            actor_id=admin.id,
            action="unsuspend_user",
            target_type="user",
            target_id=target.id,
            reason=body.reason,
        )
    )
    await session.commit()
    await session.refresh(target)
    return SuspensionStateOut.model_validate(target)
