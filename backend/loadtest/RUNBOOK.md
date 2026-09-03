# c226 load-test runbook

**THE PARK IS STILL ON.** Jose parked running the load test (board c226, Aug 30).
Nothing in this document is a licence to send one synthetic request at prod. The
harness enforces the park in code: a non-local target refuses to run unless the
config carries an `approval` block naming who approved and when, AND the operator
re-types `--confirm-park-lifted` at the command line, AND auth_mode is `firebase`
(emulated headers are ignored outside local anyway). This runbook exists so that
when Jose says GO, the run is a same-day action instead of a design session.

## What the test is for

Every capacity number we hold is computed, never measured (ARCHITECTURE.html):

| Resource | Configured | Computed ceiling |
| --- | --- | --- |
| chirp-api | maxScale 4 x concurrency 80 | 320 in-flight requests |
| chirp-ws | maxScale 2 x concurrency 200 | 400 concurrent sockets |
| Postgres | max_connections 100 | demand 28 (c248), 72 spare |

The run measures what the card asks for: p95 at ~200 concurrent users,
pool-checkout 503 rate, WS capacity per ws instance, and cold-start behavior
(visible in the report's 10s timeline during ramp).

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
list/create, chirp list/create, /auth/me, and the /ws handshake+hold. Stripe,
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

Harness-enforced (in the config file, evaluated every 2s over a 30s window):

| Criterion | Recommended | Why |
| --- | --- | --- |
| max_error_rate_pct | 2.0 | 5xx/transport errors; 2% sustained is a failing system, not noise |
| max_429_rate_pct | 1.0 | paced writes should see ~0; >1% means the model is wrong — stop and look |
| read_p95_ceiling_ms | 1500 | prod baseline is unknown (that is the point); 1.5s reads are already a bad app |
| write_p95_ceiling_ms | 2500 | writes carry commits; still generous |
| max_ws_failure_pct | 5.0 | handshake failures only; post-accept closes report by code |

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
recorded 437-664ms p95). Three changes keep that from recurring:

- **`ramp_in_seconds`** staggers virtual-user starts. Size it so connections
  open at a rate the driver absorbs (~10-20/s on the old Intel Mac).
- **`abort.grace_seconds`** holds every criterion until the ramp settles.
  Cover `ramp_in_seconds` plus a few seconds.
- **The reference probe** runs automatically: one request per second on its own
  connection, outside every cap. The report's `instrument` verdict compares the
  mix's read p95 against the probe's. **`saturated` (>3x) means the numbers
  describe the driver, not the server — stop drawing conclusions from them.**
  `no_probe` means the audit itself failed; distrust the run.

Two standing rules from the c285 post-mortem: **Cloud Run's request_latencies
are the quoted truth** for prod latency (harness numbers are client-experienced
from one machine, and say so when quoted); and **runs above ~150 users need a
cloud VM driver near us-central1** — the ramp and probe make the instrument
honest about saturating, they do not make one Intel core faster.

## Procedure

1. T-30: confirm prerequisites; capture baseline: deploy-verify 4/4 against BOTH
   services (`scripts/deploy-verify --base-url <url>`), pg connection count,
   current revision names (report them with the results).
2. T-15: mint tokens, build the manifest, dry-parse it:
   `python -m loadtest --config prod.yaml --manifest prod-manifest.json --users 1 --duration 5 --confirm-park-lifted`
   (a 1-user 5s smoke — this IS synthetic traffic, so it happens inside the
   window, not before it).
3. Phase A — WS storm only, half target: `--ws-only` with max_sockets 100
   against the chirp-ws URL. Confirms the c213 split actually carries sockets
   before HTTP load lands on chirp-api.
4. Phase B — HTTP mix ramp: 50 users, then 100, then 200 (separate invocations,
   `--users N`, 10 minutes each). Between levels: watcher reads pg connections,
   instance counts, error logs. Any harness abort (exit code 2) ends the night —
   diagnose offline, do not re-run into a wounded system.
5. Phase C — combined, only if A and B were clean: HTTP at 200 users plus WS
   storm to 300 sockets.
6. Aftermath, same night: save every report JSON to the card; run the cleanup
   queries (below); deploy-verify 4/4 both services again; post baseline vs
   after on the board.

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

Expected locally: HTTP mix all 2xx, zero 429s at paced rates; every WS socket
connects then closes 4503 (`realtime_unavailable`) because Jose's Mac has no
Redis — the handshake, auth, and DB-lookup path is exercised; the post-accept
close is the documented no-Redis behavior from app/ws/gateway.py, not a failure.
The abort machinery is proven live by a second run with an absurd ceiling
(read_p95_ceiling_ms: 1) that must exit 2 with `RESULT: ABORTED`.

**What local proving does NOT cover: delivery.** With no local Redis the
subscribe-and-forward half of the gateway has never run under this harness.
Phase A of the prod procedure is the first time the WS leg meets a live Redis —
treat its first minutes as an experiment, not a formality. To close the gap
before prod: on Q's Docker Mac, run the same recipe plus
`docker run -d --name chirp-loadtest-redis -p 6379:6379 redis:7` and
`REDIS_URL=redis://localhost:6379/0` on the uvicorn line, hold sockets open
(`hold_seconds: 30`), and `docker exec chirp-loadtest-redis redis-cli PUBLISH
user:<a-held-user-uuid> '{"type":"probe"}'` — a delivered frame proves the
forward loop under storm.
