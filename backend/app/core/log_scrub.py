"""Scrub credential-shaped query params from uvicorn's access log (c146).

THIS IS A TRIPWIRE, NOT A FALLBACK. The mechanism it originally covered — WS auth
via `?token=<firebase-id-token>` in the URL — is gone; c143 moved that onto the
WebSocket subprotocol, where it never reaches a URL a web server logs at all. If this
filter never fires again, that is the fix working, not this file being dead code.

What this actually guards against is a DIFFERENT failure: some future client putting
a credential back into a URL — a stale mobile build still on the old query-string
scheme, a third-party integration bolted on later, a developer's debug curl with
`?token=` pasted in by habit. uvicorn's access log writes the full request line,
query string included, to stdout — which on Cloud Run means Cloud Logging, readable
by anyone with log access. A credential that lands there is compromised the moment
it's logged, auth mechanism notwithstanding.

WHY THIS IS NOT THE OLD _RedactWsTokenFilter WITH A NEW NAME: that filter matched
`token=` literally, because that was the one param the WS handshake actually used.
The next regression will not necessarily reuse that name. THE FIRST VERSION OF THIS
FILE MADE THE SAME MISTAKE ONE LEVEL UP: it listed `token`, `access_token` and
`id_token` as three literal alternatives, which is still a fixed list — a future
`refresh_token=`, `auth_token=`, `session_token=` or `api_token=` would have sailed
through unscrubbed, and `refresh_token` is arguably the worst one to miss (long-lived,
unlike a self-expiring ID token). "The filter only catches the param names we already
thought of" is not a tripwire, it is a false sense of coverage, regardless of whether
the list has one entry or three. This version matches the SHAPE — any query param
whose name ends in `token=` — rather than any enumeration of literals.

SCOPE, deliberately held: this attaches to `uvicorn.access` only, the same target
the original used, because that is specifically the channel that writes a request's
full URL unprompted — every request, whether the app's own code ever logs anything
or not. Scrubbing arbitrary application log calls for credential-shaped strings is a
different, much larger feature (what counts as a credential in a log message is not
a regex problem) and is not what this card asked for.
"""
from __future__ import annotations

import logging
import re

# Matches [?&]<anything>token=<value> — token, access_token, id_token,
# refresh_token, auth_token, whatever the next one is named — case-insensitive on
# the param name, since query param casing is not guaranteed and there is no cost
# to covering it. The value is greedy up to the next & or whitespace, same
# boundary the original used. Deliberately NOT an enumerated list of prefixes:
# that was this file's own first draft, and it repeated the exact mistake it set
# out to fix one level up (see the module docstring).
_CREDENTIAL_QS_RE = re.compile(r"([?&][a-z0-9_]*token=)[^&\s]+", re.IGNORECASE)

# This hint is deliberately as broad as the regex's own suffix match, not
# narrower — "token=" appearing anywhere is necessary for the regex to match
# ANY param this filter targets, since every one of them ends in that literal.
# A log line with no "token=" substring at all can never match, so this is a
# cheap, always-safe fast path that skips the sub() call on the overwhelming
# majority of ordinary request lines.
_FAST_PATH_HINT = "token="


class _RedactCredentialQueryParamsFilter(logging.Filter):
    """Redacts any *token=-shaped query param from a log record in place.

    Mirrors the shape both of uvicorn's access-log call (args tuple) and of a plain
    string message, since either can appear depending on how a record was built.
    Always returns True — this filter never drops a record, only rewrites it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # record.msg and record.args are scrubbed INDEPENDENTLY, not as an if/elif
        # pair - the original version this generalizes used if/elif here, which
        # silently never scrubbed a plain message (logger.info("...token=...")) with
        # no %s args, because record.args for that call is () (an EMPTY tuple), and
        # isinstance((), tuple) is True - the args branch "wins" the elif and does
        # nothing, since it has zero elements to iterate. Caught by this file's own
        # plain-message test failing against that exact structure.
        if isinstance(record.msg, str) and _FAST_PATH_HINT in record.msg.lower():
            record.msg = _CREDENTIAL_QS_RE.sub(r"\1[REDACTED]", record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                _CREDENTIAL_QS_RE.sub(r"\1[REDACTED]", value)
                if isinstance(value, str) and _FAST_PATH_HINT in value.lower()
                else value
                for value in record.args
            )
        return True


def install_credential_log_scrub() -> None:
    """Idempotently attach the redaction filter to uvicorn's access logger.

    Called once from app.main.create_app, not as an import-time side effect — a bare
    module-level call that mutates global logging state on import is a surprising
    thing to trip over when reading an unrelated file, which is how this attached
    the first time around.
    """
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _RedactCredentialQueryParamsFilter) for f in access_logger.filters):
        access_logger.addFilter(_RedactCredentialQueryParamsFilter())
