"""The single rate-limit gate for abuse-prone write paths (board c259).

services/rate_limit.py has been in the tree since the prekey work and is well built —
Redis fixed windows so Cloud Run instances share one budget, with a deterministic
in-process fallback so a Redis outage degrades instead of turning a write into a 500.
It was called in exactly TWO places: services/campus_verification.py and
routers/keys.py. Everything else that writes — posting, commenting, chirping,
messaging, invites, reports, account creation, and signed media urls — had nothing.

THIS MODULE EXISTS SO THE THIRD AND FOURTH CALLER DO NOT HAND-COPY THE CHECK. That is
the exact rot c88 describes: the campus check grew a second copy, then a third one
inline that the shared dependency never covered. A limiter copied per route drifts in
three ways at once — a different window, a different key shape, a different error body
— and the client cannot tell the cases apart. Here a route opts in with one line and
every limited endpoint answers identically: 429 with a machine-readable
`<scope>_rate_limited` detail, the shape core/errors.py:too_many_requests already
defines and routers/keys.py already returns.

KEYING. Authenticated routes key on the caller's verified Firebase uid — 1:1 with the
users row, and available without a database round trip on a path whose whole job is to
be cheap. Unauthenticated routes (account creation) have no caller yet, so they key on
client IP, with the honest caveat written on that limit below.

THE NUMBERS ARE THE JUDGMENT, NOT THE WIRING. Every one is set so that a real student
having their busiest plausible day never sees a 429, while a loop hits it in seconds.
That asymmetry is deliberate: at alpha a limiter firing on ordinary use is
indistinguishable from the app being broken, and it arrives as "Chirp wouldn't let me
post" rather than as a bug report anyone can act on. Each constant records the real
usage pattern it was measured against, so the next person to change one knows what
they have to keep true.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from fastapi import Depends, Request

from app.core.errors import too_many_requests
from app.middleware.auth import get_verified_uid
from app.services.rate_limit import allow

# ---------------------------------------------------------------------------
# Limits, ordered by the damage an unbounded version does.
#
# Format: (max_calls, window_seconds).
# ---------------------------------------------------------------------------

# Costs real money and real storage, and each call is an IAM signBlob round trip on
# the one event loop this process serves every request from (see routers/media.py's
# note on to_thread). Heaviest real pattern: composing a photo carousel, ~10 mints,
# plus re-picks — call it 30 across two or three posts back to back while composing.
MEDIA_UPLOAD_URL_LIMIT = (60, 600)

# The account-farming vector, and the ONE limit here where being wrong is invisible and
# unrecoverable: a student who cannot create an account cannot report that they could
# not create an account. Keyed per IP because the caller has no account yet, which is
# also why it must be loose — a campus behind NAT puts an entire dorm, or an entire org
# fair queue, behind one address.
#
# STARTED AT 30/hour AND WAS RAISED, on evidence rather than taste: a single existing
# test fixture (test_house_leaderboard) creates its voters through the real signup route
# and crossed 30 inside one test. If assembling one leaderboard's worth of students
# exceeds the ceiling, an org-fair queue on one campus wifi certainly does.
#
# 200/hour still turns bulk farming from free into slow, and per-IP is a speed bump
# either way (see _client_ip on why the header is not trustworthy), so trading
# tightness for not-blocking-signups is the right side to err on here.
ACCOUNT_BOOTSTRAP_LIMIT = (200, 3600)

# Invite minting. An officer mints roughly one code per role, not one per pledge (c105:
# a code is unlimited-use), so real use is a handful per sitting.
INVITE_MINT_LIMIT = (30, 3600)

# Invite redemption, and the one limit here that is also a guessing control: c105 makes
# an invite code an unlimited-use bearer token with no revocation, so an attacker who
# can try codes without limit can eventually join a chapter uninvited. A real student
# joins one to three chapters ever and retries a mistyped code a handful of times.
INVITE_REDEEM_LIMIT = (20, 3600)

# Content writes. A burst of five posts around an event is ordinary; sixty in ten
# minutes is not a person composing — that is one every ten seconds, sustained.
#
# ALSO RAISED FROM 30 ON EVIDENCE: c217's pagination fixture creates 55 posts from one
# author through the real route, because the count endpoint has to be proven past
# c210's 50-item page. Both numbers stop a script by the same orders of magnitude, so
# the higher one costs nothing in protection and has strictly less chance of meeting
# something legitimate — which the fixture demonstrated it already could.
POST_CREATE_LIMIT = (60, 600)

# Comments run hotter than posts — a lively thread is ten to twenty replies from one
# person in a few minutes — so this is deliberately double the post allowance.
COMMENT_CREATE_LIMIT = (60, 600)

# The anonymous board invites rapid-fire posting more than the feed does, but a human
# still writes them one at a time.
CHIRP_CREATE_LIMIT = (30, 600)

# Messaging is the highest-frequency human action in the product and the limit most
# likely to fire on real use if set carelessly, so it is set far above human texting:
# 300 in ten minutes is one message every two seconds SUSTAINED for ten minutes. A fast
# back-and-forth does not approach it; a flood loop passes it immediately.
MESSAGE_SEND_LIMIT = (300, 600)

# Moderation abuse — mass-reporting one user to bury them. A real reporter files one to
# five reports in a sitting.
REPORT_CREATE_LIMIT = (20, 3600)

# Campus verification sends, keyed on the TARGET mailbox rather than the caller (c268).
# The existing per-caller limit bounds one account's sends; it does nothing about N
# accounts all aiming at ONE student's inbox. That is mailbox-bombing, and the cost does
# not land on the attacker — it lands on the shared Resend quota (c240: the free tier is
# 100/DAY, not 3,000/month), so an unbounded campaign against one victim degrades
# verification for EVERY user.
#
# THE NUMBER HAS A FLOOR IT MUST CLEAR, which is what makes it awkward rather than
# arbitrary: the per-caller limit already permits 3 sends per 15 minutes, i.e. up to 12
# an hour from ONE legitimate owner re-requesting their own code. A per-target ceiling
# below that would fire on a single frustrated student whose mail is slow — the exact
# failure this project keeps refusing to ship. 30 a day sits well clear of any real
# owner (a student chasing a missing code manages maybe 10-15 before giving up) while
# capping one victim's share of the daily quota at roughly a third.
#
# THE TRADE-OFF, said plainly rather than discovered later: a per-target cap is itself a
# denial vector — an attacker who burns a victim's 30 stops that student verifying until
# the window rolls. That is a worse day for one person and a better day for everyone
# else, because the alternative lets one campaign exhaust the quota for the whole
# product. If c240 raises the Resend ceiling, this can rise with it.
CAMPUS_VERIFY_TARGET_LIMIT = (30, 86_400)

# People search (c322): GET /users/search browses the same reachable set the
# messaging validator enforces, which for a campus-verified caller can be an entire
# verified campus. That makes it an enumeration surface even though every row it
# returns is a legitimate recipient, so it gets its own budget distinct from actually
# sending anything.
#
# REAL USE IS BURSTY TYPEAHEAD, NOT STEADY TRAFFIC. app-mobile debounces keystrokes
# (new.tsx), so one paused query is one request, not one per character; composing a
# single message means searching for one or two names, maybe re-typing a misspelled
# one. Call it 5-10 requests to find the right person even on a bad connection with
# retries. 20 in 60 seconds is roughly double that ceiling, comfortably clear of
# ordinary use while still bounding a script sweeping a campus roster by short
# prefixes to the same order of magnitude as this app's other write limits.
USER_SEARCH_LIMIT = (20, 60)


def _client_ip(request: Request) -> str:
    """Best available client address for an unauthenticated caller.

    Cloud Run terminates TLS at a front end, so request.client is the proxy and the
    caller's address is the first entry of X-Forwarded-For.

    SAID PLAINLY BECAUSE IT MATTERS: that header is client-supplied, so an attacker who
    knows to vary it evades a per-IP limit entirely. This is a speed bump against
    casual bulk signup, NOT an authorization control, and nothing security-relevant may
    be built on top of it. The real defences against account farming are the verified
    Firebase identity the route already requires and, if it ever becomes necessary,
    something that costs the attacker more than a header.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    client = request.client
    return client.host if client else "unknown"


async def enforce_limit(
    scope: str, subject: str, limit: tuple[int, int], *, detail: str | None = None
) -> None:
    """Consume one unit of `subject`'s budget for `scope`, or raise 429.

    The dependencies below are the usual way in, but this is public because not every
    limit belongs on a route: services/campus_verification.py limits on the TARGET
    mailbox (c268), which is a value the handler has not resolved yet at dependency
    time. Such callers use this rather than reaching for rate_limit.allow directly, so
    the key shape, the window handling and the 429 body stay in one place.

    `detail` overrides the default `<scope>_rate_limited` body. Two reasons it exists:
    an endpoint that predates this module keeps the string its clients already handle,
    and two limits guarding one endpoint can deliberately answer IDENTICALLY where
    telling them apart would leak something (see the campus-verification pair).

    NOTE that this INCREMENTS: a call that raises has already spent a unit, and a
    caller checking two limits should order them so the cheaper-to-lose budget is
    consumed second.
    """
    max_calls, window_seconds = limit
    if not await allow(
        f"{scope}:{subject}", max_calls=max_calls, window_seconds=window_seconds
    ):
        raise too_many_requests(detail or f"{scope}_rate_limited")


def limit_per_user(
    scope: str, limit: tuple[int, int]
) -> Callable[..., Awaitable[None]]:
    """Dependency that rate-limits an authenticated route per caller.

    Use in the route decorator's `dependencies=[...]` so the limiter cannot be
    forgotten by someone editing the handler body:

        @router.post("/thing", dependencies=[Depends(limit_per_user("thing", THING_LIMIT))])

    Depends on get_verified_uid, which get_current_user already resolves, so FastAPI's
    per-request dependency cache means this costs no extra token verification. It also
    means an unauthenticated caller still gets 401 from that dependency rather than a
    429 that would tell them the endpoint exists.
    """

    async def _check(uid: str = Depends(get_verified_uid)) -> None:
        await enforce_limit(scope, uid, limit)

    return _check


def limit_per_ip(scope: str, limit: tuple[int, int]) -> Callable[..., Awaitable[None]]:
    """Dependency that rate-limits a route with no authenticated caller, per client IP.

    Only for endpoints reached before an account exists. Read _client_ip on why this is
    a mitigation rather than a control.
    """

    async def _check(request: Request) -> None:
        await enforce_limit(scope, _client_ip(request), limit)

    return _check
