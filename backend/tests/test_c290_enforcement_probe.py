"""c290: the prod enforcement probe's REASONING, tested without prod.

The probe itself can only be exercised against the live stack with a one-hour token, so
the part that must not be wrong — what we conclude from a run — is a pure function, and
this file drives every branch of it against a fake clock and synthetic statuses.

Two of these tests exist because the obvious probe design is wrong in a way that reads
like a real finding:

  A fixed-window limiter (bucket = int(time.time() // 600), which is what prod runs)
  hands a run that crosses a boundary a fresh full budget partway through. Such a run
  never trips — and "never tripped" is EXACTLY what a limiter that is not counting
  across instances looks like. Concluding the latter from the former sends someone
  debugging Redis for calendar arithmetic. So the straddle check outranks every other
  verdict, including an apparent pass, and test_a_straddled_run_is_never_interpreted
  pins that ordering.

The other distinction worth its own test is EARLY vs NO_REFUSAL. Both are "the limiter
did not do what we expected", and collapsing them into one is how the interesting
failure gets filed as a success: a limiter refusing BEFORE its ceiling is a
student-facing bug, because a real person doing ordinary work is being told to stop.
"""
from __future__ import annotations

import pytest

from loadtest.enforcement_probe import (
    EXPECTED_DETAIL,
    EXPECTED_LIMIT,
    WINDOW_SECONDS,
    interpret,
    seconds_to_wait_for_clean_window,
    window_bucket,
)

PASSING = dict(
    attempts=EXPECTED_LIMIT + 1,
    first_429_at=EXPECTED_LIMIT + 1,
    detail=EXPECTED_DETAIL,
    start_bucket=100,
    end_bucket=100,
)


# ---------------------------------------------------------------------------
# the fake clock: window arithmetic
# ---------------------------------------------------------------------------


def test_bucket_matches_the_limiters_own_arithmetic() -> None:
    """Must mirror app.services.rate_limit.allow's prod path exactly, or the probe is
    reasoning about a different window than the one being tested."""
    assert window_bucket(0.0) == 0
    assert window_bucket(WINDOW_SECONDS - 1) == 0
    assert window_bucket(WINDOW_SECONDS) == 1
    assert window_bucket(WINDOW_SECONDS * 7 + 1) == 7


def test_a_window_with_room_is_started_immediately() -> None:
    """Fresh boundary: the whole window is available, so there is nothing to wait for."""
    assert seconds_to_wait_for_clean_window(WINDOW_SECONDS * 5.0) == 0.0
    # Exactly at the margin is still enough room.
    assert seconds_to_wait_for_clean_window(WINDOW_SECONDS * 5.0 + (WINDOW_SECONDS - 120)) == 0.0


def test_a_nearly_expired_window_waits_for_the_next_boundary() -> None:
    """The case that makes the probe honest: 10 seconds of budget left is not a probe,
    it is a straddle waiting to happen."""
    now = WINDOW_SECONDS * 5.0 + (WINDOW_SECONDS - 10)
    wait = seconds_to_wait_for_clean_window(now)
    assert wait == pytest.approx(11.0)
    # And waiting that long genuinely lands in the NEXT bucket, with a full window.
    assert window_bucket(now + wait) == window_bucket(now) + 1


# ---------------------------------------------------------------------------
# the verdicts
# ---------------------------------------------------------------------------


def test_the_expected_refusal_is_a_pass() -> None:
    assert interpret(**PASSING).verdict == "PASS"


def test_a_straddled_run_is_never_interpreted() -> None:
    """Outranks everything, including a result that would otherwise read as a pass.

    If this ever returns PASS, the probe has started reporting noise as evidence.
    """
    straddled = {**PASSING, "end_bucket": 101}
    assert interpret(**straddled).verdict == "STRADDLED"

    # Even a run that looks like a clean NO_REFUSAL must not be reported as one.
    also_straddled = {**PASSING, "first_429_at": None, "detail": None, "end_bucket": 101}
    assert interpret(**also_straddled).verdict == "STRADDLED"


def test_no_refusal_is_reported_as_the_open_question_it_is() -> None:
    outcome = interpret(**{**PASSING, "attempts": 90, "first_429_at": None, "detail": None})
    assert outcome.verdict == "NO_REFUSAL"
    assert "not counting" in outcome.detail


def test_early_refusal_is_its_own_verdict_and_not_a_pass() -> None:
    """A limiter that refuses at 40 when the ceiling is 60 is a bug a real student hits.
    It must never be collapsed into either PASS or NO_REFUSAL."""
    outcome = interpret(**{**PASSING, "attempts": 40, "first_429_at": 40})
    assert outcome.verdict == "EARLY"
    assert "real user" in outcome.detail


def test_late_refusal_means_prod_is_not_running_the_code_we_think() -> None:
    outcome = interpret(**{**PASSING, "attempts": 75, "first_429_at": 75})
    assert outcome.verdict == "LATE"


def test_the_right_refusal_carrying_the_wrong_detail_is_not_a_pass() -> None:
    """The detail string is the machine-readable half — a client that cannot tell WHICH
    limit it hit is the thing c259's per-scope detail exists to prevent."""
    outcome = interpret(**{**PASSING, "detail": "rate_limited"})
    assert outcome.verdict == "WRONG_DETAIL"


def test_the_probe_agrees_with_the_limit_it_is_probing() -> None:
    """The probe's constants must track app.core.rate_limits. If the limit moves and
    these do not, the probe reports EARLY or NO_REFUSAL against a healthy limiter —
    a false alarm that would be chased in prod."""
    from app.core.rate_limits import MEDIA_UPLOAD_URL_LIMIT

    max_calls, window_seconds = MEDIA_UPLOAD_URL_LIMIT
    assert EXPECTED_LIMIT == max_calls
    assert WINDOW_SECONDS == window_seconds
    assert EXPECTED_DETAIL == "media_upload_url_rate_limited"
