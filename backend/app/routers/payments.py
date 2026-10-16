"""Payments: Stripe Connect onboarding, dues PaymentIntents, and the webhook sink."""
import asyncio
import contextvars
import hashlib
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import Select, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.config import get_settings
from app.core.analytics import emit
from app.core.dues_status import dues_contributions_subquery
from app.core.errors import conflict, forbidden, is_cross_table_dues_guard_conflict, not_found
from app.core.permissions import Role, require_role
from app.db import get_session
from app.middleware.auth import get_current_user
from app.middleware.org_scope import get_current_membership
from app.schemas.payments import (
    ChapterPaymentsStatusOut,
    ConnectOnboardingOut,
    ConnectOnboardingRequest,
    DuesIntentCreate,
    DuesIntentOut,
)
from app.services import stripe_service
from app.schemas.authority import PaymentAuthorityContext, PaymentAuthorityInput
from app.services.organization_authority import check_payment_declaration, payment_context, record_authority
from app.services.settlement_binding import BoundSettlement, bind_settlement
from app.services.account_external_operations import (
    ExternalOperationConflict,
    ExternalOperationHandle,
    finish_external_operation,
    register_external_operation,
)
from app.services.account_deletion_hold import DeletionHoldActive, DeletionHoldInvalid
from app.services.account_payment_operations import (
    PaymentOperationConflict,
    PaymentOperationInvalid,
    PaymentEffectHandle,
    PaymentAttemptHandle,
    finish_payment_attempt,
    mark_payment_attempt_uncertain,
    register_payment_attempt,
    register_payment_effect,
)
from app.services.account_write_guard import bind_account_write_guard

router = APIRouter(tags=["payments"])
logger = logging.getLogger(__name__)

# c234: known intents may be retired after confirmed provider cancellation.
RESERVATION_TTL = timedelta(hours=24)
# c366: leave a margin inside Stripe's minimum 24h idempotency retention.
# Unknown no-ID outcomes are never released or recreated after this window.
CREATE_RETRY_WINDOW = timedelta(hours=23)
RESERVATION_PROVIDER_TIMEOUT_SECONDS = 15.0

# c234: PaymentIntent statuses that mean Stripe has moved past "waiting on the
# member" — a same-rail retrieve landing on one of these must not hand the client
# a client_secret that LOOKS like a fresh checkout.
_INTENT_NOT_AWAITING_PAYMENT = {"processing", "succeeded"}
_ACTIVE_PAYMENT_ATTEMPTS: contextvars.ContextVar[list[tuple[
    ExternalOperationHandle, PaymentEffectHandle, PaymentAttemptHandle,
]] | None] = contextvars.ContextVar("active_payment_attempts", default=None)


def _payment_fingerprint(**parameters: object) -> str:
    """Hash exact Stripe parameters; never persist email/name or capability data."""
    encoded = json.dumps(parameters, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _payment_identity(kind: str, stable_identity: str) -> tuple[str, str]:
    digest = hashlib.sha256(f"{kind}:{stable_identity}".encode("utf-8")).hexdigest()
    return digest, f"chirp:{kind}:{digest[:40]}"


def _idempotency_kwargs(key: str | None) -> dict[str, str]:
    # Preserve exact legacy adapter call signatures while flag-off.
    return {"idempotency_key": key} if key is not None else {}


def _valid_provider_id(value: object, prefix: str) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(rf"{re.escape(prefix)}_[A-Za-z0-9]{{1,240}}", value) is not None
    )


def _capability_http_url(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 2048:
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    try:
        port = parsed.port  # Reject malformed ports even though the host is provider-owned.
        return (
            parsed.scheme == "https"
            and parsed.hostname == "connect.stripe.com"
            and port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and bool(parsed.path)
        )
    except ValueError:
        return False


@asynccontextmanager
async def _admitted_payment_request(
    session: AsyncSession, user: models.User,
) -> AsyncIterator[ExternalOperationHandle | None]:
    """Keep the admitted request visible until route SQL and provider awaits drain."""
    if not get_settings().account_deletion_http_admission_enabled:
        yield None
        return

    user_id, firebase_uid = user.id, user.firebase_uid
    # Finish the read-only authorization transaction before the fresh admission
    # transaction. Registration commits before any payment-domain write or Stripe IO.
    await session.commit()
    session.expire_all()
    request_id = uuid.uuid4()
    try:
        parent = await register_external_operation(
            user_id=user_id,
            firebase_uid=firebase_uid,
            kind="payment_request",
            effect_key=hashlib.sha256(request_id.bytes).hexdigest(),
        )
    except DeletionHoldActive:
        raise
    except DeletionHoldInvalid as exc:
        if str(exc) == "account_deletion_hold_active":
            raise DeletionHoldActive(str(exc)) from None
        raise HTTPException(status_code=503, detail="payment_unavailable") from None
    except ExternalOperationConflict:
        raise HTTPException(status_code=409, detail="payment_request_already_started") from None

    active: list[tuple[ExternalOperationHandle, PaymentEffectHandle, PaymentAttemptHandle]] = []
    token = _ACTIVE_PAYMENT_ATTEMPTS.set(active)

    async def cleanup_failed_request() -> None:
        # The route/provider coroutine has unwound before this runs. Release SQL
        # first, then fence any result that never reached its atomic domain commit.
        try:
            await session.rollback()
        except BaseException:
            return
        for attempt_parent, effect, attempt in active:
            try:
                await mark_payment_attempt_uncertain(attempt_parent, effect, attempt)
            except BaseException:
                # Finished/reconciled children reject this transition; a failed
                # journal write remains running and the parent finish then fails.
                pass
        try:
            await finish_external_operation(parent, state="finished")
        except BaseException:
            # A still-running child or DB failure leaves the parent blocking.
            pass

    try:
        yield parent
        await session.commit()
        await finish_external_operation(parent, state="finished")
    except BaseException:
        cleanup = asyncio.create_task(cleanup_failed_request())
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # Repeated request cancellation cannot detach uncertainty writes.
                continue
            except BaseException:
                break
        if cleanup.done():
            try:
                cleanup.result()
            except BaseException:
                pass
        raise
    finally:
        _ACTIVE_PAYMENT_ATTEMPTS.reset(token)


async def _register_effect_attempt(
    parent: ExternalOperationHandle | None,
    *,
    kind: str,
    stable_identity: str,
    parameters: dict[str, object],
    connected_account_id: str | None,
    reservation_id: uuid.UUID | None = None,
    idempotency_key_override: str | None = None,
) -> tuple[PaymentEffectHandle | None, PaymentAttemptHandle | None]:
    """Register a stable logical effect and owned attempt before Stripe IO."""
    if parent is None:
        return None, None
    effect_key, generated_idempotency_key = _payment_identity(kind, stable_identity)
    idempotency_key = idempotency_key_override or generated_idempotency_key
    try:
        effect = await register_payment_effect(
            parent,
            kind=kind,
            effect_key=effect_key,
            request_fingerprint=_payment_fingerprint(**parameters),
            idempotency_key=idempotency_key,
            connected_account_id=connected_account_id,
            reservation_id=reservation_id,
        )
        attempt = await register_payment_attempt(parent, effect)
    except PaymentOperationConflict:
        raise HTTPException(status_code=409, detail="payment_reconciliation_required") from None
    except PaymentOperationInvalid:
        raise HTTPException(status_code=503, detail="payment_unavailable") from None
    active = _ACTIVE_PAYMENT_ATTEMPTS.get()
    if active is not None:
        active.append((parent, effect, attempt))
    return effect, attempt


async def _run_payment_effect(
    parent: ExternalOperationHandle | None,
    *,
    kind: str,
    stable_identity: str,
    parameters: dict[str, object],
    connected_account_id: str | None,
    reservation_id: uuid.UUID | None = None,
    idempotency_key_override: str | None = None,
    invoke: Callable[[str | None], Awaitable[object]],
) -> tuple[object, PaymentEffectHandle | None, PaymentAttemptHandle | None]:
    effect, attempt = await _register_effect_attempt(
        parent, kind=kind, stable_identity=stable_identity,
        parameters=parameters, connected_account_id=connected_account_id,
        reservation_id=reservation_id,
        idempotency_key_override=idempotency_key_override,
    )
    if parent is None:
        return await invoke(None), None, None
    try:
        result = await invoke(effect.idempotency_key)
    except BaseException:
        # The provider may have accepted the operation before raising or being
        # canceled. If this write fails, the committed running row still blocks.
        try:
            await mark_payment_attempt_uncertain(parent, effect, attempt)
        except BaseException:
            pass
        raise
    return result, effect, attempt


async def _finish_payment_effect(
    session: AsyncSession,
    parent: ExternalOperationHandle | None,
    effect: PaymentEffectHandle | None,
    attempt: PaymentAttemptHandle | None,
    *,
    provider_id: str | None = None,
) -> None:
    if parent is not None and effect is not None and attempt is not None:
        await finish_payment_attempt(
            session, parent, effect, attempt, provider_id=provider_id
        )


async def _final_payment_actor_check(
    session: AsyncSession, *, user_id: uuid.UUID, firebase_uid: str,
) -> models.User:
    """Use the normal SQL write gate only for the final capability handoff."""
    bind_account_write_guard(
        session, user_id=user_id, firebase_uid=firebase_uid
    )
    return await get_current_user(uid=firebase_uid, session=session)


# c427: this route was the ONLY provider I/O in payments.py with no handler on it.
# The dues path has caught stripe.StripeError since c366, and app/main.py registers no
# catch-all (its one handler is for SQLAlchemy's TimeoutError), so a Stripe timeout, a
# rate limit, a key rotated mid-flight or a Connect capability problem left the
# exception to escape the route entirely. On the deployed service that becomes an
# opaque 500 whose body is not the app's usual {"detail": ...} shape at all, so the
# phone could not map it to anything: the treasurer screen showed "Couldn't open Stripe
# setup" over a generic body, and no part of the response said which layer failed.
# That is both a real defect and the reason a phone report of this flow could not be
# diagnosed. Proven uncaught before this change by tests/test_c427_onboarding_provider_errors.py.
ONBOARDING_PROVIDER_DETAIL = "onboarding_unavailable"

_STRIPE_REQUEST_ID_RE = re.compile(r"req_[A-Za-z0-9]{1,64}\Z")
_STRIPE_CODE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
_STRIPE_PARAM_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9_.]*(?:\[[A-Za-z0-9_.-]+\])*\Z"
)
_STRIPE_SECRET_PREFIXES = ("sk_", "rk_", "pk_", "whsec_")


def _safe_stripe_request_id(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 68:
        return None
    return value if _STRIPE_REQUEST_ID_RE.fullmatch(value) else None


def _safe_stripe_code(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    if value.lower().startswith(_STRIPE_SECRET_PREFIXES):
        return None
    return value if _STRIPE_CODE_RE.fullmatch(value) else None


def _safe_stripe_param(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 128:
        return None
    components = (part for part in re.split(r"[.\[\]]+", value) if part)
    if any(part.lower().startswith(_STRIPE_SECRET_PREFIXES) for part in components):
        return None
    return value if _STRIPE_PARAM_RE.fullmatch(value) else None


def _safe_stripe_http_status(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 100 <= value <= 599 else None


def _onboarding_provider_failure(
    exc: stripe.StripeError, chapter_id: uuid.UUID, step: str,
) -> HTTPException:
    """A clean 503 for a Stripe-side onboarding failure, mirroring the dues path.

    Logs only bounded provider correlation fields, never str(exc), the body, headers,
    or request data. Stripe's message and response body can contain credential
    material, so each optional field is allowlisted before it reaches Cloud Logging.

    No session.rollback() here, unlike the dues path: both call sites below have
    already committed and expired their session, so there is no in-flight money row
    for a Stripe failure to leave half-written.
    """
    logger.warning(
        "c427: Connect onboarding stopped chapter=%s step=%s error=%s code=%s "
        "request_id=%s http_status=%s param=%s",
        chapter_id,
        step,
        type(exc).__name__,
        _safe_stripe_code(getattr(exc, "code", None)),
        _safe_stripe_request_id(getattr(exc, "request_id", None)),
        _safe_stripe_http_status(getattr(exc, "http_status", None)),
        _safe_stripe_param(getattr(exc, "param", None)),
    )
    return HTTPException(status_code=503, detail=ONBOARDING_PROVIDER_DETAIL)


def _onboarding_urls() -> tuple[str, str]:
    """(return_url, refresh_url) for Connect onboarding.

    Stripe rejects custom schemes, so these cannot be chirp:// deep links — they
    point at APP_PUBLIC_BASE_URL, which is expected to bounce the member back into
    the app. 503 rather than sending Stripe a URL we know is wrong.
    """
    base = get_settings().app_public_base_url
    if not base:
        raise HTTPException(status_code=503, detail="app_public_base_url_not_configured")
    base = base.rstrip("/")
    return f"{base}/stripe/connect/return", f"{base}/stripe/connect/refresh"


async def _onboarding_chapter(
    session: AsyncSession, user: models.User, chapter_id: uuid.UUID,
) -> models.Chapter:
    """Check current onboarding role and return the chapter in this transaction."""
    result = await session.execute(
        select(models.Membership.role).where(
            models.Membership.user_id == user.id,
            models.Membership.chapter_id == chapter_id,
            models.Membership.status == "active",
        )
    )
    role = result.scalar_one_or_none()
    if role is None:
        raise forbidden("not_a_member")
    if role not in (Role.treasurer.value, Role.president.value):
        raise forbidden("insufficient_role")

    chapter = await session.get(models.Chapter, chapter_id)
    if chapter is None:
        raise not_found("chapter_not_found")

    return chapter


@router.get("/chapters/{chapter_id}/payments/authority", response_model=PaymentAuthorityContext)
async def get_payment_authority(
    chapter_id: uuid.UUID,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    chapter = await _onboarding_chapter(session, user, chapter_id)
    return await payment_context(session, user, chapter)


@router.post("/payments/connect/onboarding-link")
async def create_connect_onboarding_link(
    body: ConnectOnboardingRequest,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ConnectOnboardingOut:
    """Start (or resume) Stripe Connect onboarding for a chapter; treasurer/president.

    Creates the Express account on first call and reuses it afterwards, so a member
    who abandons onboarding halfway resumes the same account instead of orphaning it.
    """
    chapter = await _onboarding_chapter(session, user, body.chapter_id)
    context = await check_payment_declaration(session, user, chapter, body.authority)
    uid, user_id, chapter_id = user.firebase_uid, user.id, chapter.id
    declaration = body.authority
    if get_settings().account_deletion_http_admission_enabled:
        async with _admitted_payment_request(session, user) as parent:
            # The request was admitted only after ordinary role/declaration auth.
            # Refresh within the admitted operation before recording authority.
            user = await get_current_user(uid=uid, session=session)
            chapter = await _onboarding_chapter(session, user, chapter_id)
            context = await check_payment_declaration(session, user, chapter, declaration)
            return await _connect_onboarding_flow(
                session, user, chapter, declaration, context, parent
            )

    return await _connect_onboarding_flow(session, user, chapter, declaration, context, None)


async def _connect_onboarding_flow(
    session: AsyncSession,
    user: models.User,
    chapter: models.Chapter,
    declaration: PaymentAuthorityInput | None,
    context: PaymentAuthorityContext | None,
    parent: ExternalOperationHandle | None,
) -> ConnectOnboardingOut:
    if context is not None:
        await record_authority(
            session, user_id=user.id, chapter_id=chapter.id,
            membership_id=context.membership_id, role_term_id=context.role_term_id,
            role=context.role, purpose="payment_setup", stripe_account_id=chapter.stripe_account_id,
        )
    uid, user_id, chapter_id, org_name = user.firebase_uid, user.id, chapter.id, chapter.org_name
    account_id = chapter.stripe_account_id
    return_url, refresh_url = _onboarding_urls()
    await session.commit()
    session.expire_all()

    if account_id is None:
        stable = str(chapter_id)
        try:
            candidate_result, effect, attempt = await _run_payment_effect(
                parent,
                kind="connect_account_create",
                stable_identity=stable,
                parameters={"chapter_id": str(chapter_id), "org_name": org_name, "type": "express", "country": "US"},
                connected_account_id=None,
                invoke=lambda key: stripe_service.create_express_account(
                    chapter_id, org_name,
                    **_idempotency_kwargs(key),
                    **({"return_resource": True} if parent is not None else {}),
                ),
            )
        except stripe.StripeError as exc:
            raise _onboarding_provider_failure(exc, chapter_id, "create_account") from None
        candidate = (
            getattr(candidate_result, "id", None)
            if parent is not None else str(candidate_result)
        )
        if parent is not None and (
            getattr(candidate_result, "object", None) != "account"
            or not _valid_provider_id(candidate, "acct")
        ):
            if effect is not None and attempt is not None:
                await mark_payment_attempt_uncertain(parent, effect, attempt)
            raise HTTPException(status_code=503, detail="payment_outcome_unconfirmed")
        try:
            user = await get_current_user(uid=uid, session=session)
            chapter = await _onboarding_chapter(session, user, chapter_id)
            await check_payment_declaration(session, user, chapter, declaration)
            await session.execute(
                update(models.Chapter).where(
                    models.Chapter.id == chapter_id, models.Chapter.stripe_account_id.is_(None),
                ).values(stripe_account_id=candidate).execution_options(synchronize_session=False)
            )
            winner = (await session.execute(
                select(models.Chapter.stripe_account_id).where(models.Chapter.id == chapter_id)
            )).scalar_one()
            if winner != candidate:
                if parent is not None:
                    await session.rollback()
                    if effect is not None and attempt is not None:
                        await mark_payment_attempt_uncertain(parent, effect, attempt)
                    raise HTTPException(status_code=503, detail="payment_outcome_unconfirmed")
                # Legacy behavior: the competing association wins and the newly
                # created but unused provider account remains inert.
                account_id = winner
            else:
                account_id = winner
            if declaration is not None:
                declaration = declaration.model_copy(update={"stripe_account_id": account_id})
                await record_authority(
                    session, user_id=user.id, chapter_id=chapter.id,
                    membership_id=declaration.membership_id, role_term_id=declaration.role_term_id,
                    role=declaration.role, purpose="payment_setup", stripe_account_id=account_id,
                )
            await _finish_payment_effect(session, parent, effect, attempt, provider_id=candidate)
            await session.commit()
            session.expire_all()
        except BaseException:
            await session.rollback()
            raise

    try:
        link_result, effect, attempt = await _run_payment_effect(
            parent,
            kind="connect_account_link",
            stable_identity=f"{parent.operation_id if parent else uuid.uuid4()}:{account_id}",
            parameters={"account_id": account_id, "return_url": return_url, "refresh_url": refresh_url},
            connected_account_id=account_id,
            invoke=lambda key: stripe_service.create_account_link(
                account_id, return_url, refresh_url, **_idempotency_kwargs(key)
            ),
        )
    except stripe.StripeError as exc:
        raise _onboarding_provider_failure(exc, chapter_id, "account_link") from None
    link = link_result
    if parent is not None and (
        getattr(link, "object", None) != "account_link"
        or not _capability_http_url(getattr(link, "url", None))
        or isinstance(getattr(link, "expires_at", None), bool)
        or not isinstance(getattr(link, "expires_at", None), int)
        or getattr(link, "expires_at") <= int(datetime.now(timezone.utc).timestamp())
    ):
        if effect is not None and attempt is not None:
            await mark_payment_attempt_uncertain(parent, effect, attempt)
        raise HTTPException(status_code=503, detail="payment_outcome_unconfirmed")
    await _finish_payment_effect(session, parent, effect, attempt)
    await session.commit()
    if parent is not None:
        user = await _final_payment_actor_check(session, user_id=user_id, firebase_uid=uid)
    else:
        user = await get_current_user(uid=uid, session=session)
    chapter = await _onboarding_chapter(session, user, chapter_id)
    if chapter.stripe_account_id != account_id:
        raise conflict("chapter_not_onboarded")
    await check_payment_declaration(session, user, chapter, declaration)
    response = ConnectOnboardingOut(
        url=link.url,
        expires_at=datetime.fromtimestamp(link.expires_at, tz=timezone.utc),
    )
    if parent is not None:
        await session.commit()
    return response


@router.get("/chapters/{chapter_id}/payments/status")
async def get_chapter_payments_status(
    chapter_id: uuid.UUID,
    _membership: models.Membership = Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
    user: models.User = Depends(get_current_user),
) -> ChapterPaymentsStatusOut:
    """Whether the chapter can accept dues yet; read live from Stripe, any member.

    Status is not mirrored into our tables on purpose: onboarding state changes on
    Stripe's side (verification, document review) with no request to us, so a stored
    copy would go stale and tell members dues are payable when they are not.
    """
    chapter = await session.get(models.Chapter, chapter_id)
    if chapter is None:
        raise not_found("chapter_not_found")
    if chapter.stripe_account_id is None:
        return ChapterPaymentsStatusOut(
            onboarded=False, charges_enabled=False, details_submitted=False
        )

    account_id, uid = chapter.stripe_account_id, user.firebase_uid
    await session.commit()
    session.expire_all()
    account = await stripe_service.retrieve_account(account_id)
    user = await get_current_user(uid=uid, session=session)
    await get_current_membership(chapter_id=chapter_id, user=user, session=session)
    chapter = await session.get(models.Chapter, chapter_id)
    if chapter is None:
        raise not_found("chapter_not_found")
    if chapter.stripe_account_id != account_id:
        raise conflict("chapter_not_onboarded")
    charges_enabled = bool(account.charges_enabled)
    details_submitted = bool(account.details_submitted)
    return ChapterPaymentsStatusOut(
        onboarded=charges_enabled and details_submitted,
        charges_enabled=charges_enabled,
        details_submitted=details_submitted,
    )


async def _bind_customer(
    session: AsyncSession, user_id: uuid.UUID, chapter_id: uuid.UUID, candidate_id: str,
    *, parent: ExternalOperationHandle | None = None,
    effect: PaymentEffectHandle | None = None,
    attempt: PaymentAttemptHandle | None = None,
) -> tuple[str, bool]:
    """Bind a provider result after revalidation, retaining a concurrent winner.

    Commit a new binding before reservation ownership so its unique-key lock does
    not queue another request ahead of c366's NOWAIT guard. The caller revalidates
    after that commit. Competing unused Customers remain inert, as before.
    """
    lookup = select(models.ChapterStripeCustomer.stripe_customer_id).where(
        models.ChapterStripeCustomer.user_id == user_id,
        models.ChapterStripeCustomer.chapter_id == chapter_id,
    )
    existing = await session.scalar(lookup)
    if existing is not None:
        if existing != candidate_id:
            if parent is not None:
                await session.rollback()
                if effect is not None and attempt is not None:
                    await mark_payment_attempt_uncertain(parent, effect, attempt)
                raise HTTPException(status_code=503, detail="payment_outcome_unconfirmed")
            return existing, False
        await _finish_payment_effect(session, parent, effect, attempt, provider_id=candidate_id)
        if parent is not None:
            await session.commit()
            session.expire_all()
        return existing, False
    await session.execute(
        pg_insert(models.ChapterStripeCustomer)
        .values(user_id=user_id, chapter_id=chapter_id, stripe_customer_id=candidate_id)
        .on_conflict_do_nothing(index_elements=["user_id", "chapter_id"])
    )
    winner = (await session.execute(lookup)).scalar_one()
    if winner != candidate_id:
        if parent is not None:
            await session.rollback()
            if effect is not None and attempt is not None:
                await mark_payment_attempt_uncertain(parent, effect, attempt)
            raise HTTPException(status_code=503, detail="payment_outcome_unconfirmed")
    await _finish_payment_effect(session, parent, effect, attempt, provider_id=candidate_id)
    await session.commit()
    session.expire_all()
    return winner, True


async def _load_dues_context(
    session: AsyncSession, user: models.User, cycle_id: uuid.UUID,
) -> tuple[models.DuesCycle, models.Chapter]:
    """Apply the same eligibility checks before and after provider preparation."""
    cycle = await session.get(models.DuesCycle, cycle_id)
    if cycle is None:
        raise not_found("dues_cycle_not_found")

    membership = await session.execute(
        select(models.Membership.id).where(
            models.Membership.user_id == user.id,
            models.Membership.chapter_id == cycle.chapter_id,
            models.Membership.status == "active",
        )
    )
    if membership.scalar_one_or_none() is None:
        raise forbidden("not_a_member")

    # c195: a member on an ACTIVE payment plan pays this cycle in installments, not
    # through this self-serve full-cycle charge path. Checked BEFORE the existence
    # guard below so the reason surfaced is specific (on_payment_plan) rather than
    # the generic already_paid/refunded_contact_treasurer split, which assumes a
    # single lump-sum obligation and would be a confusing lie to a plan member who
    # has paid some, but not all, of what they owe. The mobile client is expected to
    # gate this itself (it shows the installment schedule instead of a pay button for
    # a plan member), but that is a client-side courtesy, not the security boundary —
    # this is.
    active_plan = await session.execute(
        select(models.DuesPaymentPlan.id).where(
            models.DuesPaymentPlan.dues_cycle_id == cycle_id,
            models.DuesPaymentPlan.user_id == user.id,
            models.DuesPaymentPlan.status == "active",
        )
    )
    if active_plan.scalar_one_or_none() is not None:
        raise conflict("on_payment_plan")

    # EXISTENCE still blocks the charge path (board c172) — re-payment for one dues
    # cycle is structurally UNREPRESENTABLE in this ledger today, not merely
    # undesirable: uq_ledger_dues_payment_once allows at most one dues_payment row
    # per (cycle, member) EVER, so a second Stripe payment settling here would
    # capture real money at Stripe and then silently have no ledger row to show for
    # it when the webhook's insert loses to that constraint (see stripe_webhook
    # and the RESIDUAL EDGE note below). An earlier version of this fix let net<=0
    # (a full refund) through to Stripe, which is exactly the money-loss path — closed
    # by going back to existence, not by NOT netting at all: dues_contributions_subquery
    # (same definition chapter_overview reads, board c171) still decides WHICH honest
    # reason accompanies the block, so the two surfaces agree on whether the member
    # owes money even though this endpoint cannot yet let them pay again. Actually
    # reopening self-serve repayment after a refund needs the cycle/member's dues
    # status modeled explicitly (c83-shaped work), not a same-day guard change.
    #
    # c195 ADDS 'dues_installment' to this existence check's entry_type filter. A
    # member who FINISHED a payment plan has entry_type='dues_installment' rows on
    # the ledger and — because they never paid through this lump-sum path — NO
    # dues_payment row. Leaving this filtered to 'dues_payment' alone would silently
    # reopen exactly the double-charge this whole guard exists to close: the active-
    # plan check above only catches an IN-PROGRESS plan, so a completed one would
    # sail through both checks and let this route create a second, full-price Stripe
    # intent for a cycle already paid off in installments. Matching both types here
    # reuses the existing net-based reason split below rather than inventing a new
    # one — dues_contributions_subquery already nets both types together (c195,
    # app/core/dues_status.py), so a completed plan's net reads as >= the cycle
    # amount and this correctly resolves to already_paid.
    # LIMIT 1: unlike a bare 'dues_payment' row (uq_ledger_dues_payment_once caps that
    # at exactly one per cycle/member, so scalar_one_or_none() used to be safe on its
    # own), a member can have SEVERAL 'dues_installment' rows for one cycle — one per
    # installment recorded. Without the limit, scalar_one_or_none() raises
    # MultipleResultsFound the moment a plan member has paid a second installment;
    # this query only needs to know whether at least one qualifying row exists.
    existing_payment = await session.execute(
        select(models.LedgerEntry.id)
        .where(
            models.LedgerEntry.dues_cycle_id == cycle_id,
            models.LedgerEntry.related_user_id == user.id,
            models.LedgerEntry.entry_type.in_(("dues_payment", "dues_installment")),
        )
        .limit(1)
    )
    if existing_payment.scalar_one_or_none() is not None:
        contributions = dues_contributions_subquery(cycle.chapter_id, cycle.id)
        net_cents = await session.scalar(
            select(func.coalesce(func.sum(contributions.c.amount_cents), 0)).where(
                contributions.c.user_id == user.id
            )
        )
        # net > 0: the money is genuinely still in hand — "already_paid" (unchanged).
        # net <= 0: a correction refunded it, and the member DOES owe again, but this
        # endpoint cannot self-serve that yet — say so rather than repeating the
        # already_paid lie the President overview no longer tells.
        raise conflict("already_paid" if net_cents > 0 else "refunded_contact_treasurer")

    chapter = await session.get(models.Chapter, cycle.chapter_id)
    if chapter is None or chapter.stripe_account_id is None:
        raise conflict("chapter_not_onboarded")
    return cycle, chapter


@router.post("/payments/dues/{cycle_id}/intent")
async def create_dues_payment_intent(
    cycle_id: uuid.UUID,
    body: DuesIntentCreate,
    user: models.User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> DuesIntentOut:
    """Create a dues PaymentIntent for the caller; active members of the cycle's chapter.

    The amount comes from the dues cycle, never the client. The rail arrives in the
    body because application_fee_amount is fixed at creation time.
    """
    cycle, chapter = await _load_dues_context(session, user, cycle_id)
    chapter_id, account_id = chapter.id, chapter.stripe_account_id
    uid, actor_user_id = user.firebase_uid, user.id
    async with _admitted_payment_request(session, user) as parent:
        if parent is not None:
            user = await get_current_user(uid=uid, session=session)
            cycle, chapter = await _load_dues_context(session, user, cycle_id)
            if chapter.id != chapter_id or chapter.stripe_account_id != account_id:
                raise conflict("chapter_not_onboarded")
        email, display_name = user.email, user.display_name
        customer_id = await session.scalar(
            select(models.ChapterStripeCustomer.stripe_customer_id).where(
                models.ChapterStripeCustomer.user_id == actor_user_id,
                models.ChapterStripeCustomer.chapter_id == chapter_id,
            )
        )
        await session.commit()  # eligibility and mapping reads precede provider preparation
        session.expire_all()
        account = await stripe_service.retrieve_account(account_id)
        if not account.charges_enabled:
            raise conflict("chapter_not_onboarded")

        customer_effect = customer_attempt = None
        if customer_id is None:
            candidate_result, customer_effect, customer_attempt = await _run_payment_effect(
                parent,
                kind="customer_create",
                stable_identity=f"{actor_user_id}:{chapter_id}:{account_id}",
                parameters={
                    "account_id": account_id,
                    "email": email,
                    "display_name": display_name,
                    "user_id": str(actor_user_id),
                    "chapter_id": str(chapter_id),
                },
                connected_account_id=account_id,
                invoke=lambda key: stripe_service.create_customer(
                    account_id, email, display_name,
                    **_idempotency_kwargs(key),
                    **({"return_resource": True} if parent is not None else {}),
                ),
            )
            candidate_id = (
                getattr(candidate_result, "id", None)
                if parent is not None else str(candidate_result)
            )
            if parent is not None and (
                getattr(candidate_result, "object", None) != "customer"
                or not _valid_provider_id(candidate_id, "cus")
            ):
                if customer_effect is not None and customer_attempt is not None:
                    await mark_payment_attempt_uncertain(parent, customer_effect, customer_attempt)
                raise HTTPException(status_code=503, detail="payment_outcome_unconfirmed")
            user = await get_current_user(uid=uid, session=session)
            cycle, chapter = await _load_dues_context(session, user, cycle_id)
            if chapter.id != chapter_id or chapter.stripe_account_id != account_id:
                if parent is not None and customer_effect is not None and customer_attempt is not None:
                    await mark_payment_attempt_uncertain(parent, customer_effect, customer_attempt)
                raise conflict("chapter_not_onboarded")
            customer_id, _committed_binding = await _bind_customer(
                session, user.id, chapter_id, candidate_id,
                parent=parent, effect=customer_effect, attempt=customer_attempt,
            )
            user = await get_current_user(uid=uid, session=session)
            cycle, chapter = await _load_dues_context(session, user, cycle_id)
            if chapter.id != chapter_id or chapter.stripe_account_id != account_id:
                raise conflict("chapter_not_onboarded")
        else:
            # Revalidate current eligibility after the read-only account lookup.
            user = await get_current_user(uid=uid, session=session)
            cycle, chapter = await _load_dues_context(session, user, cycle_id)
            if chapter.id != chapter_id or chapter.stripe_account_id != account_id:
                raise conflict("chapter_not_onboarded")

        # Customer/account setup above precedes reservation ownership. The bounded
        # section below retains c366's existing money-row lock and deadline.
        bound_user_id = user.id
        try:
            async with asyncio.timeout(RESERVATION_PROVIDER_TIMEOUT_SECONDS):
                intent, amount_cents, payment_intent_status = await _prepare_reserved_intent(
                    session, cycle, user, chapter, body.rail, account_id, customer_id, parent
                )
        except DBAPIError:
            await session.rollback()
            raise
        except (stripe.StripeError, stripe_service.UnexpectedPaymentIntent, TimeoutError) as exc:
            await session.rollback()
            detail = stripe_service.intent_error_detail(exc)
            logger.warning(
                "c366: intent preparation stopped cycle=%s user=%s outcome=%s",
                cycle_id, bound_user_id, detail,
            )
            raise HTTPException(status_code=503, detail=detail) from None

        session_effect_key = str(parent.operation_id) if parent is not None else str(uuid.uuid4())
        session_secret_result, session_effect, session_attempt = await _run_payment_effect(
            parent,
            kind="customer_session_create",
            stable_identity=f"{session_effect_key}:{customer_id}:{account_id}",
            parameters={"account_id": account_id, "customer_id": customer_id},
            connected_account_id=account_id,
            invoke=lambda key: stripe_service.create_customer_session(
                account_id, customer_id,
                **_idempotency_kwargs(key),
                **({"return_resource": True} if parent is not None else {}),
            ),
        )
        if parent is not None and (
            getattr(session_secret_result, "object", None) != "customer_session"
            or getattr(session_secret_result, "customer", None) != customer_id
            or not isinstance(getattr(session_secret_result, "client_secret", None), str)
            or not getattr(session_secret_result, "client_secret", "")
        ):
            if session_effect is not None and session_attempt is not None:
                await mark_payment_attempt_uncertain(parent, session_effect, session_attempt)
            raise HTTPException(status_code=503, detail="payment_outcome_unconfirmed")
        await _finish_payment_effect(session, parent, session_effect, session_attempt)
        await session.commit()

        if parent is not None:
            user = await _final_payment_actor_check(
                session, user_id=actor_user_id, firebase_uid=uid
            )
            cycle, chapter = await _load_dues_context(session, user, cycle_id)
            if chapter.id != chapter_id or chapter.stripe_account_id != account_id:
                raise conflict("chapter_not_onboarded")
            current_customer = await session.scalar(
                select(models.ChapterStripeCustomer.stripe_customer_id).where(
                    models.ChapterStripeCustomer.user_id == actor_user_id,
                    models.ChapterStripeCustomer.chapter_id == chapter_id,
                )
            )
            if current_customer != customer_id:
                raise conflict("payment_outcome_unconfirmed")

        response = DuesIntentOut(
            payment_intent_client_secret=intent.client_secret,
            customer_session_client_secret=(
                session_secret_result.client_secret if parent is not None
                else str(session_secret_result)
            ),
            customer_id=customer_id,
            publishable_key=stripe_service.publishable_key(),
            stripe_account_id=account_id,
            amount_cents=amount_cents,
            application_fee_cents=stripe_service.platform_fee_cents(
                amount_cents, body.rail
            ),
            rail=body.rail,
            payment_intent_status=payment_intent_status,
        )
        await session.commit()
        return response


async def _lock_reservation(
    session: AsyncSession, statement: Select[tuple[models.DuesPaymentIntent]],
) -> models.DuesPaymentIntent | None:
    """Fresh row ownership or an immediate retryable conflict, never a lock queue."""
    try:
        return await session.scalar(
            statement.with_for_update(nowait=True).execution_options(populate_existing=True)
        )
    except DBAPIError as exc:
        await session.rollback()
        if getattr(exc.orig, "sqlstate", None) == "55P03":
            raise conflict("payment_already_in_progress") from None
        raise


async def _prepare_reserved_intent(
    session: AsyncSession, cycle: models.DuesCycle, user: models.User,
    chapter: models.Chapter, rail: str, account_id: str, customer_id: str,
    parent: ExternalOperationHandle | None = None,
) -> tuple[stripe.PaymentIntent, int, str]:
    """One serialized money-row transition; caller bounds ALL its provider awaits."""
    # RESERVE BEFORE CHARGING (c51). The already_paid check above can only see
    # SETTLED payments, so it cannot stop a member re-paying a cycle whose ACH
    # debit is still processing — and because the Stripe idempotency key is
    # per-rail, that retry would mint a genuinely different PaymentIntent.
    # uq_dues_intent_live makes the database, not Stripe, arbitrate: the second
    # attempt loses here, before any money moves.
    reservation = await _lock_reservation(
        session, select(models.DuesPaymentIntent).where(
            models.DuesPaymentIntent.dues_cycle_id == cycle.id,
            models.DuesPaymentIntent.user_id == user.id,
            models.DuesPaymentIntent.status.in_(("open", "failed", "succeeded")),
        )
    )
    if (
        reservation is not None
        and reservation.status in ("open", "failed")
        and reservation.stripe_payment_intent_id is not None
        and reservation.created_at < datetime.now(timezone.utc) - RESERVATION_TTL
    ):
        # Maybe abandoned (c234): never resolved by a webhook and long past any
        # reasonable window for the member to still be mid-checkout. AGE ALONE IS
        # NOT PROOF OF ABANDONMENT (adversarial-review catch, c234 amendment): an
        # ACH payment ordinarily sits in 'processing' for 1-3 business days -
        # longer than this TTL - and a lost webhook can leave a succeeded intent
        # looking 'open' here indefinitely. Expiring either and minting a fresh
        # intent charges the member TWICE at Stripe, with the second capture only
        # ever surfacing as the settlement reconciliation log's ERROR line.
        #
        # So Stripe's confirmed canceled status is the TEST. Do not infer it from
        # age or a guessed list of cancelable statuses (some processing intents
        # can still be canceled). An accepted cancellation, or a GET reporting
        # already canceled, releases this reservation. Anything else keeps it.
        expire = False
        if reservation.stripe_payment_intent_id is not None:
            cancellation_effect, cancellation_attempt = await _register_effect_attempt(
                parent,
                kind="payment_intent_cancel",
                stable_identity=reservation.stripe_payment_intent_id,
                parameters={
                    "account_id": account_id,
                    "payment_intent_id": reservation.stripe_payment_intent_id,
                },
                connected_account_id=account_id,
                reservation_id=reservation.id,
            )
            try:
                canceled = await stripe_service.cancel_payment_intent(
                    account_id,
                    reservation.stripe_payment_intent_id,
                    **_idempotency_kwargs(
                        cancellation_effect.idempotency_key
                        if cancellation_effect is not None else None
                    ),
                )
                expire = canceled.status == "canceled"
            except BaseException as cancel_error:
                expire = False
                if isinstance(cancel_error, asyncio.CancelledError):
                    if parent is not None and cancellation_effect is not None and cancellation_attempt is not None:
                        try:
                            await mark_payment_attempt_uncertain(
                                parent, cancellation_effect, cancellation_attempt
                            )
                        except BaseException:
                            pass
                    raise
                # A follow-up retrieve can resolve a lost cancel response. Keep
                # the attempt running until that read has completed so its exact
                # result can terminalize the same owner without guessing.
                try:
                    stale = await stripe_service.retrieve_payment_intent(
                        account_id, reservation.stripe_payment_intent_id
                    )
                    if stale.status == "canceled":
                        canceled = stale
                        expire = True
                    else:
                        canceled = stale
                except Exception:
                    canceled = None
                    logger.warning(
                        "c234: could not determine state of aged intent %s "
                        "(reservation %s); keeping the reservation - erring "
                        "toward a 409 over a possible double charge",
                        reservation.stripe_payment_intent_id,
                        reservation.id,
                    )
                if canceled is None:
                    if parent is not None and cancellation_effect is not None and cancellation_attempt is not None:
                        try:
                            await mark_payment_attempt_uncertain(
                                parent, cancellation_effect, cancellation_attempt
                            )
                        except BaseException:
                            pass
                elif parent is not None:
                    if expire:
                        await _finish_payment_effect(
                            session, parent, cancellation_effect, cancellation_attempt,
                            provider_id=reservation.stripe_payment_intent_id,
                        )
                    elif cancellation_effect is not None and cancellation_attempt is not None:
                        # A read showing the PI is still non-canceled does not
                        # prove the failed cancel request had no later effect.
                        await mark_payment_attempt_uncertain(
                            parent, cancellation_effect, cancellation_attempt
                        )
            else:
                if parent is not None:
                    if expire:
                        await _finish_payment_effect(
                            session, parent, cancellation_effect, cancellation_attempt,
                            provider_id=reservation.stripe_payment_intent_id,
                        )
                    elif cancellation_effect is not None and cancellation_attempt is not None:
                        # A successful API response with a non-canceled status is
                        # still an unresolved cancellation outcome. It must not
                        # reconcile an earlier uncertain attempt.
                        await mark_payment_attempt_uncertain(
                            parent, cancellation_effect, cancellation_attempt
                        )
        if expire:
            # 'expired' is not a value the status CHECK constraint allows
            # (migration 0010), so confirmed cancellation uses 'canceled'. A
            # declined attempt remains held as 'failed' until Stripe confirms
            # cancellation; its client secret can otherwise still be retried.
            reservation.status = "canceled"
            reservation.updated_at = datetime.now(timezone.utc)
            await session.commit()
            reservation = None

    if reservation is not None:
        if reservation.status == "succeeded":
            # Effectively unreachable in normal operation (board c172): a reservation
            # only reaches 'succeeded' via the webhook's stripe_webhook, which
            # commits in the SAME transaction as stripe_webhook's ledger insert
            # — so whenever this is true, the existence check above has already
            # raised (with the honest already_paid/refunded_contact_treasurer split)
            # before this line runs. Left as a defensive backstop rather than removed,
            # matching uq_ledger_dues_payment_once's own "independent of the
            # reservation" backstop reasoning (migration 0010).
            raise conflict("already_paid")
        if reservation.rail != rail:
            # THE double-charge case: an ACH debit is still processing (days) and
            # the member is now trying to pay the same cycle by card. The per-rail
            # Stripe idempotency key would happily mint a second real intent.
            raise conflict("payment_already_in_progress")
        # Same rail — an ordinary client retry. Handled below: reused if we already
        # have a stored intent id, created if we do not.
    else:
        reservation = models.DuesPaymentIntent(
            chapter_id=chapter.id,
            dues_cycle_id=cycle.id,
            user_id=user.id,
            rail=rail,
            # c349: snapshot what this intent is being created FOR, in the same
            # transaction as the reservation. The webhook binds settlement to these
            # two values (migration 0033) rather than to the cycle or the event.
            amount_cents=cycle.amount_cents,
            currency=stripe_service.CURRENCY,
        )
        session.add(reservation)
        try:
            await session.commit()
        except IntegrityError:
            # Concurrent first attempt won the race. uq_dues_intent_live, not the
            # check above, is what actually makes this safe under concurrency.
            await session.rollback()
            raise conflict("payment_already_in_progress") from None
        except DBAPIError as exc:
            # c230: cross_table_dues_guard_intents (migration 0028) — a treasurer
            # committed an ACTIVE payment plan for this (cycle, member) between
            # the active_plan read-guard above and this INSERT actually landing.
            # Same cross-table TOCTOU that read-guard exists to close, now
            # backstopped at the database; raise the identical 409 the read-guard
            # would have given had it run a moment later. Any OTHER DBAPIError
            # (not this trigger) re-raises untouched — this is a backstop for one
            # specific conflict, not a blanket swallow.
            await session.rollback()
            if not is_cross_table_dues_guard_conflict(exc):
                raise
            raise conflict("on_payment_plan") from None
        # The durable reservation commit released the INSERT's lock. Reacquire
        # and reload: another same-rail request could have resolved it in the gap.
        reservation = await _lock_reservation(
            session, select(models.DuesPaymentIntent).where(
                models.DuesPaymentIntent.id == reservation.id
            ),
        )
        if reservation is None or reservation.status not in ("open", "failed", "succeeded"):
            raise conflict("payment_already_in_progress")
        if reservation.status == "succeeded":
            raise conflict("already_paid")

    payment_intent_status = "awaiting_payment"
    if reservation.stripe_payment_intent_id is not None:
        # A same-rail retry against an intent we already created (board c193). Stripe
        # only retains an idempotency key for 24h, while ACH can sit in 'processing'
        # for DAYS with no payment_intent.processing webhook to move the reservation
        # out of 'open' in the meantime — a create() call here past that window would
        # mint a genuinely NEW real intent (a second bank debit) instead of resolving
        # to the original. Retrieve, never create, once an intent id is on file.
        intent = await stripe_service.retrieve_payment_intent(
            account_id, reservation.stripe_payment_intent_id
        )
        if intent.status in _INTENT_NOT_AWAITING_PAYMENT:
            # c234: Stripe moved past "waiting on the member" while the client was
            # away (settled, or an ACH debit mid-flight) and no webhook has resolved
            # this reservation yet. payment_intent_client_secret below is still the
            # real one Stripe issued — harmless to hand back — but it must not be
            # presented as a fresh checkout; the client is expected to read this
            # field instead of blindly reopening PaymentSheet.
            payment_intent_status = intent.status
    else:
        # No stored ID means an earlier response may have been lost. Retain the
        # original key even after a definite rejection of THIS request: it cannot
        # prove that an older unknown request never created an intent.
        if reservation.created_at <= datetime.now(timezone.utc) - CREATE_RETRY_WINDOW:
            logger.warning("c366: unresolved intent needs reconciliation reservation=%s", reservation.id)
            raise conflict("payment_reconciliation_required")
        intent_result, intent_effect, intent_attempt = await _run_payment_effect(
            parent,
            kind="payment_intent_create",
            stable_identity=str(reservation.id),
            parameters={
                "account_id": account_id,
                "customer_id": customer_id,
                "amount_cents": reservation.amount_cents,
                "currency": reservation.currency,
                "rail": rail,
                "cycle_id": str(cycle.id),
                "user_id": str(user.id),
                "chapter_id": str(chapter.id),
                "reservation_id": str(reservation.id),
            },
            connected_account_id=account_id,
            reservation_id=reservation.id,
            idempotency_key_override=(
                f"dues:{cycle.id}:{user.id}:{rail}:{reservation.id}"
            ),
            invoke=lambda _key: stripe_service.create_dues_payment_intent(
                account_id=account_id,
                customer_id=customer_id,
                amount_cents=reservation.amount_cents,
                currency=reservation.currency,
                rail=rail,
                cycle_id=cycle.id,
                user_id=user.id,
                chapter_id=chapter.id,
                reservation_id=reservation.id,
            ),
        )
        intent = intent_result
        if parent is not None and (
            getattr(intent, "object", None) != "payment_intent"
            or not _valid_provider_id(getattr(intent, "id", None), "pi")
            or not isinstance(getattr(intent, "client_secret", None), str)
            or not getattr(intent, "client_secret", "")
        ):
            raise HTTPException(status_code=503, detail="payment_outcome_unconfirmed")

        reservation_id = reservation.id
        try:
            reservation.stripe_payment_intent_id = intent.id
            await _finish_payment_effect(
                session, parent, intent_effect, intent_attempt, provider_id=intent.id
            )
            await session.commit()
        except IntegrityError as exc:
            # Belt, not the primary fix (c231): uq_dues_intent_stripe_id spans every
            # status, so if the id Stripe just handed back is already claimed by a
            # DIFFERENT reservation row, this commit loses the race instead of
            # silently stealing it. Expected to be unreachable once c231's per-
            # reservation nonce above is fully rolled out; kept as a belt for the
            # deploy window where an old-format (cycle, member, rail) key can still
            # be live in Stripe's 24h idempotency cache and get replayed by an
            # instance still running the old code. This reservation was never live
            # at Stripe under that id — still the create branch, so canceling it is
            # safe per the invariant above — and the client's next retry reserves a
            # fresh row with a fresh idempotency key rather than hitting a 500.
            await session.rollback()
            # Only this named unique violation proves the returned intent already
            # belongs to a different reservation. A check/trigger failure (or a
            # different unique violation) after provider success leaves an unknown
            # outcome: keep the durable row/key open and propagate the DB failure.
            # SQLAlchemy's asyncpg adapter keeps SQLSTATE on orig and the original
            # driver's structured constraint metadata on its chained cause.
            driver_error = getattr(exc.orig, "__cause__", None)
            if (
                getattr(exc.orig, "sqlstate", None) != "23505"
                or getattr(driver_error, "constraint_name", None) != "uq_dues_intent_stripe_id"
            ):
                raise
            stale = await _lock_reservation(
                session, select(models.DuesPaymentIntent).where(models.DuesPaymentIntent.id == reservation_id)
            )
            if stale is not None and stale.status == "open" and stale.stripe_payment_intent_id is None:
                stale.status = "canceled"
                stale.updated_at = datetime.now(timezone.utc)
                await session.commit()
            raise conflict("payment_intent_conflict") from None

        # c227 (skeptic catch): emitted HERE, inside the create branch after the
        # intent id committed, never after the branches converge - the retrieve
        # path re-serves an existing intent on every same-rail retry/poll (for
        # ACH, across days), and an emit there overcounts "intents created" by
        # however many times the member reopens the screen.
        emit(
            "payment_intent_created",
            chapter_id=chapter.id,
            cycle_id=cycle.id,
            user_id=user.id,
            rail=rail,
        )

    # The retrieve path still owns its row; release it before CustomerSession I/O.
    # Amount is the immutable c349 reservation snapshot, including on same-key retry.
    amount_cents = reservation.amount_cents
    await session.commit()
    if getattr(intent, "status", None) in _INTENT_NOT_AWAITING_PAYMENT:
        payment_intent_status = intent.status
    return intent, amount_cents, payment_intent_status


def _emit_stripe_webhook_event(event_type: str, bound: BoundSettlement) -> None:
    """Emit only committed, first-time transitions, using database-owned identity."""
    if event_type not in ("payment_intent.succeeded", "payment_intent.payment_failed"):
        return
    reservation = bound.reservation
    emit(
        "payment_succeeded" if event_type == "payment_intent.succeeded" else "payment_failed",
        event_type=event_type,
        rail=reservation.rail,
        cycle_id=str(reservation.dues_cycle_id),
        user_id=str(reservation.user_id),
    )


@router.post("/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Authenticate then bind settlement to its stored reservation (c349).

    The event receipt, quarantine or ledger change commit together. Only expected
    uniqueness conflicts are acknowledged; failed database writes remain retryable.
    No raw payload, customer details, or secret enters logs or quarantine.
    """
    if not stripe_signature:
        raise HTTPException(status_code=400, detail="missing_stripe_signature")
    event = stripe_service.verify_webhook_event(await request.body(), stripe_signature)

    receipt = await session.scalar(
        pg_insert(models.ProcessedStripeEvent)
        .values(event_id=event["id"], event_type=event["type"])
        .on_conflict_do_nothing(index_elements=["event_id"])
        .returning(models.ProcessedStripeEvent.event_id)
    )
    if receipt is None:
        await session.rollback()
        return {"received": True}

    statuses = {
        "payment_intent.succeeded": "succeeded",
        "payment_intent.payment_failed": "failed",
        "payment_intent.canceled": "canceled",
    }
    event_type = event["type"]
    bound = await bind_settlement(session, event) if event_type in statuses else None
    changed = False
    reconciliation = None
    if bound is not None:
        reservation, cycle = bound.reservation, bound.cycle
        if event_type == "payment_intent.succeeded":
            # Both unique payment indexes arbitrate concurrent settlement. All
            # non-unique failures propagate and roll back the event receipt too.
            recorded = await session.scalar(
                pg_insert(models.LedgerEntry).values(
                    chapter_id=reservation.chapter_id, entry_type="dues_payment",
                    amount_cents=reservation.amount_cents, category="dues",
                    description=cycle.name, related_user_id=reservation.user_id,
                    dues_cycle_id=reservation.dues_cycle_id,
                    stripe_payment_intent_id=reservation.stripe_payment_intent_id,
                    created_by=reservation.user_id,
                ).on_conflict_do_nothing().returning(models.LedgerEntry.id)
            )
            if recorded is not None:
                reservation.status = "succeeded"
                reservation.updated_at = datetime.now(timezone.utc)
                changed = True
            else:
                recorded_intent = await session.scalar(
                    select(models.LedgerEntry.stripe_payment_intent_id).where(
                        models.LedgerEntry.dues_cycle_id == reservation.dues_cycle_id,
                        models.LedgerEntry.related_user_id == reservation.user_id,
                        models.LedgerEntry.entry_type == "dues_payment",
                    )
                )
                if recorded_intent != reservation.stripe_payment_intent_id:
                    reconciliation = (
                        str(reservation.dues_cycle_id), str(reservation.user_id),
                        reservation.stripe_payment_intent_id, recorded_intent,
                    )
        elif event_type == "payment_intent.payment_failed":
            # A declined attempt remains retryable at Stripe (c387). `failed`
            # holds the same reservation/index slot as `open`; late failures
            # must never revive an intent already canceled or succeeded.
            if reservation.status in ("open", "failed"):
                changed = reservation.status != "failed"
                reservation.status = "failed"
                reservation.updated_at = datetime.now(timezone.utc)
        elif reservation.status != "succeeded":
            # Only confirmed cancellation releases a held attempt. Never demote
            # a successfully paid cycle on an out-of-order cancellation.
            changed = reservation.status != statuses[event_type]
            reservation.status = statuses[event_type]
            reservation.updated_at = datetime.now(timezone.utc)

    await session.commit()
    if reconciliation is not None:
        logger.error(
            "dues payment reconciliation: cycle=%s user=%s intent=%s captured but "
            "NOT recorded on the ledger; recorded intent=%s. Verify both against "
            "Stripe and reconcile manually.", *reconciliation,
        )
    if changed and bound is not None:
        _emit_stripe_webhook_event(event_type, bound)
    return {"received": True}
