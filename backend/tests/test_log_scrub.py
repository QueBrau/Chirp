"""app.core.log_scrub: the credential-in-a-URL tripwire (c146).

Tests the EMISSION — what actually comes out of the logger once formatted — not the
filter object's return value in isolation. A logging filter is exactly the kind of
code that can silently stop doing anything (wrong logger name, filter never attached,
regex no longer matches a real call shape) while still returning True and looking
fine to a test that only calls .filter() directly. Going through the real logger is
what would have caught any of those.

Deliberately has no dependency on the app/db/client fixtures - this is a pure logging
concern and should stay fast and isolated from everything else in the suite.
"""
from __future__ import annotations

import logging

import pytest

from app.core.log_scrub import install_credential_log_scrub

ACCESS_LOGGER_NAME = "uvicorn.access"


@pytest.fixture(autouse=True)
def _installed():
    """Idempotent by design, but call it explicitly rather than relying on some
    earlier test's create_app() having already done it - this file's assumptions
    should not depend on suite ordering."""
    install_credential_log_scrub()


def _emit_and_capture(caplog: pytest.LogCaptureFixture, msg: str, *args: str) -> str:
    """Log through the REAL uvicorn.access logger and return the formatted message
    exactly as a handler would see it after every attached filter has run."""
    logger = logging.getLogger(ACCESS_LOGGER_NAME)
    with caplog.at_level(logging.INFO, logger=ACCESS_LOGGER_NAME):
        logger.info(msg, *args)
    assert len(caplog.records) == 1, "expected exactly one emitted record"
    return caplog.records[0].getMessage()


# ---------------------------------------------------------------------------
# The shape uvicorn's real access log actually uses: a %s-args tuple, not an
# f-string. This is the branch the original filter existed for in the first place.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "param",
    ["token", "access_token", "id_token"],
    ids=["token", "access_token", "id_token"],
)
def test_credential_query_params_are_redacted_in_the_args_tuple(
    caplog: pytest.LogCaptureFixture, param: str
) -> None:
    """THE POINT OF c146: the next regression will not necessarily be named `token`.
    access_token and id_token must be caught too, or this is coverage theater."""
    url = f'GET /ws?{param}=super-secret-value HTTP/1.1" 200'
    emitted = _emit_and_capture(caplog, '%s - "%s', "127.0.0.1:0", url)

    assert "super-secret-value" not in emitted
    assert f"{param}=[REDACTED]" in emitted


def test_credential_query_param_is_redacted_case_insensitively(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Query param casing is not guaranteed by any spec; a filter that only matches
    lowercase is a gap the moment a client capitalizes it."""
    url = 'GET /ws?Token=super-secret-value HTTP/1.1" 200'
    emitted = _emit_and_capture(caplog, '%s - "%s', "127.0.0.1:0", url)

    assert "super-secret-value" not in emitted


def test_a_second_credential_param_further_in_the_query_string_is_also_redacted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regex must not stop after the first match - a request line legitimately has
    other query params around a credential."""
    url = 'GET /ws?campus_id=abc&access_token=super-secret-value&debug=1 HTTP/1.1" 200'
    emitted = _emit_and_capture(caplog, '%s - "%s', "127.0.0.1:0", url)

    assert "super-secret-value" not in emitted
    assert "campus_id=abc" in emitted, "unrelated params must survive untouched"
    assert "debug=1" in emitted, "unrelated params must survive untouched"


# ---------------------------------------------------------------------------
# The plain-message branch — a direct logger.info(f"...") call, not %s-args.
# ---------------------------------------------------------------------------


def test_credential_query_param_is_redacted_in_a_plain_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    emitted = _emit_and_capture(
        caplog, "handled request for /auth/campus-verification?id_token=super-secret-value"
    )

    assert "super-secret-value" not in emitted
    assert "id_token=[REDACTED]" in emitted


# ---------------------------------------------------------------------------
# Must not scrub what it has no business touching.
# ---------------------------------------------------------------------------


def test_a_normal_request_line_with_no_credential_passes_through_untouched(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The filter must be a no-op on the overwhelming majority of real traffic - if
    this test ever shows a scrubbed or mangled line, the regex is too broad."""
    url = 'GET /chapters/abc-123/posts?limit=20&cursor=xyz HTTP/1.1" 200'
    emitted = _emit_and_capture(caplog, '%s - "%s', "127.0.0.1:0", url)

    assert emitted == '127.0.0.1:0 - "GET /chapters/abc-123/posts?limit=20&cursor=xyz HTTP/1.1" 200'


def test_a_param_that_merely_contains_token_as_a_substring_is_not_touched(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """tokenized_id is a real-shaped param name that is NOT a credential; the regex
    anchors on token/access_token/id_token specifically via the [?&] boundary and
    must not fire on every string containing the letters "token"."""
    url = 'GET /events?tokenized_id=abc123 HTTP/1.1" 200'
    emitted = _emit_and_capture(caplog, '%s - "%s', "127.0.0.1:0", url)

    assert "tokenized_id=abc123" in emitted


def test_install_is_idempotent() -> None:
    """Calling it twice (as this file's own fixture already does, on top of whatever
    create_app() did during the test session) must not attach the filter twice —
    that would double-run the substitution, harmless in effect but a sign something
    is wrong if it starts happening silently."""
    install_credential_log_scrub()
    install_credential_log_scrub()

    from app.core.log_scrub import _RedactCredentialQueryParamsFilter

    access_logger = logging.getLogger(ACCESS_LOGGER_NAME)
    matching = [f for f in access_logger.filters if isinstance(f, _RedactCredentialQueryParamsFilter)]
    assert len(matching) == 1
