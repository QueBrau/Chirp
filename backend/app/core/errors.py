"""Shared HTTPException helpers so routers raise consistent error shapes."""
from fastapi import HTTPException


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
