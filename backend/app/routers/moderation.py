"""Moderation: content reports, user blocks, account suspension, and admin content
removal (chirps, posts, comments) — all with an audit trail (board card c76)."""
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
from app.schemas.chirp import (
    ContentReportCreate,
    ContentReportOut,
    UserBlockCreate,
    UserBlockOut,
    ChirpRemoveRequest,
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
    target (see list_reports, remove_chirp) so an e-board member of chapter A cannot see
    or act on chapter B's reports/chirps (SECURITY-REVIEW finding 1). Gating chapter
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

    chirp -> chirp.campus_id; post/comment -> the post's own campus_id;
    anything else (message_forward, user, or a missing/unresolvable target) falls back
    to the reporter's own users.campus_id, best-effort. Never trusts the client.

    Reads posts.campus_id rather than hopping through the post's chapter (c71). The
    hop is no longer reliable: a post by a chapter-less student has no chapter, so
    it would have fallen through to the reporter's campus — which silently files a
    report on the wrong campus's moderation queue if the reporter ever differs.
    """
    if target_id is not None and target_type == "chirp":
        chirp = await session.get(models.Chirp, target_id)
        if chirp is not None:
            return chirp.campus_id
    elif target_id is not None and target_type == "post":
        post = await session.get(models.Post, target_id)
        if post is not None:
            return post.campus_id
    elif target_id is not None and target_type == "comment":
        comment = await session.get(models.PostComment, target_id)
        if comment is not None:
            post = await session.get(models.Post, comment.post_id)
            if post is not None:
                return post.campus_id
    return reporter.campus_id


def _content_campus_tier(source_post: models.Post | None) -> bool:
    """Whether a piece of content is campus-wide, given its (possibly unresolvable)
    source post, for c108's tier (c139/c142/c147).

    Board card c147 (security's #66): factored out so BOTH call sites — reports
    (_report_campus_content, below) and admin removal (remove_content) — share one
    fail-closed default instead of two independently-hand-rolled ones that could
    silently disagree. They used to: remove_content defaulted to False when a
    comment's parent post could not be resolved, the OPPOSITE of this function's
    True. That was safe only by accident of evaluation order — campus_id is also
    None whenever source_post is, and _require_eboard_for_campus 403s on campus_id
    being None BEFORE campus_content is ever read, so the wrong default was never
    actually reachable. It would have become reachable the moment anyone changed
    that ordering, with nothing here to say the two defaults were coupled.

    A post or comment's tier follows its own `audience` column, not its type — a
    comment has none of its own, so it inherits its parent post's. An unresolvable
    source has no chapter-content justification to point to, so it defaults to the
    STRICTER campus tier rather than assuming it is safe to relax — the burden is
    on positively proving chapter content, not on disproving campus content.
    """
    if source_post is None:
        return True
    return source_post.audience == "campus"


async def _report_campus_content(
    session: AsyncSession, target_type: str, target_id: uuid.UUID | None
) -> bool:
    """Whether a report's target is campus-wide content, for c108's tier (c139/c142).

    THE BUG THIS CLOSES: the tier used to come from target_type alone
    (target_type == "chirp"), which was true only until c71 let a Post carry
    audience="campus" and publish to the campus feed. A campus-audience post's
    report was then keyed as chapter content — an unverified officer could
    dismiss it, the exact reverse of feed.py's create-side rule that PUBLISHING
    a campus post requires a verified .edu. Removing/dismissing must never be the
    weaker side of that pair.

    Chirps are campus-wide by definition; anything else defers to _content_campus_tier,
    which is where the unresolvable-target default actually lives (c147).
    """
    if target_type == "chirp":
        return True
    if target_type == "post" and target_id is not None:
        post = await session.get(models.Post, target_id)
        return _content_campus_tier(post)
    if target_type == "comment" and target_id is not None:
        comment = await session.get(models.PostComment, target_id)
        if comment is not None:
            post = await session.get(models.Post, comment.post_id)
            return _content_campus_tier(post)
    return True


async def _require_eboard_for_campus(
    session: AsyncSession,
    moderator: models.User,
    campus_id: uuid.UUID | None,
    *,
    campus_content: bool,
) -> None:
    """403 unless moderator is active e-board in a chapter of campus_id AND verified there.

    Factored out of remove_chirp (SECURITY-REVIEW finding 1's fix) so remove_content
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
    in both directions and this function serves BOTH tiers. It gates chirps, which are
    campus-wide, AND chapter posts and comments, which are org content. The first
    implementation of c108 put the verification check here unconditionally, which would
    have locked an unverified officer out of moderating their OWN CHAPTER'S posts — the
    exact opposite of the two-tier ruling that chapter membership grants chapter content
    in full with no email at all. Passing it explicitly at every call site makes the
    tier a decision someone had to make rather than a default they inherited.

    So: a president who never verified keeps every chapter power, including removing a
    member's post, and cannot touch the campus Chirp board.
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
    list_reports/remove_chirp can scope moderation to the right campus.
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
    So the moderation queue shipped in c35 could remove a reported chirp and still not
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
    # A report's tier follows its TARGET's actual audience, not its type (c108/c139/
    # c142) — a campus-audience post is campus-wide content wearing "post" as its
    # target_type, and dismissing that report is a campus moderation act just like
    # dismissing a chirp report is. Only a genuinely chapter-scoped post/comment lets an
    # unverified officer clear their own chapter's queue; _report_campus_content is
    # what tells the two apart.
    #
    # Scoping reads the loaded report row, which is fine unlocked: a report's
    # campus_id and target_type/target_id are set once when it is filed and never
    # change, so there is nothing to race on here.
    campus_content = await _report_campus_content(session, report.target_type, report.target_id)
    await _require_eboard_for_campus(
        session, moderator, report.campus_id, campus_content=campus_content
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
    """Block another user; 403 on self, 409 if the block already exists (incl. a concurrent double-tap)."""
    # c237. Self-blocking was accepted here while block_chirp_author below has always
    # refused it, so the same act was legal through one endpoint and forbidden through
    # the other. It is not harmless: contact is unaffected (blockers_of filters
    # subject_id out), but feed.py's c35 anti-join hides posts whose author the caller
    # has blocked, and that includes the caller themselves - so a self-block silently
    # removes your OWN posts from your own feed, which reads as data loss rather than
    # as a moderation setting. Same 403 cannot_block_self as by-chirp, deliberately:
    # one act, one status, one detail string.
    if body.blocked_id == user.id:
        raise forbidden("cannot_block_self")
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


@router.post("/moderation/blocks/by-chirp/{chirp_id}", status_code=204)
async def block_chirp_author(
    chirp_id: uuid.UUID,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Block the (anonymous) author of a chirp without ever revealing who that author is.

    Resolves author_id server-side and returns no body — unlike POST /moderation/blocks,
    which echoes UserBlockOut(blocked_id=...). Echoing blocked_id here would let a caller
    block-by-chirp on two different chirps and diff the response to learn whether they share
    an author, breaking the SPEC §8.3 anonymity invariant. For the same reason, an
    already-existing block (including one lost to a concurrent insert race) is treated
    as success (204) rather than 409: a 409-vs-204 split is itself a one-bit oracle for
    "have I already blocked this chirp's author", which also leaks author identity across
    chirps. So this endpoint is idempotent by design, not just by convenience.
    """
    chirp = await session.get(models.Chirp, chirp_id)
    if chirp is None or chirp.removed_at is not None:
        raise not_found("chirp_not_found")
    # A FOURTH copy of the old `user.campus_id != chirp.campus_id` comparison lived here
    # (c88). It was invisible to the dependency swap that fixed feed.py and chirps.py,
    # because it is hand-rolled inside the handler — which is precisely the failure mode
    # the shared module exists to end. Routing it through the same check keeps this
    # endpoint honest when the rule changes again.
    require_verified_campus(user, chirp.campus_id)
    if chirp.author_id == user.id:
        raise forbidden("cannot_block_self")

    # Unconditional idempotent upsert, deliberately: the earlier read-then-maybe-insert
    # returned the same 204 either way, but an already-blocked author short-circuited on
    # the read while a new block paid for an INSERT plus a commit. That latency gap is
    # itself the one-bit oracle the 204 was chosen to close — timing the response still
    # answered "do these two chirps share an author". Every call now does the same work.
    await session.execute(
        pg_insert(models.UserBlock)
        .values(blocker_id=user.id, blocked_id=chirp.author_id)
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


@router.post("/moderation/chirps/{chirp_id}/remove", status_code=204)
async def remove_chirp(
    chirp_id: uuid.UUID,
    body: ChirpRemoveRequest,
    moderator: models.User = Depends(_require_any_eboard),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Admin removal: sets removed_at + removed_reason, and writes a moderation_actions
    audit row (board card c76: who removed it, not just that it was removed).

    SECURITY-REVIEW finding 1: previously any e-board member of any chapter could
    remove any campus's chirps. Now requires the caller to be active e-board in some
    chapter whose campus matches the chirp's campus.
    """
    chirp = await session.get(models.Chirp, chirp_id)
    if chirp is None:
        raise not_found("chirp_not_found")
    if chirp.removed_at is not None:
        raise conflict("already_removed")
    # Chirps are the campus-wide surface by definition (c108).
    await _require_eboard_for_campus(session, moderator, chirp.campus_id, campus_content=True)
    chirp.removed_at = datetime.now(timezone.utc)
    chirp.removed_reason = body.reason
    session.add(
        models.ModerationAction(
            actor_id=moderator.id,
            action="remove_content",
            target_type="chirp",
            target_id=chirp.id,
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
    author content; chirps have their own dedicated route above, anonymous-content shaped).

    Marks removed via the SAME deleted_at column the author/president self-delete in
    routers/feed.py already sets — that is what makes a moderator's removal actually
    disappear from feed.py's queries without this router being able to touch feed.py.
    removed_reason is what distinguishes the two paths: NULL means self/president
    delete, set means a moderator removed it. Writes a moderation_actions audit row
    either way (who, when, why).
    """
    if body.target_type == "post":
        post = await session.get(models.Post, body.target_id)
        if post is None:
            raise not_found("post_not_found")
        if post.deleted_at is not None:
            raise conflict("already_removed")
        target: models.Post | models.PostComment = post
        source_post = post
    else:
        comment = await session.get(models.PostComment, body.target_id)
        if comment is None:
            raise not_found("comment_not_found")
        if comment.deleted_at is not None:
            raise conflict("already_removed")
        target = comment
        source_post = await session.get(models.Post, comment.post_id)

    # campus_id and the c108 tier both come straight off the POST row (c139/c142) -
    # never through Chapter, which this route used to hop for campus_id alone and
    # which broke two ways at once:
    #
    # (1) campus_content was hard-coded False here on the theory that a post/comment
    #     is always chapter content. c71 made that false: a Post can carry
    #     audience="campus" and publish to the campus feed, same as a chirp. That made
    #     REMOVING a campus post require less than PUBLISHING one required
    #     (feed.py's create route demands a verified .edu for audience="campus") -
    #     backwards, and exploitable by an unverified officer in a different chapter
    #     on the same campus.
    #
    # (2) Chapter.campus_id is unreachable for a chapter-less campus post
    #     (chapter_id NULL, allowed by ck_posts_org_requires_chapter since c71) - the
    #     hop silently produced campus_id=None, which _require_eboard_for_campus
    #     treats as "no campus matches" and 403s EVERY officer, verified or not.
    #     Post.campus_id is a first-class, non-nullable column set unconditionally by
    #     both create routes in feed.py, so reading it directly fixes both bugs with
    #     one change - the same shift _resolve_report_campus_id already made above
    #     for the reports path, for the identical reason.
    #
    # A comment carries neither column itself, so it inherits its parent post's.
    # campus_content used to be hand-rolled here with the wrong default on an
    # unresolvable source post - see _content_campus_tier's docstring (c147) for why
    # that was silent and how it agrees with _report_campus_content now.
    campus_id = source_post.campus_id if source_post is not None else None
    campus_content = _content_campus_tier(source_post)
    await _require_eboard_for_campus(session, moderator, campus_id, campus_content=campus_content)

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
