"""Wire the app's own module loggers (`logging.getLogger(__name__)` everywhere under
`app/`) to a stdout handler at startup (board c176).

THE BUG THIS FIXES, confirmed empirically before writing a line of this file:
nothing in this codebase ever called `logging.basicConfig` or
`logging.config.dictConfig` for the app's own loggers. uvicorn configures ITS OWN
"uvicorn" / "uvicorn.access" / "uvicorn.error" loggers when it starts — that is why
the access log line for every request always reached Cloud Logging — but every
`app.*` logger (email_service, the WS gateway, the Redis startup probe,
media_reconcile, ...) sat on top of a bare, unconfigured root logger. Two things
compounded: (1) an unconfigured root logger defaults to level WARNING, so
`logger.info(...)` never even passed `isEnabledFor()` — the record was never built
at all; (2) even a WARNING/ERROR call that did get built had no handler anywhere in
its chain, so it fell through to Python's `lastResort` handler, a hardwired
StreamHandler on stderr. Net effect: `logger.info` calls vanished outright, and
`logger.warning`/`logger.error` calls escaped only via `lastResort`'s bare
`%(message)s` formatting, not anything Cloud Run-legible. Reproduced locally with a
real uvicorn process and a real POST /auth/campus-verification: the uvicorn access
log line landed, `email_service.py`'s `logger.info("email sent ...")` did not appear
on either stream. See `tests/test_app_logging.py` for the falsifying test.

FIX SCOPE, deliberately narrow: this configures exactly one logger, "app" — the
common ancestor of every `app.*` module logger by Python's dotted-name convention —
with one handler, INFO and above, formatted with uvicorn's own formatter class so a
Cloud Logging viewer cannot tell an app line from a uvicorn one by shape alone.
`propagate` is left at its default (True) on purpose: pytest's `caplog` fixture
captures by attaching its own handler to the ROOT logger, and records only reach it
by propagating there. Setting `propagate=False` here would make this fix invisible
to `caplog` and silently break every test in `test_email_service.py` that asserts on
`caplog.records` — the redaction-discipline tests that must never go dark. This does
NOT touch "uvicorn", "uvicorn.access" or "uvicorn.error" — those are already
configured by uvicorn itself and work today; touching them risks c146's
credential-scrubbing filter on uvicorn.access, which is out of scope here.
"""
from __future__ import annotations

import logging.config


def configure_app_logging() -> None:
    """Idempotently attach a stdout handler to the "app" logger at INFO.

    Safe to call more than once — `create_app()` does, once per test via the
    `client` fixture, and uvicorn's `--factory` could in principle call it again on
    a reload. `dictConfig` REPLACES the named logger's handler list on every call
    rather than appending to it (see `logging.config.DictConfigurator`), so repeated
    calls cannot accumulate duplicate handlers and double-print every line.
    """
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "app": {
                    "()": "uvicorn.logging.DefaultFormatter",
                    "fmt": "%(levelprefix)s %(name)s - %(message)s",
                },
            },
            "handlers": {
                "app": {
                    "class": "logging.StreamHandler",
                    "formatter": "app",
                    "stream": "ext://sys.stdout",
                },
            },
            "loggers": {
                "app": {
                    "handlers": ["app"],
                    "level": "INFO",
                },
            },
        }
    )
