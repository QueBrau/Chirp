"""app.core.logging_config: wiring for board c176 (module loggers reaching stdout).

Before that module existed, the only thing standing between an `app.*` module's
`logger.info(...)` call and the void was Python's own defaults: an unconfigured root
logger's effective level is WARNING, so an INFO record is never even built, and any
WARNING/ERROR record that IS built falls through to `lastResort` (stderr only) if no
handler exists anywhere in the chain. Confirmed with a real `uvicorn` process and a
real `POST /auth/campus-verification` call (board c176): uvicorn's own access log
line landed, `email_service.py`'s `logger.info("email sent ...")` did not appear on
either stream.

This test deliberately does NOT use `caplog`. pytest's `caplog` fixture attaches its
own capturing handler directly to the ROOT logger; since these loggers propagate by
default regardless of whether `app` itself has a handler, `caplog` would see the
record either way and cannot tell this bug apart from the fix — it is not a valid
falsifying test for THIS bug (`test_email_service.py`'s caplog-based tests are still
the right tool for the redaction-content assertions they make; this file is about
whether the line reaches a stream at all).

Runs in a completely fresh subprocess instead: no pytest logging instrumentation,
the same shape a `uvicorn app.main:create_app --factory` process runs in. Revert
`configure_app_logging()`'s call in app.main.create_app (or gut the function body)
and this test goes red — that is the falsification this file exists to provide.
"""
from __future__ import annotations

import subprocess
import sys

from tests.conftest import BACKEND_DIR

_MARKER = "c176 app-logger marker line"
_DEBUG_MARKER = "c176 app-logger DEBUG marker - must not appear"

_SCRIPT = (
    "import logging\n"
    "from app.main import create_app\n"
    "create_app()\n"
    "log = logging.getLogger('app.tests.c176_marker')\n"
    f"log.debug({_DEBUG_MARKER!r})\n"
    f"log.info({_MARKER!r})\n"
)


def test_app_logger_info_reaches_stdout_in_a_fresh_process() -> None:
    """The exact shape of the c176 bug, reproduced outside pytest's own logging
    instrumentation: a bare `python -c` process, no caplog, no root handler pytest
    may have wired for itself. Before the fix this prints nothing on either stream —
    an INFO call never reaches Python's isEnabledFor() gate at WARNING-default, let
    alone a handler. After the fix, create_app() wires "app" to a stdout handler at
    INFO and the line lands, formatted with uvicorn's own formatter class.
    """
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        cwd=str(BACKEND_DIR),
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert _MARKER in result.stdout, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    # The fix must not turn the volume up past what was asked for: DEBUG stays off,
    # on stdout or stderr, exactly like it was (silently) before.
    assert _DEBUG_MARKER not in result.stdout
    assert _DEBUG_MARKER not in result.stderr
