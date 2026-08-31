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

# ---- message content (c252) ----
#
# THESE ARE NOT PROSE AND MUST NOT BE SIZED LIKE IT. A ciphertext cap picked by
# analogy to the post cap would be wrong in a way that only shows up when a real
# long message fails to send, so both numbers below are derived forward from ONE
# decision - the largest plaintext message we intend to carry - rather than chosen
# to look reasonable next to MAX_POST_BODY_LENGTH.

# The decision everything else here derives from. Set equal to the post ceiling on
# purpose: the longest thing a student writes anywhere in Chirp is a long-form post,
# and pasting that same text into a DM is ordinary behaviour, so a message must be
# able to carry at least what a post can.
MAX_MESSAGE_PLAINTEXT_LENGTH = 10_000

# A reported E2EE message's decrypted body, forwarded to moderators (SPEC 6.7). It
# is one of the messages above, so it inherits that ceiling rather than getting an
# independently chosen one — pick these apart and a message becomes reportable only
# up to some other number, which is a bug nobody would find until a moderator needed
# the evidence.
MAX_FORWARDED_PLAINTEXT_LENGTH = MAX_MESSAGE_PLAINTEXT_LENGTH

# Ciphertext expands over its plaintext TWICE, and the cap has to survive both:
#
#   1. UTF-8      10,000 characters x 4 bytes worst case (emoji, CJK)  = 40,000 bytes
#   2. Signal     Double Ratchet framing, worst case a PreKeySignalMessage
#                 (identity key + base key + registration/prekey ids on top of the
#                 ratchet message), plus AES-CBC padding to a 16-byte boundary
#                                                            + ~256 bytes
#                                                            = 40,256 bytes
#   3. base64     4 characters per 3 bytes, rounded to a whole group:
#                 ceil(40,256 / 3) = 13,419 groups x 4       = 53,676 characters
#
# Rounded up to 64 KiB, which leaves ~22% over the derived figure for a protocol
# version that frames messages slightly larger, and reads as deliberate rather than
# as a number someone typed. Still ~650x smaller than the 32MB request limit that
# was the real ceiling before this.
#
# libsignal is a typed stub today (app-mobile/src/crypto/signal.ts throws until the
# milestone-3 spike), so this is derived from the intended protocol, NOT measured
# against live output. Re-check the arithmetic when real ciphertext first flows.
MAX_CIPHERTEXT_B64_LENGTH = 65_536


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
