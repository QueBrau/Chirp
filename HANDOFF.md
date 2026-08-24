# HANDOFF — where everything actually is

_Last updated: Aug 24 2026 (c63 Redis/VPC deployment and release-hygiene audit)._

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

What this file keeps is the part the board cannot hold: how to set the thing up, the
rules that are load-bearing, and the lessons that cost us something to learn.

> **If you are about to write a number into this file, stop.** Either it belongs on the
> board, or it belongs in a command someone can run. The one exception is the
> verified-state snapshot below, which is explicitly dated and explicitly sourced.

## State snapshot — Aug 23 2026, with its sourcing

Sourcing is split on purpose. "Verified" means someone ran the check this session.
"Board-sourced" means it is the board's claim and nobody has re-run it against the live
system. Do not promote a board-sourced line to verified without re-checking it.

| Thing | State | How we know |
| --- | --- | --- |
| API revision | `chirp-api-00027-hwf`, serving 100% | **court-verified Aug 23** against Cloud Run |
| Open PRs | none at session start | **court-verified Aug 23** |
| Merged PRs | through **#78** | **verified Aug 23** via `gh pr list --state merged` |
| Alembic head (repo) | **0013**, single head | **verified Aug 23** via `alembic heads` |
| Prod DB `alembic_version` | **0013** | **board-sourced** (c71, applied by Jose Aug 22) — **re-verify at the next deploy** |
| Local backend suite | 357 passed, 3 skipped | **verified Aug 23**, local PG14, run from `backend/` |
| Website | https://chirps-prod.web.app | live |
| Stripe | test mode, armed | **no money has ever moved** |
| Redis | not provisioned as of this Aug 23 snapshot | historical only; superseded by the Aug 24 verification below |

## Latest verification — Aug 24 2026

| Thing | State | How we know |
| --- | --- | --- |
| API revision | `chirp-api-00030-m97`, serving 100% | **verified Aug 24** against Cloud Run after the merge-safe Redis/VPC service update; serving image digest is unchanged from `00029-s4d` |
| Prod DB `alembic_version` | **0013** | **verified Aug 24** through the Cloud SQL Auth Proxy |
| Media signing secret | configured and bound to the Cloud Run runtime account | **verified Aug 24** by Secret Manager and revision inspection; the bucket remains public until a physical-device capability-route render passes and Jose explicitly approves the privacy flip |
| Deployed route checks | `/_health` 200, `/auth/me` 401, lineage/attendance auth routes 401, malformed `/media/{token}` 403, `/openapi.json` 404 | **verified Aug 24** against the live revision |
| Mobile/release static checks | TypeScript, API contract verifier (82 client calls / 92 backend routes), Expo public config, Python syntax, and c156's two-call dashboard wiring pass | **delegated GPT-5.6 QA audit Aug 24**; fresh backend pytest is blocked by a local Python 3.11/pytest startup segfault, not a test assertion |
| c153 reconciliation operations | Dedicated runner SA, read-only DB secret, restricted bucket roles, and dry-run Cloud Run Job are live; execution `chirp-media-reconcile-p679k` reported `scanned=1 referenced=1 eligible=0 deleted=0` | **verified Aug 24**; no media deletion was attempted and the runtime `tmp/`-only IAM was preserved |
| c145 log cleanup | The two affected Cloud Logging streams were purged; unrelated logs remain | **verified Aug 24**; 6,328 request and 17,275 stderr entries removed, exact post-delete cutoff checks returned zero |
| Redis infrastructure | `chirp-redis` Basic 1 GiB/Redis 7.0 and `chirp-vpc` on `10.8.0.0/28`, `e2-micro` min 2/max 3 are `READY`; `REDIS_URL` v1 and private-ranges-only connector egress are bound to Cloud Run | **verified Aug 24** from live resource descriptions and revision inspection; two real Firebase users completed an HTTP 201 message to WebSocket 101 event round-trip, with authenticated history read-back |
| Repository / GitHub | `origin/main` was `53d34f6`; the shared root `main` was at `75b8d2a`, two board-only commits behind, while this hygiene branch was cut directly from `origin/main`; no open PRs; Actions run **493** passed on `53d34f6` | **verified Aug 24** via fetch, GitHub's public API, and local ref inspection; treat this as a dated snapshot and run the commands at the top of this file for live state |

The snapshot is evidence, not a cache to trust forever. Re-run the live-source commands
at the top before any release. In particular, `0013` was verified on prod before
`00029-s4d`; c63 changed only service networking/secrets and did not run a migration or
change the image. A future code deploy still re-checks the database first.

## Migrations — read this before you write one

**The head is not the highest number on disk, and right now it is not even close.**
The chain is:

```
0011 -> 0014 -> 0017 -> 0015 -> 0016 -> 0013
```

`0013` is the head. Alembic walks `down_revision`, not filenames, so this is correct and
must not be "fixed" by renumbering. `0012` was claimed for c69 and released unused, so it
is a hole in the sequence, not a missing file.

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
authenticated route.

## Known-bad things that are shipped

These are live in front of users right now. Each has a card; the card is authoritative.

- **Apple and Google sign-in are visual stubs** (c89) that skip authentication entirely.
  Apple guideline 4.8 makes Sign in with Apple mandatory once Google ships.
- **`/terms` and `/privacy` are not lawyer-reviewed** (c75), scoped to NC and UNCG on
  purpose. The liability sections are still placeholders.
- **Transactional email cannot reach arbitrary recipients** (c87). The app-to-Resend chain
  works, but without a verified sending domain Resend refuses anyone outside the account.
- **The media bucket is still public by design** (c140). Signed capability URLs are live,
  but Public Access Prevention and `allUsers` removal remain gated on physical-device
  render proof plus Jose's explicit approval. Redis/c63 did not close that gate.

## Alpha

Alpha is Q's old chapter plus a couple more orgs — real students, real phones, small
enough to phone someone when it breaks. It is defined as a fixed list of named gates, not
a vibe: `board.html` carries the shipped foundations and the gate list, and the readiness
bar recomputes from the cards on every render. Moving a card moves the bar.

Read the bar off the board. Do not copy the number into this file.
