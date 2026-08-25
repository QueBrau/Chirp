"""Shared field-level validation helpers for schemas that accept client input.

validate_public_url exists because of c184: alumni.linkedin_url and
job_posts.apply_url were persisted as plain `str | None` with zero validation,
and the mobile client opens them blind via Linking.openURL — a verified
phishing / intent-URI vector (c182 audit finding). `javascript:`, `intent://`,
`data:`, and similar schemes are not hypothetical there; Linking.openURL will
attempt to hand them to the OS.
"""
from urllib.parse import urlsplit

MAX_URL_LENGTH = 2048
_ALLOWED_SCHEMES = {"http", "https"}


def validate_public_url(value: str | None) -> str | None:
    """Reject anything that is not a well-formed http(s) URL. None passes through.

    Applied only to schemas that accept CLIENT input (e.g. AlumniProfileUpdate,
    JobPostCreate/Update, EventCreate, UserCreate/Update) — never to the *Out
    schemas that read existing rows back via `from_attributes`, so a row already
    in the database that predates this validator can still be read without a
    500. The column stays a plain `str`; this only narrows what NEW input may
    write into it.
    """
    if value is None:
        return None
    if len(value) > MAX_URL_LENGTH:
        raise ValueError(f"url must be at most {MAX_URL_LENGTH} characters")
    parts = urlsplit(value)
    if parts.scheme not in _ALLOWED_SCHEMES:
        raise ValueError("url must start with http:// or https://")
    if not parts.netloc:
        raise ValueError("url must include a host")
    return value
