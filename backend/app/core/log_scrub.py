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
The next regression will not necessarily reuse that name — `access_token` and
`id_token` are exactly as likely, and "the filter only catches the param name we
already fixed" is not a tripwire, it's a false sense of coverage. This one matches
the SHAPE (anything ending in `token=`) rather than one literal string.

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

# Matches ?token=, &access_token=, &id_token=, case-insensitive on the param name —
# query param casing is not guaranteed, and there is no cost to covering it. The
# value is greedy up to the next & or whitespace, same boundary the original used.
_CREDENTIAL_QS_RE = re.compile(r"([?&](?:access_)?(?:id_)?token=)[^&\s]+", re.IGNORECASE)

# Every param this filter targets contains "token" as a substring (token,
# access_token, id_token all do), so this is a cheap, always-safe fast path: a log
# line with none of the three can never match the regex, and skips the sub() call
# entirely on the overwhelming majority of ordinary request lines.
_FAST_PATH_HINT = "token="


class _RedactCredentialQueryParamsFilter(logging.Filter):
    """Redacts token/access_token/id_token query params from a log record in place.

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
