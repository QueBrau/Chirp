# Local cohort workload (c363)

This separate command observes a fixed mix of cursor reads, chapter rosters,
three successive paced inbox refreshes, and actual message delivery across
isolated synthetic cohorts. It preserves the default load harness and the
existing receipt/reconnect commands. It accepts only literal loopback HTTP/WS
origins, emulated authentication, and the private manifest created below.

It does not implement the proposed 60/15/15/10 staging workload. Photos, polls,
dues, production capacity, fairness acceptance, server warm/cold control, server
latency/pool waits, dependency/process/restore recovery, and device acceptance
remain `NOT_PROVEN`. The remote-load park and c287 HA trigger remain in force.

## Fixture and ownership

Use a dedicated local database/API/Redis process that belongs to this run.
Follow the database creation/migration and local process setup in
[MESSAGE-RECEIPTS.md](MESSAGE-RECEIPTS.md), including explicit
`ENV=local AUTH_MODE=emulated SERVICE_ROLE=all`. The database must use the same
strict `chirp_receipts_<8–32 lowercase hex characters>` disposable namespace;
the helper reuses its validated literal-loopback SQLAlchemy URL boundary.
An existing application database, hostname alias, ambiguous authority, query,
missing port, or existing application rows refuses creation. No cleanup/delete
operation is provided. Never point this at another session's database or process.

From `backend/`, using the already configured disposable database:

```sh
python -m loadtest.cohort_fixture
python -m loadtest.cohort_fixture --apply --out "$CHIRP_RECEIPT_DIR/cohorts.json"
```

The first command prints counts without opening a database or creating a file.
Only `--apply` writes. It reserves a new mode-0600 manifest before opening the
database; existing files and symlinks refuse before writes. The transaction
creates all rows together. If a commit/output response is lost, keep the private
file and inspect the disposable database before retrying; do not assume rollback.

The default two cohorts each contain a distinct campus, chapter, sender/device
and two selected recipients: six active users and four held sockets in total.
Their rosters contain 100 and 300 synthetic members. Each has 51 or 201 campus
posts, a separate set of 51 or 201 chapter posts, inbox conversations and
historical messages. Only the selected users
join the inbox conversations; the other roster members generate no requests or
fan-out. Repeated fixture timestamps exercise the actual compound cursor tie
break. Historical message reads use a fixed upper timestamp so newly accepted
messages cannot change the seeded denominator while the workload runs.

`--cohorts` allows 2–4, alternating the same profiles. `--recipients` allows 1–4
per cohort, subject to at most 20 active users and 10 selected sockets in total.
These are data-cardinality fixtures, not equivalent numbers of active clients.

## Run and evidence

Copy `cohort-config.yaml` to a private run directory and set its two literal
loopback ports to the owned API/WS listeners. Set `ws.max_sockets` for the total
selected recipients. `mix_weights: {me: 1}` and `ramp_in_seconds: 0` are required
fixed-plan settings; the command rejects other values rather than silently
claiming that mix ran.

```sh
python -m loadtest.cohort_workload \
  --config "$CHIRP_RECEIPT_DIR/cohort-config.yaml" \
  --manifest "$CHIRP_RECEIPT_DIR/cohorts.json" \
  --out "$CHIRP_RECEIPT_DIR/cohort-report.json"
```

Every selected socket must reach the exact gateway-ready fence before any
cohort's read phase starts. Each active member in each cohort must complete
campus-feed, chapter-post, inbox and historical-message cursor walks, an exact
roster check, and three first-page inbox refreshes. Page IDs, scope, membership,
ordering and full fixture denominators are validated. A wrong cohort, duplicate,
missing row, non-200 response, redirect, oversized response or transport error
stops the run. The instrument never follows a returned link or endpoint.
One sequential reader task per cohort rotates through its selected member
identities; the active-user count names those identities, not simultaneous HTTP
tasks. Receipt production overlaps the read tasks.

Every HTTP read and message POST shares one aggregate token bucket and semaphore:
at most 20 configured RPS with an explicitly reported initial burst, and at most
10 simultaneous permits. There are no extra reference clients. All message
dispatch starts share a serialized minimum four-second interval across cohorts.
There are 1–3 messages per cohort, at most 10 in total; no retry is automatic.
Each accepted HTTP 201 binds the exact message, conversation, sender device,
payload and creation time to every selected recipient's live WS event, using
the existing reviewed receipt machinery. The configured read duration is at
most 120 seconds and the aggregate wall/cleanup budget is 180 seconds. Reads
already dispatched at phase expiry may finish within their request timeout.

Success is `LOCAL_COHORT_MIX_OBSERVED` only when every cohort completes coverage
and its full receipt denominator during actual HTTP activity. Partial coverage,
missing receipt, abort, cancellation or cleanup failure yields `NOT_PROVEN`.
The report contains ordinal cohort/member labels, fixture counts, route/status
counts, client-response latency, scheduler wait, process CPU and event-loop
delay. It prints no raw identity, token, message ID or ciphertext.

Observed request shares describe these selected synthetic clients. There is
one isolated chapter per campus, so chapter and campus dimensions coincide.
Different data sizes and response costs can change the shares; they do not prove
server fairness or starvation resistance. Process CPU includes all work in that
Python process (including the app when an integration test runs both together).
Client latency, queue wait and loop delay are not server timing or proof that
the driver has spare capacity. A reusable artifact should be retained with the
exact source revision, private config/manifest and environment observations.

All local database tests for this slice run through `scripts/with-suite-lock`
to avoid contention with concurrent ticket work. No deployment is required for
these developer-only tools, and none of their evidence closes c363's separate
staging, recovery, device, HA or capacity acceptance.
