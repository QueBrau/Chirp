# HANDOFF — where everything actually is

_Last updated: Aug 16 2026, after the c94/c93/c58/c66/c95/c96 session. **Prod is current and verified.**
Migration 0011 is applied, revision `chirp-api-00013-rbg` is serving it, and a real
signed-in request was confirmed returning 200. Alpha readiness is **50%** — 10 of 20
named gates. Nothing has been deployed since; c94 is a mobile-only change._

**board.html is the source of truth for tasks** — 101 cards, 62 decisions. It now has an
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

**Answer c96's scope question first — it may reorder everything below.** See "The
campus problem" further down.

1. **c58 — live browser QA.** Jose's auth/orgs half is **walked** against real prod.
   Q's vertical (Yak content, campus feed, messaging, treasurer/secretary dashboards,
   CSV export) is still unproven, which is the only reason the card isn't Done.
2. **c74** — `chirp.shared@gmail.com` is published on the live site *and* owns GCP,
   Firebase and Stripe. Wants a forwarding alias; **blocked on c73's domain purchase**.
3. **c73** — marketing site to `about.<domain>`. **Blocked on buying a domain** (yours).
4. **c87 → c86 → c88** — transactional email, then `.edu` verification, then the gate.
   Strictly in that order. Previously "hold until after alpha" — **c96 challenges that**.

**PR #21 (`jose/c94-signin-navigation`) is open and holds c94, c93, c98, c95, c97, c99 and c100.**
Until it merges, `DEPLOY.md` on main **still carries the `--set-env-vars` line that wipes
`CORS_ORIGINS`** — do not redeploy off main's copy; deploy with no env flags at all.

Done this session, all verified live against prod, not by reading code:
**c94** (the sign-in bounce was a *race* — the app navigated on the Firebase credential
while SessionProvider was still resolving `/auth/me`, so the destination guard read a
stale `"signedOut"` and redirected straight back; reproduced on the pre-fix code first,
then re-verified: sign out and back in lands on `/feed` in ~1s) · **c93** (+ four more
paste-traps of the same family across three docs) · **c98** (Family Tree drew a blank box
in the state *every* chapter starts in) · **c95** (zero 401s now, on both sign-in and cold
reload) · **c66** (seven paste-traps fixed in the private runbook; inline password kept,
your call) · **c97 / c99 / c100** (the fake-content trio — the seven invented people are
cut from Home, the hardcoded "Sophomore · Business" bio is gone from every profile, and
"brothers" is gone from the two places it appeared; the rest of the app was already
inclusive).

**No invented content ships to users any more**, with one exception worth knowing:
Apple/Google sign-in are still visual stubs (c89).

Still open from the walk: **c101** (below).

Also open and unowned: **c84** (a chapter-less author cannot delete their own post) and
**c91** (no endpoint to resolve a report).

## The campus problem (c96) — decide this before alpha

**Nothing in the backend writes `users.campus_id`. Anywhere.** Every `campus_id=`
assignment in `backend/app` is a `Chapter`, `ContentReport` or `Yak`; every other
reference is a read, mostly a 403 comparison. Bootstrap says so in its own comment,
added by c85: *"The .edu redemption in c86 is the only writer of this column."*

So "hold c87 → c86 → c88 until after alpha" does not defer email. It means **every alpha
user permanently has no campus**, which darkens **Home's Campus tab and the entire Yak
tab** (both confirmed live on prod), and makes alpha gate **c71** — *"a campus-only
student can post"* — unreachable by construction.

The org half of the two-tier reasoning is genuinely fine: Orgs, events, members, dues and
invites all work perfectly with `campus_id` null. That's why this went unnoticed.

Either alpha ships with two of five tabs dark and c71 dropped from the gate list, or c86
moves ahead of alpha. **Do not "fix" it by writing a campus assignment into bootstrap** —
that is exactly the hole c85 closed, and see c101: `q/campus-posts` predates c85, still
has `campus_id=body.campus_id`, and will conflict on those very lines. Its side is the
*tempting* resolution, because it is the only code in either branch that makes campus
features work. Resolve in favour of main.

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
- **`q/campus-posts` carries TWO hazards, not one** (c101). Besides the heads problem
  below, it predates c85 and still sets `campus_id` from the request body — the merge will
  conflict on those lines and the branch's side looks like the fix. Take main's.
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

**PR #21 (`jose/c94-signin-navigation`, c94 + c93) is open and waiting on Jose to merge.**

Q merged two of theirs on Aug 16 without touching the board: **PR #17** (c35, the
moderation UI — an *alpha gate*) and **PR #20** (`q/nav-back-autohide`, real back button
plus an auto-hiding tab bar, which rewrote `(tabs)/_layout.tsx` around the auth guard;
the guard itself is unchanged). c35's card now says merged-pending-verification — when Q
verifies it live and moves it to Done, the alpha bar moves with it.

`q/compose` (PR #14, draft, c49) is Q's. Origin was
pruned to `main` plus branches with open PRs; two superseded branches were **tagged**
(`archive/q-website`, `archive/q-mock-identity-fixes`) before deletion, so nothing was
lost.

## Known-bad things that are shipped

- **Real users see fabricated people.** A brand-new account's Home shows `MOCK_MOMENTS` —
  Tyler, Maria, Priya, Devon, Sam, Ethan, Noah — in the "Your story" row. There is no
  backend concept for Moments, but it is in front of every user. **Now carded as c97.**
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
