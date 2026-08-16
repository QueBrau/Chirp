# HANDOFF — where everything actually is

_Last updated: Aug 16 2026, early morning. **Prod is current and verified.** Migration
0011 is applied, revision `chirp-api-00013-rbg` is serving it, and a real signed-in
request was confirmed returning 200. Alpha readiness is **50%** — 10 of 20 named gates._

**board.html is the source of truth for tasks** — 94 cards, 58 decisions. It now has an
**alpha readiness bar** that recomputes from the cards on every render, and cards are
**headlines with the detail behind a click** (a right-hand drawer). Open a card before
acting on it; the headline is deliberately short and the reasoning lives in the drawer.

This file only answers what the board can't: which copy of Chirp you are looking at, and
what will bite you.

## What is live right now

| Thing | Where | State |
| --- | --- | --- |
| API | `chirp-api-593616178468.us-central1.run.app`, rev **chirp-api-00013-rbg** | Current with `main` |
| Prod DB | Cloud SQL `chirp-db` | `alembic_version` **0011** |
| Website | **https://chirps-prod.web.app** | Live, phone-tuned, real phone menu |
| Stripe | test mode, acct `acct_1U4pwbFjVGWnUErJ` | Armed. **No money has moved** |
| Redis | — | **Never provisioned** on prod (deliberate, c61) |
| Tests | 168 pass, 3 skip, 0 fail locally | Skips are the Redis fan-out tests (c92) |

## The one thing blocking the sprint goal

**c39 — Q rebuilds the EAS dev build.** Three native modules landed after the current
build was cut, so the dues PaymentSheet cannot run on a device. It is alpha gate one and
step four of the payment chain. Nothing on Jose's list moves it.

Then **c11** — the test-mode dues payment on card AND ACH, including the cross-rail retry
that must return 409.

## Jose's queue (the "Next" column, owner J)

Ordered by what I'd actually do first:

1. **c94 — sign-in authenticates but does not navigate.** Signing in with an *existing*
   account returns `GET /auth/me` 200 and then sits on `/sign-in`. Reload and you land on
   `/feed` with the session intact. Sign-*up* is fine; it walks itself through. This is a
   **second, independent cause** of the bounce that CORS (c64) was masking — fixing the
   first cause of a compound bug is why it looked closed. Start at
   `app/(auth)/sign-in.tsx`: the sign-up path calls `applyBootstrap()` then `proceed()`,
   and the sign-in submit has no equivalent post-success navigation. **Verify on a real
   device** — headless saw it once.
2. **c93 — DEPLOY.md will wipe the CORS fix.** Line 83 uses `--set-env-vars`, which
   *replaces* the whole env block. Running it as written resets `CORS_ORIGINS` to a
   placeholder and re-breaks phone login, failing in the browser rather than at deploy
   time. Small doc fix, high blast radius.
3. **c58 — live browser QA.** Partly done Aug 16: full signup → bootstrap → feed verified
   against real prod on a phone viewport. The rest of the surface is still unproven.
4. **c66** — the runbook's DB password is literally the string `REDACTED`.
5. **c74** — `chirp.shared@gmail.com` is published on the live site *and* owns GCP,
   Firebase and Stripe. Wants a forwarding alias; coupled to c73's domain purchase.
6. **c73** — marketing site to `about.<domain>`. Blocked on buying a domain.
7. **c87 → c86 → c88** — transactional email, then `.edu` verification, then the gate.
   Strictly in that order. **Hold these until after alpha**: the two-tier decision means
   chapter membership grants org content with no email at all, so alpha runs without them.

Also open and unowned: **c84** (a chapter-less author cannot delete their own post) and
**c91** (no endpoint to resolve a report).

## Things that will bite you

- **Migrate FIRST, then deploy.** This bit us on Aug 16: the migration failed, the deploy
  succeeded, and prod briefly served code reading `users.suspended_at` against a schema
  without it. It looked fine — health checks and unauthenticated requests never reach
  `get_current_user`, so nothing 500s until a real user signs in. **"Health is 200" is not
  evidence a deploy is healthy when the broken path is behind auth.**
- **The prod `DATABASE_URL` secret is in Cloud Run's UNIX-SOCKET form**
  (`...@/chirp?host=/cloudsql/...`). To migrate from a laptop you must *decompose* it and
  rebuild with `127.0.0.1:5433`, not regex the host out — a substitution that silently
  matches nothing leaves asyncpg trying a socket that does not exist. Working recipe is in
  c93's card detail.
- **`cloud-sql-proxy` is not on PATH.** It lives at `~/cloud-sql-proxy`, and it must use
  **5433** because local Postgres 14 owns 5432.
- **Jose's zsh has `interactive_comments` off.** A `#` in a pasted command runs as a
  command, and a trailing comment gets fed to the program as an argument. Hand over
  commands with no inline comments.
- **`/healthz` is unreachable** — Google's frontend answers it. The route is `/_health`.
- **The firebase CLI is logged in as `madden25boss1@gmail.com`**, which cannot see
  `chirps-prod`. Website deploys go through the gcloud ADC; runbook in `web/README.md`.
- **Card ids and migration numbers are shared resources.** Take the next one from
  *origin's current* board, not the copy you started editing. Taken: 0011 (c76), 0013
  (c71, on Q's branch). **0012 was claimed for c69 and released unused.** Next free: 0014.
- **A multiple-heads hazard is pending.** Q's `0013_campus_posts.py` has
  `down_revision = "0010"`, and `0011` is now on main with the same parent. Main is
  single-head today; the moment `q/campus-posts` merges there will be **two heads** and
  `alembic upgrade head` fails outright. 0013 needs re-pointing to 0011 before that lands.

## Alpha is defined, not vibes

Alpha = Q's old chapter plus a couple more orgs, real students on real phones, small
enough to phone someone when it breaks. `board.html` carries the list: 7 shipped
foundations plus 13 named gates, each reading its status from its own card. Moving a card
moves the bar. **50% as of Aug 16.**

## Open PRs and branches

`q/compose` (PR #14, draft, c49) and `q/moderation-ui` (PR #17, c35) are Q's. Origin was
pruned to `main` plus branches with open PRs; two superseded branches were **tagged**
(`archive/q-website`, `archive/q-mock-identity-fixes`) before deletion, so nothing was
lost.

## Known-bad things that are shipped

- **Real users see fabricated people.** A brand-new account's Home shows `MOCK_MOMENTS` —
  Tyler, Maria, Priya, Devon, Sam, Ethan, Noah — in the "Your story" row. There is no
  backend concept for Moments, but it is in front of every user. Not yet carded.
- **Apple and Google sign-in are visual stubs** that skip authentication entirely and drop
  the user into a disconnected onboarding flow (c89). Apple guideline 4.8 makes Sign in
  with Apple mandatory once Google ships.
- **`/terms` and `/privacy` are not lawyer-reviewed** (c75), scoped to NC and UNCG on
  purpose. c76 made the moderation claim true; the liability sections are still
  placeholders.

## Environment

Two very different Macs, both written up in `CLAUDE.md`. Jose: Intel Mac, no Docker, local
PG14 on 5432, no `redis-server`. Q: Apple Silicon, Docker, postgres:16 on 5434. Run the
backend suite from **inside `backend/`** or every async test errors confusingly.

Creds, runbooks and live fixture ids: `INFRA-PRIVATE.html` at the repo root — gitignored,
never commit it.
