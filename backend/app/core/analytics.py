"""Allowlisted, content-free product events for the Cloud Logging analytics sink.

Anonymous Chirps are excluded entirely; poll events cannot carry voter identity.
The router source guard is a regression tripwire, not a proof of the whole call
graph. Runtime schemas below also reject unknown events/properties and free text.
Events describe accepted operations/transitions, not every app visit or provider
attempt. Logging is best-effort; the ledger remains the source of payment truth.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from uuid import UUID, uuid4

logger = logging.getLogger("app.analytics")
diagnostics = logging.getLogger("app.analytics_diagnostics")

# Every accepted property is either a UUID, a bounded count or an explicit enum.
# A nullable scope is retained as null, never replaced with an invented campus.
_ID = "uuid"
_OPTIONAL_ID = "nullable_uuid"
_COUNT = "count"
_ACCOUNT = ("greek", "non_greek", "alumni")
_RAIL = ("card", "ach")
_SCHEMAS: dict[str, dict[str, str | tuple[str, ...]]] = {
    "user_signed_up": {"user_id": _ID, "account_type": _ACCOUNT},
    "account_type_changed": {
        "user_id": _ID, "previous_account_type": _ACCOUNT, "account_type": _ACCOUNT,
    },
    "campus_verification_started": {"user_id": _ID, "campus_id": _OPTIONAL_ID},
    "campus_verification_redeemed": {"user_id": _ID, "campus_id": _ID},
    "post_created": {
        "user_id": _ID, "chapter_id": _OPTIONAL_ID, "campus_id": _OPTIONAL_ID,
        "audience": ("org", "org_actives", "campus"), "post_type": ("text", "photo", "video"),
    },
    "message_sent": {
        "user_id": _ID, "conversation_id": _ID,
        "message_type": ("signal", "sender_key_distribution"), "recipient_count": _COUNT,
    },
    "event_created": {
        "user_id": _ID, "chapter_id": _ID, "event_id": _ID,
        "visibility": ("chapter", "campus", "verified", "public"),
    },
    "event_rsvp": {
        "user_id": _ID, "chapter_id": _ID, "event_id": _ID, "status": ("going", "maybe", "cant"),
    },
    "poll_voted": {"poll_id": _ID, "chapter_id": _ID},
    "payment_intent_created": {"chapter_id": _ID, "cycle_id": _ID, "user_id": _ID, "rail": _RAIL},
    "payment_succeeded": {
        "event_type": ("payment_intent.succeeded",), "cycle_id": _ID, "user_id": _ID, "rail": _RAIL,
    },
    "payment_failed": {
        "event_type": ("payment_intent.payment_failed",), "cycle_id": _ID, "user_id": _ID, "rail": _RAIL,
    },
    # An operator-generated delivery check has no user, domain object or content.
    # It is excluded from all product denominators in ANALYTICS-VERIFICATION.md.
    "pipeline_probe": {"probe_id": _ID},
}


def _property(value: object, rule: str | tuple[str, ...]) -> object:
    if rule == _OPTIONAL_ID and value is None:
        return None
    if rule in (_ID, _OPTIONAL_ID):
        if isinstance(value, UUID):
            # asyncpg returns its own UUID subclass. Rebuild from the numeric
            # value instead of invoking a subclass's arbitrary __str__ method.
            return str(UUID(int=value.int))
        if type(value) is str:
            return str(UUID(value))
        raise ValueError("invalid identifier")
    if rule == _COUNT:
        if type(value) is not int or not 0 <= value <= 2**31 - 1:
            raise ValueError("invalid count")
        return value
    if type(value) is not str or value not in rule:
        raise ValueError("invalid enum")
    return value


def emit(event: str, **props: object) -> None:
    """Emit one bounded JSON line; never propagate a telemetry failure.

    Unknown fields reject the whole event. UUID conversion is explicit: arbitrary
    __str__ methods, nested metadata, amounts, tokens and bodies cannot be logged.
    emission_id identifies this emission for downstream transport deduplication;
    it does not make the domain write/log atomic or recover a missing emission.
    """
    try:
        if type(event) is not str:
            raise ValueError("invalid event")
        schema = _SCHEMAS[event]
        if set(props) != set(schema):
            raise ValueError("invalid fields")
        safe = {key: _property(props[key], rule) for key, rule in schema.items()}
        payload = json.dumps({
            "analytics": True, "schema_version": 1, "event": event,
            "emission_id": str(uuid4()), "emitted_at": datetime.now(timezone.utc).isoformat(),
            "severity": "INFO", **safe,
        }, separators=(",", ":"), allow_nan=False)
        if len(payload.encode("utf-8")) > 4096:
            raise ValueError("oversized event")
        logger.info(payload)
    except Exception:
        # Diagnostics must not stringify the event, props or exception: even an
        # unknown event name/error can contain private content. A second logging
        # failure must not turn an already committed domain operation into a 500.
        try:
            diagnostics.warning("analytics emit failed: event dropped")
        except Exception:
            pass
