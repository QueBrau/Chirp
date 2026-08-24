""".edu verification: send a code to a school address, redeem it, record the proof (c86).

This is the ONLY writer of users.campus_verified_at, which per app.core.campus_access is
the only thing that opens the campus feed and the Yak board.

WHY THE CAMPUS IS NEVER TAKEN FROM THE REQUEST. The caller supplies an address and
nothing else; the campus is resolved from that address's domain, server-side, at send
time. A body carrying both an address and a campus_id would let someone pair a domain
they control with the campus they want to read — which is c85's vulnerability rebuilt
one layer up, and c85 is the reason this flow exists at all.

WHY REDEMPTION MAY OVERWRITE campus_id. c96's invite path only ever fills a NULL, on
purpose, so that this flow can win. A proved .edu is a strictly stronger claim than an
invite code someone forwarded, so redeeming here SETS campus_id outright. The ordering
must never be reversed.

THE LIVE-PATH PROTECTIONS, which matter more than the stored hash: a six-digit code has
only a million values, so the defence is not cryptographic. It is that a code expires in
minutes, dies after a handful of wrong guesses, can be used once, and cannot be requested
in a loop. All four are enforced here.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.core.errors import forbidden, not_found, too_many_requests
from app.services import email_service, rate_limit

CODE_DIGITS = 6
CODE_TTL = timedelta(minutes=15)
MAX_ATTEMPTS = 5

# A student mistyping their address should be able to retry; a script should not be able
# to mail an inbox repeatedly. Keyed per user, not per address, so switching the address
# does not reset the budget.
SEND_MAX_PER_WINDOW = 3
SEND_WINDOW_SECONDS = 15 * 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_code(verification_id, code: str) -> str:
    """Salted digest. The row id is the salt, so one precomputed table cannot cover
    every row — see the CampusVerification docstring on what this does and does not buy.
    """
    return hashlib.sha256(f"{verification_id}:{code}".encode()).hexdigest()


def _generate_code() -> str:
    """A cryptographically random N-digit code, zero-padded.

    secrets, not random — the latter is seeded predictably and is not for anything
    that gates access.
    """
    return f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}"


def normalize_email(raw: str) -> tuple[str, str]:
    """Return (normalized_address, domain), or 400 if it is not a usable address."""
    address = raw.strip().lower()
    if address.count("@") != 1:
        raise HTTPException(status_code=400, detail="invalid_email")
    local, _, domain = address.partition("@")
    if not local or not domain or "." not in domain:
        raise HTTPException(status_code=400, detail="invalid_email")
    return address, domain


async def resolve_campus(session: AsyncSession, domain: str) -> models.Campus:
    """The campus that owns `domain`, or 400.

    EXACT match against campuses.email_domains — "uncg.edu" does not admit
    "students.uncg.edu". A campus needing the subdomain lists it explicitly, so the rule
    widens only by a visible data change. Fails CLOSED when no campus configures domains.
    """
    result = await session.execute(
        select(models.Campus).where(models.Campus.email_domains.any(domain))
    )
    campus = result.scalars().first()
    if campus is None:
        raise HTTPException(status_code=400, detail="unrecognized_edu_domain")
    return campus


async def start_verification(
    session: AsyncSession, user: models.User, raw_email: str
) -> models.CampusVerification:
    """Mail a fresh code to `raw_email` and record the pending verification."""
    address, domain = normalize_email(raw_email)
    campus = await resolve_campus(session, domain)

    if not await rate_limit.allow(
        f"campus_verify_send:{user.id}",
        max_calls=SEND_MAX_PER_WINDOW,
        window_seconds=SEND_WINDOW_SECONDS,
    ):
        raise too_many_requests("verification_rate_limited")

    # Any earlier pending code for this user is retired first, so "resend" cannot leave
    # two live codes and double an attacker's guessing budget.
    await _consume_pending(session, user)

    now = _now()
    verification = models.CampusVerification(
        user_id=user.id,
        campus_id=campus.id,
        edu_email=address,
        code_hash="",  # replaced below, once the row has an id to salt with
        sent_at=now,
        expires_at=now + CODE_TTL,
    )
    session.add(verification)
    await session.flush()

    code = _generate_code()
    verification.code_hash = _hash_code(verification.id, code)
    await session.commit()

    # Sent AFTER the commit: a delivered code whose row failed to persist is
    # unredeemable, and the student has no way to know that is why.
    await email_service.send_email(
        to=address,
        subject=f"Your {campus.name} verification code",
        html=(
            f"<p>Your Chirp verification code is <strong>{code}</strong>.</p>"
            f"<p>It expires in {int(CODE_TTL.total_seconds() // 60)} minutes. "
            f"If you did not ask for this, you can ignore it.</p>"
        ),
        text=f"Your Chirp verification code is {code}. "
        f"It expires in {int(CODE_TTL.total_seconds() // 60)} minutes.",
    )
    return verification


async def _consume_pending(session: AsyncSession, user: models.User) -> None:
    """Mark every live pending code for this user as spent."""
    result = await session.execute(
        select(models.CampusVerification).where(
            models.CampusVerification.user_id == user.id,
            models.CampusVerification.consumed_at.is_(None),
        )
    )
    now = _now()
    for row in result.scalars().all():
        row.consumed_at = now
        session.add(row)


async def redeem(
    session: AsyncSession, user: models.User, code: str
) -> models.CampusVerification:
    """Check `code` against the caller's newest pending verification and record the proof."""
    result = await session.execute(
        select(models.CampusVerification)
        .where(
            models.CampusVerification.user_id == user.id,
            models.CampusVerification.consumed_at.is_(None),
        )
        .order_by(models.CampusVerification.sent_at.desc())
    )
    verification = result.scalars().first()
    if verification is None:
        raise not_found("no_pending_verification")

    now = _now()
    if verification.expires_at <= now:
        raise forbidden("verification_expired")
    if verification.attempts >= MAX_ATTEMPTS:
        raise too_many_requests("verification_attempts_exhausted")

    if not secrets.compare_digest(
        verification.code_hash, _hash_code(verification.id, code.strip())
    ):
        # Board card c138 (security's 7-day-pass finding): `verification.attempts += 1`
        # here used to be a Python-side read-modify-write with no row lock. Proven against
        # a real Postgres — 25 concurrent wrong guesses against a cap of 5, counter reached
        # 3 — because concurrent requests all read the SAME stale `attempts` value, each
        # computes its own `old + 1`, and whichever commits last wins; the others' increments
        # are simply overwritten, not lost to a crash. "Dies after a handful of wrong
        # guesses" (see the module docstring) did not hold under concurrency.
        #
        # Same shape as c51's dues reservation, c105's invite seat claim, c91's report
        # resolution and c114's spend-approval decision: the guard IS the write. The
        # database picks whether this guess counted, not a Python-side precondition
        # checked before it. `attempts = attempts + 1` is computed server-side from the
        # row Postgres is currently looking at (row-locked for the statement's duration),
        # not from a value read moments earlier in this session — so N truly concurrent
        # wrong guesses against a fresh row converge on exactly `MAX_ATTEMPTS` accepted
        # increments, not fewer.
        result = await session.execute(
            update(models.CampusVerification)
            .where(
                models.CampusVerification.id == verification.id,
                models.CampusVerification.attempts < MAX_ATTEMPTS,
            )
            .values(attempts=models.CampusVerification.attempts + 1)
            .returning(models.CampusVerification.attempts)
            .execution_options(synchronize_session=False)
        )
        if result.scalar_one_or_none() is None:
            # A concurrent guess already pushed attempts to the cap between our SELECT
            # above and this UPDATE — the guess was never counted, and it must not be:
            # counting a guess the guard refused would let the cap creep past MAX_ATTEMPTS
            # under exactly the concurrency this fix exists to close.
            await session.commit()
            raise too_many_requests("verification_attempts_exhausted")
        await session.commit()
        raise forbidden("verification_code_invalid")

    verification.consumed_at = now
    user.campus_verified_at = now
    # Overwrites outright rather than filling a NULL — see the module docstring. A proved
    # .edu supersedes a campus inherited from an invite code.
    user.campus_id = verification.campus_id
    session.add(verification)
    session.add(user)
    await session.commit()
    return verification
