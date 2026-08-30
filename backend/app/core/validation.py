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

# ---- content-body ceilings (c245) ----
#
# Every user-supplied content body was Field(min_length=1) with no upper bound, so
# the effective ceiling was Cloud Run's 32MB request limit: one chirp could carry
# megabytes into a Text column that every reader of that feed then downloads. The
# package already caps display_name at 80 and a poll question at 500, so a bounded
# body is the house convention, not a new policy.
#
# These live here, next to MAX_URL_LENGTH, because the reason cap is shared across
# schemas.chirp and schemas.moderation and two byte-identical copies is exactly the
# drift this module exists to prevent.
#
# THE BIAS IS DELIBERATELY GENEROUS. The job is to stop a script, not to edit a
# student: a cap a real person can hit arrives as a bug report, while a cap only a
# script can hit is doing its job silently. Every number below is several times the
# longest input a real user plausibly writes, and all of them are four orders of
# magnitude under the 32MB they replace.

# A chirp is the anonymous board's short form, not an essay: ~300 words, already
# several phone screens. A student venting at length lands nowhere near it.
MAX_CHIRP_BODY_LENGTH = 2_000

# A feed post IS the long form — a chapter announcement carrying full event details
# or a philanthropy recap is a legitimately long piece of writing, so this gets a
# ceiling several times the chirp's rather than sharing it.
MAX_POST_BODY_LENGTH = 10_000

# A comment is a reply, not a post. Same shape as a chirp, so the same number.
MAX_COMMENT_BODY_LENGTH = 2_000

# A moderation reason is a sentence or two a human types to explain an action, and
# it is read later from an audit row. 1,000 leaves room for a reporter describing
# harassment in detail without inviting an essay into moderation_actions.
MAX_REASON_LENGTH = 1_000


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
