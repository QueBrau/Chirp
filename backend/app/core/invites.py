"""Invite-code policy constants (board card c105).

These live in one module rather than inline in the router because the schema
validates against them and the router enforces them, and the two drifting apart is
how a cap quietly stops being a cap. Deliberately NOT in config.py: they are a
product rule, not a deployment knob, and a rule that can be raised by an env var on
a bad afternoon is not a rule.
"""

from datetime import datetime, timedelta, timezone

# How long a minted code lives when the caller does not say. Seven days covers a
# recruitment week, which is the actual unit chapters invite in.
INVITE_DEFAULT_TTL_DAYS = 7

# The longest life a caller may ASK for. A president wanting a code for the whole
# semester is a real request, and the answer is to mint a new one in thirty days.
INVITE_MAX_TTL_DAYS = 30

# Redemptions per code when unspecified. A chapter invite is a pledge-class artifact
# — one code posted to a room of people — so single-use would break the workflow
# rather than secure it. The bound is what matters, not the number.
INVITE_DEFAULT_MAX_USES = 25

# Hard ceiling on max_uses. Enforced again as a CHECK constraint in migration 0016,
# because the schema is only as strong as the code path that happens to use it.
INVITE_MAX_USES_CAP = 200


def default_invite_expiry(now: datetime | None = None) -> datetime:
    """The expiry a code gets when the caller does not supply one."""
    return (now or datetime.now(timezone.utc)) + timedelta(days=INVITE_DEFAULT_TTL_DAYS)


def clamp_invite_expiry(
    requested: datetime | None, now: datetime | None = None
) -> datetime:
    """Resolve a requested expiry into one the system will actually store.

    None becomes the default window — this is the line that makes "no expiry"
    unreachable. A request beyond the cap is CLAMPED to the cap rather than
    rejected: the caller asked for a long-lived code and gets the longest one
    policy allows, which fails toward the safe value instead of toward a 422 that
    tempts the next person to widen the cap.

    An expiry in the PAST is not clamped here — the router rejects it outright.
    Minting a code that is dead on arrival is safe but useless, and silently
    handing one back reads as a bug to whoever tries to use it.
    """
    now = now or datetime.now(timezone.utc)
    ceiling = now + timedelta(days=INVITE_MAX_TTL_DAYS)
    if requested is None:
        return default_invite_expiry(now)
    if requested.tzinfo is None:
        requested = requested.replace(tzinfo=timezone.utc)
    return min(requested, ceiling)
