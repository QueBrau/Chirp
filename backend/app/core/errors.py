"""Shared HTTPException helpers so routers raise consistent error shapes."""
from fastapi import HTTPException

# Migration 0028 (board c230): the cross_table_dues_guard_intents/plans triggers
# RAISE with this literal substring in their MESSAGE so both insert sites that can
# hit them — payments.py's reservation insert, finance.py's plan insert — agree on
# how to recognize the backstop rather than each guessing at the error shape.
CROSS_TABLE_DUES_GUARD_MARKER = "cross_table_dues_guard"


def not_found(detail: str = "not_found") -> HTTPException:
    """404 with a machine-readable detail string."""
    return HTTPException(status_code=404, detail=detail)


def forbidden(detail: str) -> HTTPException:
    """403 with a machine-readable detail string."""
    return HTTPException(status_code=403, detail=detail)


def conflict(detail: str) -> HTTPException:
    """409 with a machine-readable detail string."""
    return HTTPException(status_code=409, detail=detail)


def too_many_requests(detail: str = "rate_limited") -> HTTPException:
    """429 with a machine-readable detail string."""
    return HTTPException(status_code=429, detail=detail)


def is_cross_table_dues_guard_conflict(exc: Exception) -> bool:
    """True if `exc` is the cross_table_dues_guard trigger's RAISE (migration 0028).

    The trigger's RAISE EXCEPTION carries SQLSTATE P0001 with no more specific
    class, so asyncpg/SQLAlchemy surface it as a bare sqlalchemy.exc.DBAPIError —
    NOT an IntegrityError, even though the trigger is standing in for one
    (verified against a real trigger firing under this exact asyncpg dialect
    while writing migration 0028). Callers must therefore catch DBAPIError
    (IntegrityError included, since it is a subclass) and use this to tell the
    guard's own conflict apart from any other DBAPIError before treating it as a
    409 — an unrelated DBAPIError (a dropped connection, say) must keep
    propagating as the 500 it actually is.
    """
    return CROSS_TABLE_DUES_GUARD_MARKER in str(getattr(exc, "orig", exc))
