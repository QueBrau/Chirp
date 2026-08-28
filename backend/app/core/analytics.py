"""Structured behavioral analytics events to stdout (board c227), for a Cloud Logging
-> BigQuery sink the manager sets up separately, outside this repo.

THE HARD PRIVACY RULE, enforced in code AND in test, per Jose's approval of c227:
nothing in this pipeline may carry or link chirp authorship. The API deliberately
withholds who posted a chirp (SPEC §8.3) - analytics must be structurally unable to
undo that. Two enforcement points:

  1. No call anywhere in app/routers/chirps.py - creation, voting, or reporting -
     may reach this module at all. tests/test_analytics.py source-scans that file's
     text for the literal string "analytics" and fails the build if it appears,
     which also catches an `import ... as` alias or a re-exported wrapper - anything
     that would let a chirp route reach `emit` without the string "analytics"
     appearing in its own source is a bug in the scan, not a loophole to use.
  2. No `emit()` call anywhere in this codebase may pair a chirp id with a user id -
     there is no chirp_id parameter in use anywhere outside chirps.py to make that
     pairing possible in the first place. A poll vote is the adjacent case (secret
     ballot, not chirp anonymity, but the same shape of guarantee): its emit call
     carries poll_id and a scope id, never the voter's user_id.

Everything else in the pipeline is normal product analytics and may carry a user_id
freely (signup, posts, messages, event RSVPs, dues payments - none of those are
anonymous features).
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger("app.analytics")


def emit(event: str, **props: object) -> None:
    """Log one structured analytics event as a single JSON line.

    Written on the "app.analytics" logger, a child of "app" by Python's dotted-name
    logging convention. It carries no handler of its own and needs none: verified by
    reading app/core/logging_config.py (board c176), which configures exactly one
    logger - "app" - with an INFO-level StreamHandler to stdout, and deliberately
    leaves `propagate` at its default (True) rather than setting it False. A record
    built here has no handler on "app.analytics" itself, so Logger.callHandlers walks
    up the ancestor chain to "app" and invokes that handler - the same mechanism that
    already carries every other `app.*` module logger (email_service, the WS gateway,
    ...) to stdout today. No new wiring is required for this module to reach Cloud
    Run's log collection; it rides the c176 fix that already covers all of `app.*`.

    NEVER RAISES. A telemetry pipeline is best-effort by design (board c227): a prop
    that turns out not to be JSON-serializable, or any other internal failure, is
    caught here and reported as a `logger.warning` instead of propagating - the
    request that triggered the event must never fail because analytics did. This is
    also why every call site in the routers is a plain one-liner with no try/except
    of its own; the safety is centralized here, once, rather than repeated at each of
    the fifteen-odd call sites.

    PROPS ARE ALWAYS COARSE. Ids (user_id, chapter_id, campus_id, event_id, poll_id,
    cycle_id, ...), enums, booleans, and counts (whole cents is the finest money
    granularity) only - never free text, never a message or post body, never an email
    address, never a token. See the module docstring for the one further restriction
    that applies specifically to chirps.

    `default=str` on the json.dumps call is deliberate, not decorative: every id
    passed in from a router is a live `uuid.UUID` object (or occasionally an enum),
    neither of which `json.dumps` can serialize on its own - without this, EVERY
    call site would 500-safe into a silent `logger.warning` and no event would ever
    actually reach stdout. `default=str` turns each into its ordinary text form the
    same way `str(some_uuid)` already does everywhere else in this codebase.
    """
    try:
        logger.info(json.dumps({"analytics": True, "event": event, **props}, default=str))
    except Exception:
        logger.warning("analytics emit failed event=%s", event, exc_info=True)
