"""The skip guard must check WHICH tests skipped, not merely how many (card c402).

c103 added a ceiling: under CHIRP_REQUIRE_DB=1 a run that skips more tests than
CHIRP_MAX_SKIPS is failed, because a skip is not a pass and a collapsed run must
not be readable as evidence. c364 became the first card to raise that ceiling
above zero (6, for an opt-in EXPLAIN harness that is gated off in CI on purpose).

A raised count is a checksum, and a checksum cannot distinguish one-in-one-out
from nothing-changed. The day the c364 harness stops skipping here - renamed, no
longer collected, or CHIRP_EXPLAIN set in CI - six entirely unrelated tests could
start skipping and the total would still read 6. c402 therefore declares the
allowed skips by identity (CHIRP_ALLOWED_SKIP_PREFIXES, nodeid prefixes) and
fails on anything outside that list regardless of the total.

These tests drive REAL pytest subprocesses, the pattern tests/test_c394_redis_guard.py
established, because the guard lives in pytest_sessionfinish and only exists as
an exit code plus a message on a real run. The target is a real, already-present
always-skipping module rather than a fixture file written into the repo at test
time, so the subprocess exercises the actual conftest.

INHERITANCE IS THE TRAP HERE, and c394 hit it: CI sets CHIRP_REQUIRE_DB=1 for the
OUTER run, so a subprocess that merely inherits os.environ can pass or fail for
reasons the test never chose. Every subprocess below is given an explicit,
fully-specified environment for the four variables that matter, so each case is
the condition it claims to be on a developer machine and in CI alike.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
# Six tests, all skipped unless CHIRP_EXPLAIN=1. Asserted below rather than assumed.
ALWAYS_SKIPPING_TARGET = "tests/test_c364_query_plans.py"


def _run_pytest(target: str, *, require_db: str, max_skips: str, allowed: str | None) -> subprocess.CompletedProcess:
    """Run pytest on `target` in a subprocess with the guard's inputs pinned.

    Explicitly sets (or explicitly removes) every variable the guard reads, so
    nothing about the outer run - a developer shell, or CI's own
    CHIRP_REQUIRE_DB=1 - can decide the outcome.
    """
    env = dict(os.environ)
    env["CHIRP_REQUIRE_DB"] = require_db
    env["CHIRP_MAX_SKIPS"] = max_skips
    env.pop("CHIRP_EXPLAIN", None)  # the target must actually skip
    if allowed is None:
        env.pop("CHIRP_ALLOWED_SKIP_PREFIXES", None)
    else:
        env["CHIRP_ALLOWED_SKIP_PREFIXES"] = allowed
    return subprocess.run(
        [sys.executable, "-m", "pytest", target, "-q", "-p", "no:cacheprovider"],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )


def test_the_target_really_skips_and_nothing_else_runs() -> None:
    """The discriminating condition, constructed and asserted before it is relied on.

    Every case below means nothing unless this module genuinely skips its tests
    with CHIRP_EXPLAIN unset. If someone later makes that module run by default,
    these tests must fail loudly here rather than silently start passing on an
    empty skip list.
    """
    result = _run_pytest(
        ALWAYS_SKIPPING_TARGET, require_db="0", max_skips="99", allowed=ALWAYS_SKIPPING_TARGET
    )
    assert "skipped" in result.stdout, result.stdout[-2000:]
    assert " passed" not in result.stdout, (
        "the target module is no longer skip-only, so these cases no longer mean "
        f"what they claim: {result.stdout[-2000:]}"
    )


def test_declared_skips_pass_the_guard() -> None:
    """The allowed case: the skips are declared by identity, so the run is green."""
    result = _run_pytest(
        ALWAYS_SKIPPING_TARGET, require_db="1", max_skips="6", allowed=ALWAYS_SKIPPING_TARGET
    )
    assert result.returncode == 0, result.stdout[-3000:]
    assert "skipped that no one declared" not in result.stdout


def test_an_undeclared_skip_fails_even_inside_the_count_ceiling() -> None:
    """The whole point of c402, and the case a count alone cannot catch.

    The ceiling is 6 and exactly 6 tests skip, so c103's counting guard is
    satisfied and would let this run report green. The identity check must fail
    it anyway, because none of those six were declared.
    """
    result = _run_pytest(ALWAYS_SKIPPING_TARGET, require_db="1", max_skips="6", allowed=None)
    assert result.returncode != 0, (
        "an undeclared skip passed the guard while the total stayed inside the "
        f"ceiling - the count is still the only thing being checked: {result.stdout[-3000:]}"
    )
    assert "skipped that no one declared" in result.stdout, result.stdout[-3000:]


def test_the_failure_names_the_offending_tests() -> None:
    """A count cannot say what went missing; this guard must.

    Naming is the difference between 'something is wrong' and a person knowing
    where to look, which is the whole reason the ceiling exists.
    """
    result = _run_pytest(
        ALWAYS_SKIPPING_TARGET, require_db="1", max_skips="6", allowed="tests/some_other_file.py"
    )
    assert result.returncode != 0
    assert ALWAYS_SKIPPING_TARGET in result.stdout, result.stdout[-3000:]
    assert "::" in result.stdout


def test_a_declared_prefix_does_not_excuse_a_different_file() -> None:
    """Declaring one file must not bless another."""
    result = _run_pytest(
        ALWAYS_SKIPPING_TARGET,
        require_db="1",
        max_skips="6",
        allowed="tests/test_c365_query_plans.py",
    )
    assert result.returncode != 0, result.stdout[-3000:]


def test_a_directory_prefix_covers_the_files_under_it() -> None:
    """A prefix ending on a "/" boundary is the legitimate broad declaration."""
    result = _run_pytest(ALWAYS_SKIPPING_TARGET, require_db="1", max_skips="6", allowed="tests")
    assert result.returncode == 0, result.stdout[-3000:]


def test_a_partial_filename_prefix_blesses_nothing() -> None:
    """The anchoring case, found in review by constructing a file that went green.

    "tests/test_c364" is not a file or a directory, it is half a filename. Under
    a bare startswith it would bless tests/test_c364_query_plans.py AND an
    unrelated tests/test_c3640_probe.py, which is the silent coverage drift this
    card exists to prevent. A prefix must end on the whole nodeid, a "/" or a
    "::" boundary, so a half-filename matches nothing at all.

    Falsification: red without the boundary check in _unexpected_skips, because
    the six skips would be blessed and the run would exit 0.
    """
    result = _run_pytest(
        ALWAYS_SKIPPING_TARGET, require_db="1", max_skips="6", allowed="tests/test_c364"
    )
    assert result.returncode != 0, (
        "a half-filename prefix blessed a real module's skips - prefix matching is "
        f"unanchored: {result.stdout[-3000:]}"
    )
    assert "skipped that no one declared" in result.stdout


def test_unset_variable_keeps_c103_zero_tolerance() -> None:
    """Default stays c103's: with nothing declared, any skip fails the run.

    This is what a developer running the suite by hand gets, and it must not
    quietly become permissive just because CI declares an exception.
    """
    result = _run_pytest(ALWAYS_SKIPPING_TARGET, require_db="1", max_skips="0", allowed=None)
    assert result.returncode != 0, result.stdout[-3000:]


def test_the_guard_stays_off_without_require_db() -> None:
    """c103's own scope: the guard only speaks under CHIRP_REQUIRE_DB=1.

    Without it, skips are an ordinary local condition (no Redis, no service) and
    must not fail anyone's run.
    """
    result = _run_pytest(ALWAYS_SKIPPING_TARGET, require_db="0", max_skips="0", allowed=None)
    assert result.returncode == 0, result.stdout[-3000:]
    assert "skipped that no one declared" not in result.stdout
