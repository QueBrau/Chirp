# HANDOFF — where everything actually is

_Last updated: Sep 1 2026 (ops-doc sweep, board card c262 — two more deploy windows,
migration 0029, and ARCHITECTURE.html landed since the Aug 28 launch-hardening batch
below)._

**This file deliberately contains almost no numbers.** The previous version rotted
within a week because it hardcoded the prod revision, the open-PR list, the migration
head and the alpha percentage in prose — every one of which changes several times a day
now that multiple sessions work the repo at once. Each of those has a live source, and
this file points at the source instead of copying it.

| Question | Live source — go here, not to this file |
| --- | --- |
| What is being worked on, by whom, what is blocked | `board.html`, open it in a browser |
| Which PRs are open right now | `gh pr list` |
| What the migration head is | `cd backend && alembic heads` |
| What prod is serving | `gcloud run services describe chirp-api --region us-central1` |
| Alpha readiness | the bar at the top of `board.html`, recomputed from the cards on every render |
| Credentials, runbooks, live fixture ids | `INFRA-PRIVATE.html` at the repo root (gitignored, never commit it) |
| System architecture — deployment topology, domain model, dues and message sequences | `ARCHITECTURE.html` at the repo root (UML reference, drawn from live prod) |

What this file keeps is the part the board cannot hold: how to set the thing up, the
rules that are load-bearing, and the lessons that cost us something to learn.

> **If you are about to write a number into this file, stop.** Either it belongs on the
> board, or it belongs in a command someone can run. The one exception is the
> verified-state snapshot below, which is explicitly dated and explicitly sourced.

## State snapshot — Aug 28 2026, updated in place Sep 1 2026, with its sourcing

Sourcing is split on purpose. "Verified" means someone ran the check this session.
"Board-sourced" means it is the board's claim and nobody has re-run it against the live
system. Do not promote a board-sourced line to verified without re-checking it. The
Aug 23/24 snapshots that used to sit here were removed rather than stacked on: three
dated tables in one file is how a reader ends up trusting the wrong row — this table
gets edited in place instead, row by row, which is why some rows below still say
Aug 28 and others say Sep 1.

The Sep 1 pass (board card c262, an ops-doc sweep) is itself a docs check against the
board, not a fresh live-system check — every row it touched is marked board-sourced,
not verified, even where the Aug 28 version of that row had been verified directly.
Re-verify before promoting any of them.

| Thing | State | How we know |
| --- | --- | --- |
| Serving revisions | **two services**: `chirp-api-00043-7ts` and `chirp-ws-00008`, deploy-verify reported 4/4 on both | **board-sourced, not verified this pass** — c269, deploy window #3 (Aug 31, deploy-only, no migration); confirm live with `gcloud run services describe` |
| Why two services | open WebSockets were consuming chirp-api's HTTP concurrency budget, capping concurrently-online users near 320 (c209/c213) | see `INFRA-PRIVATE.html#chirpws`; **every image update must deploy BOTH** |
| Prod DB `alembic_version` | **0029** (adds the `user_blocks` self-block CHECK constraint; migration 0028→0029), and both c230 triggers still present per the same window | **board-sourced, not verified this pass** — c260/c237, deploy window #2 (Aug 30, migrate-then-redeploy, closed clean); confirm live with `alembic current` through the Auth Proxy |
| Alembic head (repo) | matches prod as of this line (0029) — **but run `alembic heads` yourself**, see the migrations section | **board-sourced, not verified this pass** — c260/c237 |
| DB tier | `db-custom-1-3840` (dedicated core, ~100 connections), ZONAL | **verified Aug 28**; regional HA is a separate priced call Jose has not taken |
| Backups / restore | daily, 14 retained, 7-day PITR, deletion protection on — and the restore is **rehearsed**, not just configured (~15 min to data, ~25 min to full service) | **court-rehearsed Aug 27**, runbook at `INFRA-PRIVATE.html#restore` |
| Local backend suite | growing every day — **do not trust a number here**, run it yourself: `cd backend && pytest -q` (full-suite runs go through `scripts/with-suite-lock`) | **board-sourced only, NOT verified** — last count seen on the board was 778 passed / 5 skipped / 0 failed (c265, Aug 31); it was already stale by the time this line was written |
| Website | https://chirps-prod.web.app | live |
| Stripe | test mode, armed | **NO MONEY HAS EVER MOVED — and no test-mode payment has ever cleared end to end (board c11). That is the launch gate.** |
| Analytics | emitter live; Cloud Logging sink `chirp-analytics-bq` into BigQuery dataset `chirp_analytics` | **verified Aug 28**; chirp authorship is provably never linked (c227 rule, test-pinned) |

## Durable infrastructure facts (not revision-dated)

These do not change every deploy, so they are kept here rather than in the snapshot
above. Anything with a revision number or a migration number belongs in the snapshot or,
better, in a command you run.

- **Redis / VPC**: `chirp-redis` Basic 1 GiB Redis 7.0 and connector `chirp-vpc` on
  `10.8.0.0/28`, `e2-micro`, min 2 / max 3, both `READY` and bound to Cloud Run. Redis is
  fan-out only, never storage, and the app degrades gracefully when it is down. A real
  Firebase-authenticated HTTP-201-to-WebSocket-101 round-trip with authenticated history
  read-back was proven live (c63).
- **Media**: the bucket is private (public-access prevention enforced, `allUsers` removed)
  and reads go through app-signed HMAC capability URLs; a direct object URL 403s (c140/c155).
  Signing is done off the event loop since c211 — do not reintroduce a synchronous
  `signBlob` call on a request path.
- **c153 reconciliation**: dedicated runner SA, read-only DB credential, delete permission
  conditioned to `posts/` only, stored job is dry-run by default. Never add a schedule or
  a `--delete` default; the runtime SA must keep its `tmp/`-only IAM.
- **c145**: the log streams that briefly carried WS tokens were purged in full; the scrub
  that prevents a recurrence lives in `app/core/log_scrub.py`.
- **Deployed route shapes** worth knowing when you verify a release: `/_health` 200,
  auth-gated routes 401 unauthenticated, malformed `/media/{token}` 403, `/openapi.json`
  404 on any non-local env. `/healthz` is intercepted by Google's frontend — never use it.

## Migrations — read this before you write one

**The head is not the highest number on disk, and it never has been. Do not read a
head number out of this file — run `cd backend && .venv/bin/alembic heads` and parent on
what it prints.** Any number written here is stale the next time anyone merges; this
section deliberately no longer names one. Alembic walks `down_revision`, not filenames,
so an out-of-order chain is correct and must never be "fixed" by renumbering. `0012` was
claimed for c69 and released unused, so it is a hole in the sequence, not a missing file.

**This trap has now cost four separate branches**, which is why it leads this section:
0024 (c198), 0018 (c162), and two more all wrote `down_revision` pointing at whatever the
head was on the day the file was WRITTEN, then sat on a branch while main moved. Each one
would have produced two heads the moment it merged, and each was caught only because
someone re-ran `alembic heads` at merge time. **Re-point at the current head immediately
before you merge, not when you start** — that is the rule the merges above actually
enforce, and CI's single-head check cannot see the collision while the other migration is
still unmerged.

- **Run `alembic heads` and parent on what it prints.** Claiming a number on the board
  prevents duplicate revision ids; it does not stop two people parenting different numbers
  on the same head. That produced a two-heads failure on Aug 16 and is the single most
  expensive migration mistake this repo has made.
- **Claim the next number on the board before writing the file.** `scripts/board-check`
  prints the next genuinely free id and fails if one is in use without a card.
- **When two migrations collide, the side that has not merged re-points at the current
  head.** Renumbering a revision id breaks anything that already recorded it.
- A duplicate revision id that is already applied to prod is **skipped silently** — the
  tables never get created and nothing errors. This is why the rule is not optional.

## Things that will bite you

- **Migrate FIRST, then deploy.** Aug 16: the migration failed, the deploy succeeded, and
  prod briefly served code reading `users.suspended_at` against a schema without it. It
  looked fine, because health checks and unauthenticated requests never reach
  `get_current_user`. **"Health is 200" is not evidence a deploy is healthy when the
  broken path is behind auth.** Pick a route that requires a real signed-in user.
- **`/healthz` is unreachable** — Google's frontend answers it before your container does.
  The route is `/_health`. To check liveness from outside, hit a real route and expect a
  401.
- **The prod `DATABASE_URL` secret is in Cloud Run's UNIX-SOCKET form**
  (`...@/chirp?host=/cloudsql/...`). To migrate from a laptop you must *decompose* it and
  rebuild against `127.0.0.1:5433` — do not regex the host out. A substitution that
  silently matches nothing leaves asyncpg trying a socket that does not exist. Recipe is
  in c93's card detail.
- **`cloud-sql-proxy` is not on PATH.** It lives at `~/cloud-sql-proxy` and must use
  **5433**, because local Postgres 14 owns 5432.
- **Jose's zsh has `interactive_comments` off.** A `#` in a pasted command runs as a
  command, and a trailing comment is fed to the program as an argument. Hand over commands
  with no inline comments.
- **The firebase CLI is logged in as `madden25boss1@gmail.com`**, which cannot see
  `chirps-prod`. Website deploys go through the gcloud ADC; runbook in `web/README.md`.
- **Mobile CI is `tsc` only.** There is no mobile test harness at all, so a green mobile
  check means "it compiles" and never "it works". Anything user-facing needs a real
  render.
- **`npx tsc` in a worktree with no `node_modules` does not run the TypeScript compiler.**
  It fetches the npm placeholder package called `tsc` and prints "This is not the tsc
  command you are looking for". Symlink `node_modules` from the main checkout before any
  mobile check in a worktree (`.gitignore` already documents the symlink and why it must
  never be committed). **Cite `tsc --version` in your evidence** — a type-check whose
  output does not include a version number did not type-check anything.
  Measured, because the distinction changes what you watch for: the placeholder exits
  **1**, not 0, on npm 10.8.2 / node 20.20.0. So this is a loud failure, not a silent
  false green — a `&&` chain or a CI step stops on it. The way it actually costs you time
  is a human skimming the red box, reading it as an environment hiccup, and reporting
  "tsc ran" — not a gate that wrongly passes.
- **A green signal can answer a question nobody asked.** A CI gate is a property of the
  RUN, not of the PR: it only protects a merge if the check is newer than both the gate's
  existence and the base's last move. When branches move fast, re-check the merge result
  rather than trusting a green tick.
- **`scripts/deploy-verify` defaults to `localhost:8000` and says so, but that has still
  fooled someone.** Run it bare after a prod deploy and you get a TOTAL red — `0 passed,
  4 failed`, four `000` status codes — because nothing at `localhost:8000` answered, not
  because prod is down (c250). `000` across every probe means **wrong target**, not a
  broken deploy: check the `target:` line the script prints as its second line before you
  re-run or start diagnosing. Don't confuse this with a cold-start flake, which is a
  PARTIAL red against a real URL (e.g. 3/1) — they look nothing alike. Correct invocation
  after a prod deploy is `scripts/deploy-verify --base-url <service URL>`; see `DEPLOY.md`
  for where that URL lives.

## Multi-session rules (these are enforced, not advisory)

Several Claude sessions work this repo simultaneously. The full rules live in
`CLAUDE.md`; these are the two that have caused the most damage.

- **One worktree per session.** The shared tree at the repo root stays on `main`. Never
  `git checkout` a branch there — a peer can switch it between your commands, and your
  "board commit to main" then silently lands on their branch. Read other branches with
  `git show <ref>:<path>`; take your own `git worktree add` for any code work.
- **Write the card before you cut the branch.** Card ids are a shared resource and have
  collided three times. `scripts/board-check` prints the next genuinely free id.
- **Run `scripts/board-check` before committing `board.html`, and
  `scripts/board-check --pushed` after pushing.** Four board changes vanished silently in
  one day and a JSON-parse check caught none of them.
- Edit the embedded `<script id="board-data">` JSON by parsing and re-serializing it
  (`json.dumps(..., indent=2, ensure_ascii=True)` round-trips byte-identically, so the
  diff stays minimal). Never regex or find/replace the HTML.

## Environment

Two very different Macs, both written up in `CLAUDE.md`.

- **Jose**: Intel Mac, macOS 12, **no Docker**. Local Postgres 14 via
  `pg_ctl -D /usr/local/var/postgresql@14 start`. Backend venv at `backend/.venv`. No
  `redis-server`, which is why the Redis fan-out tests skip locally.
- **Q**: Apple Silicon, Docker available, no native Postgres — backend tests run against a
  `postgres:16` container on host port **5434**, so runs there match prod's PG16.

**Run the backend suite from inside `backend/`** or every async test errors confusingly.
Tests no longer need any database setup: since c106 each run creates and drops its own
`chirp_test_p<pid>` database, so concurrent sessions cannot tear down each other's tables.
The old "give yourself your own DB" recipe is gone — just run `pytest`.

## Deploys

Backend changes need a Cloud Run redeploy to go live. The command and credentials are in
`INFRA-PRIVATE.html`; `DEPLOY.md` carries the procedure. Deploys and prod DB migrations
are **not** a worker session's call — they run through Jose and the manager session.

Order is always **migrate, then deploy**, and the post-deploy check must exercise a real
authenticated route. Several coordinated deploy windows have run since this file's Aug 28
snapshot — see the board for the full history of what shipped in each; the snapshot table
above reflects only the most recent one.

## Known-bad things that are shipped

These are live in front of users right now. Each has a card; the card is authoritative.

- **Apple and Google sign-in buttons are honest stubs, not a bypass** (c89, corrected
  here — this line used to say they "skip authentication entirely," which was true until
  c89 landed and is no longer): the buttons are disabled with an honest caption and create
  no session, proven on the native sim. What is still missing is the real native provider
  wiring, tracked as c169 (backlog). Apple guideline 4.8 makes Sign in with Apple
  mandatory once Google ships.
- **`/terms` and `/privacy` are not lawyer-reviewed** (c75), scoped to NC and UNCG on
  purpose. The liability sections are still placeholders.
- **No test-mode payment has ever cleared end to end** (c11) — card or ACH. The money code
  is heavily reviewed and hardened, but that is not the same claim as "a payment worked".
  **This is the launch gate**: it must pass at the device window before real members are
  invited to pay.
- **`.edu` verification: the last hop is unproven** (c134/c71). Sending IS proven — a real
  202 from `hello@josedev.app` to an arbitrary `.edu` address, Aug 25. What has never
  happened is a mailbox the team controls RECEIVING one. Q is a real student; one send to
  his own `.edu` inbox closes it, and no school mailbox for Jose is needed.
- **Mobile realtime still points at chirp-api's own `/ws`** (c213). The dedicated
  `chirp-ws` service is live and the client supports `EXPO_PUBLIC_WS_URL`, but until that
  var is set in the build config and one authenticated round-trip is proven against it,
  the concurrency split is not actually buying anything.

Corrected here rather than left to rot, because both were stated as current in this file
until Aug 28 and both are now false: the **media bucket is private** (public-access
prevention enforced, `allUsers` removed, capability URLs verified, direct object access
403s — c140/c155 flipped Aug 24), and **email is not domain-blocked** (`josedev.app` is
verified in Resend, c134).

## Alpha

Alpha is Q's old chapter plus a couple more orgs — real students, real phones, small
enough to phone someone when it breaks. It is defined as a fixed list of named gates, not
a vibe: `board.html` carries the shipped foundations and the gate list, and the readiness
bar recomputes from the cards on every render. Moving a card moves the bar.

Read the bar off the board. Do not copy the number into this file.
