# Load-test runbook (c226 / c363)

**THE PARK IS STILL ON.** Jose parked running the load test (board c226, Aug 30).
Nothing in this document is a licence to send one synthetic request at prod. The
harness enforces the park in code: a non-local target refuses to run unless the
config carries an `approval` block naming who approved and when, AND the operator
re-types `--confirm-park-lifted` at the command line, AND auth_mode is `firebase`
(emulated headers are ignored outside local anyway). This runbook exists so that
when Jose says GO, the run is a same-day action instead of a design session.

## What the test is for

This harness is an instrument for a future authorized mixed-workload test. The
c363 correction below proves local orchestration and accounting; it does not
establish a production user ceiling, receipt delivery, failover, or recovery.

Use [DEPLOY-CONFIGURATION.md](../../DEPLOY-CONFIGURATION.md),
[infra/deployment.json](../../infra/deployment.json), and its shared
`scripts/deployment-config` checker for current service/pool envelopes. The old
4-instance / 28-connection arithmetic formerly copied here is stale. Capture
fresh intended-versus-observed evidence before an authorized run; configured
concurrency and connection arithmetic are not measured capacity.

The c287 HA decision remains: **first invitations outside the home chapter OR
about 100 real users** trigger review/activation of the deferred HA pair. A local
harness pass does not waive that trigger. Availability targets, representative
staging load, fault recovery and the operational decision remain open on c363.

## Decision: the test runs WITH the c259 rate limits, not around them

Elevating limits for test accounts was considered and rejected:

1. The system students use HAS the limiter; measuring a limiter-less variant
   answers a question nobody is asking. The limiter's Redis round-trip is itself
   part of per-request latency under load — removing it removes real cost.
2. The harness paces every virtual user's writes to at most 50% of each c259
   limit (enforced by the config loader, not by discipline), so per-user 429s
   are expected to be ~zero. Concurrency scales by ADDING USERS, never by
   speeding a user up — which is also how real load arrives.
3. A rising 429 rate under pacing is therefore a genuine signal (limiter
   misbehaving, or shared-Redis contention) and gets its own abort criterion
   instead of being noise we configured ourselves into.

What this costs: we cannot measure "where does one hot user break" — which is
c259's job to prevent in the first place.

## Traffic the harness can and cannot generate, structurally

The mix touches: campus feed reads, chapter post list/create, comment
list/create, chirp list/create, /auth/me, and /ws upgrade, application readiness and hold. Stripe,
email/campus verification, media, messages, moderation, and every money path are
not in the route table at all — the harness has no code that can reach them.
All synthetic bodies carry a literal `LOADTEST` marker plus the writing uid, so
cleanup is one query per table.

## Prod prerequisites (all four, in order)

1. **Jose's explicit GO** recorded on the board (c226 card), lifting the park.
2. **Accounts**: N real Firebase accounts with `.edu`-verified campus + chapter
   membership on a throwaway campus/chapter, provisioned by Jose/manager (this
   repo's seed script structurally refuses non-local databases). The manifest
   the harness eats is `{campus_id, chapter_id, users: [{uid, id_token}]}`;
   id tokens expire hourly, so mint them at T-15, not the night before.
3. **A quiet hour** agreed with the manager: no deploy window open, no migration
   pending, alpha users asleep (02:00-05:00 ET has been the working definition).
4. **Two operators**: one runs the harness, one watches the dashboards below.
   The watcher owns the abort decision for everything the harness cannot see.

## Abort criteria for the prod run

Harness-enforced (in the config file, evaluated every 2s and once after the
selected legs finish). HTTP uses the configured rolling window; WS uses the
whole finite cohort of settled attempts. Both respect the written grace period
and minimum sample count:

| Criterion | Recommended | Why |
| --- | --- | --- |
| max_error_rate_pct | 2.0 | 5xx/transport errors; 2% sustained is a failing system, not noise |
| max_429_rate_pct | 1.0 | paced writes should see ~0; >1% means the model is wrong — stop and look |
| read_p95_ceiling_ms | 1500 | prod baseline is unknown (that is the point); 1.5s reads are already a bad app |
| write_p95_ceiling_ms | 2500 | writes carry commits; still generous |
| max_ws_failure_pct | 5.0 | failures / (failures + completed holds); pending/stopped/canceled attempts excluded |

Operator-enforced (the watcher aborts the run by telling the driver to Ctrl-C):

- **DB connection headroom floor (c248): abort if total Postgres connections
  exceed 80** (headroom under 20 of the 100 max). Watch through the proxy:
  `SELECT count(*) FROM pg_stat_activity;` every minute.
- **Cloud Run**: either service pinned at maxScale for over 2 minutes, or
  container restarts appearing. `gcloud run services describe chirp-api
  --region us-central1` / the metrics console.
- **Redis**: memory or CPU alarming on chirp-redis (the limiter and WS fan-out
  share it).
- **Anything at all from a real user report channel.**

## The instrument audits itself (c285) — read this line of the report first

B3 of the Sep 2 run aborted on p95s of 4-5 seconds that the SERVER never
produced: Cloud Run's own request_latencies stayed at 172-390ms through the
window while 176 unramped users saturated the driver Mac (proven offline: an
independent probe read 11.5ms p50 through the same server while the harness
recorded 437-664ms p95). Three controls help detect that problem; none proves the driver is unsaturated:

- **`ramp_in_seconds`** staggers virtual-user starts. Size it so connections
  open at a rate the driver absorbs (~10-20/s on the old Intel Mac).
- **`abort.grace_seconds`** holds every criterion until the ramp settles.
  Cover `ramp_in_seconds` plus a few seconds.
- **The reference probe** runs automatically: one request per second on its own
  connection, outside every cap. The report's `instrument` verdict compares the
  mix's read p95 against the probe's. The legacy `saturated` (>3x) verdict flags
  inconsistent latency; endpoint mix costs and driver contention can both
  contribute. `clean` means only ratio <=3, not proof that the driver is healthy.
  `no_probe` means no usable reference samples; distrust the run. Correlate
  process CPU/loop lag, network and server telemetry before attribution.

Two standing rules from the c285 post-mortem: **Cloud Run's request_latencies
are the quoted truth** for prod latency (harness numbers are client-experienced
from one machine, and say so when quoted); and **runs above ~150 users need a
cloud VM driver near us-central1** — the ramp and probe make the instrument
honest about saturating, they do not make one Intel core faster.

## Procedure

1. T-30: confirm prerequisites and capture baseline using the current paired
   [deployment configuration](../../DEPLOY-CONFIGURATION.md) and
   [authenticated verification](../../DEPLOY-VERIFICATION.md) procedures for
   both services. Record their required schema/revision/fixture evidence and
   database connection count with the run; a legacy URL-only check is incomplete.
2. T-15: mint tokens, build the manifest, dry-parse it:
   `python -m loadtest --config prod.yaml --manifest prod-manifest.json --users 1 --duration 5 --confirm-park-lifted`
   (a 1-user 5s smoke — this IS synthetic traffic, so it happens inside the
   window, not before it).
3. Phase A — WS storm only, half target: `--ws-only` with max_sockets 100
   against the chirp-ws URL. Confirms the c213 split actually carries sockets
   before HTTP load lands on chirp-api.
4. Phase B — HTTP mix ramp (`--http-only`): 50 users, then 100, then 200 (separate invocations,
   `--users N`, 10 minutes each). Between levels: watcher reads pg connections,
   instance counts, error logs. Any harness abort (exit code 2) ends the night —
   diagnose offline, do not re-run into a wounded system.
5. Phase C — combined, only if A and B were clean: use neither mode flag for
   HTTP at 200 users plus the configured WS cohort. Require the report's
   `coverage.http_ready_ws_overlap` to be `OBSERVED`, then inspect the count
   of actual HTTP responses and 2xx responses during ready/open socket life.
   Concurrent task creation alone is not combined-load evidence.
6. Aftermath, same night: save every report JSON to the card; run the cleanup
   queries (below); repeat the current paired configuration and authenticated
   verification procedures; post baseline versus after evidence on the board.

## Cleanup (prod, after any run)

Synthetic rows are identifiable by body marker and by author uid prefix agreed
at provisioning time. Via the Auth Proxy, in this order (comments before posts):

```sql
DELETE FROM post_comments WHERE body LIKE 'LOADTEST %';
DELETE FROM chirps WHERE body LIKE 'LOADTEST %';
DELETE FROM posts WHERE body LIKE 'LOADTEST %';
```

Then decide with Jose whether the test accounts stay (future runs) or go.
Ledger/dues tables are untouched by construction — the harness cannot reach
them.

## Local proving (what CI of this harness means)

The end-to-end proof this PR ships ran entirely against localhost:

```bash
# 1. local Postgres 14 up; own database so no dev data is touched
createdb chirp_load   # or: psql -c 'CREATE DATABASE chirp_load'
cd backend
DATABASE_URL='postgresql+asyncpg://chirp:chirp@localhost:5432/chirp_load' .venv/bin/alembic upgrade head
DATABASE_URL='postgresql+asyncpg://chirp:chirp@localhost:5432/chirp_load' .venv/bin/python scripts/seed_loadtest.py --users 60 --manifest-out /tmp/loadtest-manifest.json

# 2. serve that database on a port nothing else owns (8081 is Metro's; 8000 may be a dev API)
DATABASE_URL='postgresql+asyncpg://chirp:chirp@localhost:5432/chirp_load' AUTH_MODE=emulated \
  .venv/bin/uvicorn app.main:app --port 8010 &

# 3. run the harness
.venv/bin/python -m loadtest --config loadtest/example-config.yaml --manifest /tmp/loadtest-manifest.json --out /tmp/loadtest-report.json
```

A current gateway requires Redis subscription acknowledgement before sending
`{"type":"ready"}`. Without Redis, upgrade followed by 4503 is a **readiness
failure**, never a successful hold. A configured grace/minimum may prevent an
abort for a tiny cohort; inspect the terminal outcomes even when no criterion
trips. For a readiness run, supply an owned loopback Redis and a disposable test
DB; do not reuse another developer's service/port or a production endpoint.

The focused executable contract tests need only the locked Python dependencies
and ephemeral loopback listeners, not GCP, Redis or PostgreSQL:

```sh
cd backend
.venv/bin/python -m pytest -q tests/test_c363_mixed_harness.py tests/test_c226_load_harness.py tests/test_c285_self_audit.py
```

They execute the installed modern `websockets` client against local peers for
exact ACKs, early closes (including 1000), absent ACKs, stalled upgrade/close,
full holds, cancellation, and real HTTP responses while ready sockets remain
open. Controlled supervisor tests cover concurrent default selection, HTTP
expiry, abort during ramp, late failures and unexpected child exceptions.
These are bounded correctness fixtures, not a load benchmark. The historical
`results-2026-09-02` artifacts retain their original sequential/handshake-only
semantics; they were not rewritten or upgraded into new evidence.

## Current instrument contract (c363 first slice)

- Default starts HTTP and WS together. `--http-only` and `--ws-only` are mutually
  exclusive. HTTP retains its warmup, then runs its configured duration and
  cancels only its own users/probe. WS attempts start no faster than
  `connects_per_second` and each holds for `hold_seconds` **after readiness**.
  The run waits for both selected legs; a shorter HTTP duration does not silently
  shorten a socket hold. Warmup may outlast a short WS cohort, so actual overlap
  must be read from the report, not inferred from these settings.
- The existing global HTTP token bucket (initial burst `max(2, max_rps)`),
  request semaphore and per-user write buckets are retained. The reference
  probe adds up to one request per second on its separate connection; it is
  outside those caps and is not evidence of mixed traffic. Proxy environment
  variables are disabled in both HTTP clients and the WS client so the selected
  target is contacted directly; TLS verification remains enabled for HTTPS/WSS.
  Neither client follows HTTP redirects. The pinned WS client's tested
  `process_redirect` override preserves its original handshake exception, so a
  local redirect cannot escape the target guard or forward the auth subprotocol.
  Explicitly approved direct non-local targets retain the existing approval gate.
- An upgrade is `connected`, not `ready`. The first application frame must be
  text JSON containing exactly one `type: ready` member, without duplicate or
  extra keys, within 15 seconds after upgrade. A different first frame fails
  explicitly. The client uses the modern asyncio API, a 10-second upgrade
  timeout, 128 KiB incoming message limit, receive queue high-water mark 4 and
  no compression. These are client-side controls, not a whole RSS or server
  inbound-buffer bound. See the primary
  [websockets client API](https://websockets.readthedocs.io/en/stable/reference/asyncio/client.html).
- A socket stays ready for overlap accounting only while its actual client
  state is OPEN. An HTTP mix response must complete during that interval to
  increment the mixed observation; warmup, reference probes, transport errors,
  upgrades alone, and already-parsed closes do not qualify. The report also
  separates 2xx responses. This is overlapping client activity, not evidence
  of receipt delivery or all instances experiencing the same load.
- Terminal outcomes distinguish completed hold, upgrade error/timeout, missing
  or invalid readiness, close before readiness/during hold, cleanup timeout,
  internal error, coordinated stop and cancellation. **An early 1000 close is a
  failed hold**, even though its protocol code says normal. Close codes are
  retained separately, even if transport cleanup stalls; 1006 denotes no observed
  peer close code. Pending
  attempts are shown separately and never inflate failure during ramp.
- WS failure percentage uses only failures plus completed holds. With no such
  settled samples it is JSON `null`, not measured zero; abort evaluation waits
  for its configured minimum. Stopped/canceled attempts remain visible but are
  excluded from that percentage. A criteria abort stops both legs, including
  queued requests, active sockets and a sleeping connection ramp. The final
  check catches failures between the last periodic tick and completion.
- Unexpected worker exceptions propagate through owned task groups and produce
  `internal_error` plus exit 2, with no exception body in the report. Interrupted
  CLI runs produce a partial report and exit 130; stopped/canceled/pending WS
  attempts cannot yield a completed report. Criteria aborts return 2. A run
  completing below configured thresholds is not a production signoff.
- Receive waits are canceled at their readiness/hold deadline; the pinned modern
  client's `recv()` permits cancellation. Cleanup gives `close()` a 1-second
  caller budget as well as its native close timeout, then aborts a still-open
  transport. Local unresponsive-peer fixtures verify this boundary. Event-loop
  scheduling and physical OS cleanup are separate; there is no hard whole-run
  wall-clock or memory guarantee. Intended WS elapsed time includes paced ramp,
  upgrade, readiness, full hold and cleanup, and can exceed HTTP duration.

**Receipt delivery is NOT_PROVEN.** This slice drains post-ready frames but has
no publisher, message identity, sent/received reconciliation, or durable receipt
contract. Manually publishing a frame would not turn its output into delivery
percentiles. Use the separate c354 gateway proofs for their stated local scope;
representative mixed receipts, reconnect/recovery, fixed deployment/pool budgets
and staging/production capacity remain c363 follow-up acceptance. No production
load or cloud modification was executed for this correction.
