"""c290: prove the rate limiters actually REFUSE in prod, against real Redis.

NOT a load test, and deliberately not part of the harness next door. The c226 runs
recorded ZERO 429s across 41,439 requests, which proves only that the limiters do not
fire SPURIOUSLY on paced traffic: the harness paced at 50% of the ceilings and
substituted a read whenever a write's pacing bucket was empty (44-45% of intended writes
never happened - see metrics.record_substituted_write). So the ENFORCEMENT branch has
never executed against real Redis. Every 429 this codebase has produced came from the
in-process fallback in the test suite.

One account, one endpoint, one question: does the Redis fixed-window path increment and
refuse on the real stack, and does the machine-readable detail reach a real client?

/media/upload-url is chosen as the cheapest possible target: it mints signed URLs and
uploads nothing, the tmp/ lifecycle rule eats any objects, it writes no rows, and it
needs no cleanup. The cost is ~61 signed-URL mints, the same number of IAM signBlob
calls, and that one account's media budget for the rest of its window.

TOKEN CONTRACT: CHIRP_ID_TOKEN, a Firebase ID token minted by the manager from the
provisioning refresh-token store at T-0. Tokens live ONE HOUR, so a manifest minted for
an earlier run is dead and cannot be reused. Never committed, never passed on a command
line that lands in shell history - export it, run, unset it.

    CHIRP_ID_TOKEN='<fresh token>' python -m loadtest.enforcement_probe

WHY THE INTERPRETATION LIVES IN A PURE FUNCTION. The part of this that must not be wrong
is not the HTTP loop - it is what we conclude from the result, and a probe that can only
be exercised by pointing it at prod is a probe whose reasoning is never tested. So
`interpret` takes plain data and returns a verdict, and tests/test_c290_enforcement_probe.py
drives every branch against a fake clock and synthetic statuses. No network, no prod.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

BASE_URL = "https://chirp-api-593616178468.us-central1.run.app"
PROBE_PATH = "/media/upload-url"

# These three MUST track app.core.rate_limits.MEDIA_UPLOAD_URL_LIMIT. If that limit
# changes and these do not, the probe reports EARLY or NO_REFUSAL against a limiter that
# is behaving correctly.
WINDOW_SECONDS = 600
EXPECTED_LIMIT = 60
EXPECTED_DETAIL = "media_upload_url_rate_limited"

# Stop well short of a second window even if nothing ever refuses, so a broken limiter
# costs a bounded number of mints rather than an open-ended loop.
MAX_ATTEMPTS = 90
# A probe needs at least this much window left to fit inside one bucket.
BOUNDARY_MARGIN_SECONDS = 120

# The route is 201, not 200: create_upload_url is declared status_code=201.
CREATED = 201


def window_bucket(now: float, window_seconds: int = WINDOW_SECONDS) -> int:
    """The fixed-window bucket `now` falls in.

    Mirrors app.services.rate_limit.allow's prod path exactly:
    `bucket = int(time.time() // window_seconds)`. Prod is a FIXED window, not a rolling
    one, which is the whole reason the straddle problem below exists.
    """
    return int(now // window_seconds)


def seconds_to_wait_for_clean_window(
    now: float,
    window_seconds: int = WINDOW_SECONDS,
    margin: int = BOUNDARY_MARGIN_SECONDS,
) -> float:
    """How long to sleep so the probe runs inside ONE bucket. 0 if it already fits.

    THIS IS THE DIFFERENCE BETWEEN A PROBE AND A COIN FLIP. Because the window is fixed
    rather than rolling, a run that crosses a boundary is handed a fresh full budget
    partway through and never trips - and that outcome is indistinguishable from "the
    shared Redis window is not counting at all", which is precisely the failure this
    probe exists to detect. Concluding the latter from the former would send someone
    debugging Redis for what was calendar arithmetic.
    """
    remaining = window_seconds - (now % window_seconds)
    return remaining + 1 if remaining < margin else 0.0


@dataclass(frozen=True)
class Outcome:
    verdict: str
    detail: str


def interpret(
    *,
    attempts: int,
    first_429_at: int | None,
    detail: str | None,
    start_bucket: int,
    end_bucket: int,
    expected_limit: int = EXPECTED_LIMIT,
    expected_detail: str = EXPECTED_DETAIL,
) -> Outcome:
    """Turn a probe run into a verdict. Pure - this is the part that must not be wrong.

    The straddle check comes FIRST and outranks everything, including an apparent pass:
    a run spanning two buckets tells us nothing either way, and reporting anything else
    about it would be reporting noise as a finding.

    EARLY IS NOT A PASS, and is kept distinct from NO_REFUSAL on purpose. A limiter that
    refuses before its ceiling is a student-facing bug - it means a real person doing
    ordinary work gets told to stop - which is a different and worse problem than a
    limiter that never refuses at all. Collapsing the two into "did it 429? yes/no" is
    how the interesting failure gets filed as a success.
    """
    if start_bucket != end_bucket:
        return Outcome(
            "STRADDLED",
            "the run crossed a fixed-window boundary and was handed a fresh budget "
            "mid-probe; nothing can be concluded either way - re-run inside one bucket",
        )
    if first_429_at is None:
        return Outcome(
            "NO_REFUSAL",
            f"{attempts} attempts, no 429. Either the shared Redis window is not "
            f"counting across instances, or the real ceiling is above {attempts}",
        )
    if first_429_at < expected_limit + 1:
        return Outcome(
            "EARLY",
            f"refused on attempt {first_429_at}, expected {expected_limit + 1}. A real "
            "user doing ordinary work would hit this too - investigate before launch",
        )
    if first_429_at > expected_limit + 1:
        return Outcome(
            "LATE",
            f"refused on attempt {first_429_at}, expected {expected_limit + 1}. The "
            "ceiling in prod is not the ceiling in the code",
        )
    if detail != expected_detail:
        return Outcome(
            "WRONG_DETAIL",
            f"refused in the right place but carried {detail!r}, not "
            f"{expected_detail!r}; a client cannot tell which limit it hit",
        )
    return Outcome(
        "PASS",
        f"refused on attempt {first_429_at} with {detail!r} - the Redis fixed-window "
        "path increments and refuses on the real stack",
    )


def _post(token: str) -> tuple[int, str | None]:
    """One mint attempt. Returns (status, detail-or-None); status 0 means transport."""
    request = urllib.request.Request(
        BASE_URL + PROBE_PATH,
        data=json.dumps({"content_type": "image/jpeg", "byte_size": 1000}).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read()
            return response.status, None
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode()).get("detail")
        except Exception:
            return exc.code, None
    except Exception as exc:  # transport, not a response
        return 0, type(exc).__name__


def main() -> int:
    token = os.environ.get("CHIRP_ID_TOKEN", "").strip()
    if not token:
        print(
            "CHIRP_ID_TOKEN is unset. A FRESH Firebase ID token is required - they live "
            "one hour, so a manifest minted for an earlier run is already dead.",
            file=sys.stderr,
        )
        return 2

    wait = seconds_to_wait_for_clean_window(time.time())
    if wait:
        print(f"waiting {wait:.0f}s for a clean window boundary so the probe cannot straddle")
        time.sleep(wait)

    start_bucket = window_bucket(time.time())
    created = 0
    first_429_at: int | None = None
    detail: str | None = None
    attempt = 0

    while attempt < MAX_ATTEMPTS:
        attempt += 1
        status, body_detail = _post(token)
        if status == 0:
            print(f"attempt {attempt}: transport failure ({body_detail}) - stopping")
            break
        if status == CREATED:
            created += 1
        elif status == 429:
            first_429_at, detail = attempt, body_detail
            break
        else:
            print(f"attempt {attempt}: unexpected status {status} detail={body_detail!r} - stopping")
            break
        if window_bucket(time.time()) != start_bucket:
            break

    end_bucket = window_bucket(time.time())
    outcome = interpret(
        attempts=attempt,
        first_429_at=first_429_at,
        detail=detail,
        start_bucket=start_bucket,
        end_bucket=end_bucket,
    )
    print(f"\nattempts={attempt} created={created} first_429_at={first_429_at} detail={detail!r}")
    print(f"buckets start={start_bucket} end={end_bucket} straddled={start_bucket != end_bucket}")
    print(f"\n{outcome.verdict}: {outcome.detail}")
    return 0 if outcome.verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
