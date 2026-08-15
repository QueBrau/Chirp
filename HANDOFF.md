# HANDOFF — where everything actually is

_Last updated: Aug 15 2026 (late). **THERE ARE NO OPEN PRs. The merge queue is closed.**
`gh pr list --state open` returns `[]`. The one thing standing between `main` and a
working prod is that **prod is two migrations behind** — see the next section. The
first-ever review of PRs #2/#3/#7/#8 ran on Aug 15 and found a CRITICAL dues
double-charge, fixed in PR #11 and now on `main` — see "The review"._

**board.html is the source of truth for tasks.** This file only answers the question the
board can't: which copy of Chirp you are looking at.

## THE MERGE QUEUE IS DONE

All three merged Aug 15 in the required order, CI green on every merge commit:

| PR | branch | brings | merge commit |
| --- | --- | --- | --- |
| #9 | `q/feed` | campus FYP, migration **0009** | `6334957` |
| #11 | `jose/auth-orgs` | six review fixes, migration **0010** | `9673ff6` |
| #10 | `q/eas-prep` | EAS dev-build blockers | `1aa19ba` |

`main` is at **`1aa19ba`**. The migration chain is linear and single-head, `0001 → 0010`.

Q ran the queue, including the conflict PR #11 hit once #9's newer tip (`bba26e0`)
landed. **The resolution kept both sides rather than taking one**, which is the part
worth remembering: `app-mobile/app/(tabs)/feed/index.tsx` has Q's `getCampus()` eyebrow
with the hardcoded mascot deleted, and `app-mobile/app/(tabs)/chapter/index.tsx` has
Jose's `listMembers()`/`findMember()` roster resolution with `mockUserById` gone. Taking
either side wholesale would have silently reverted a fix the other side had just made,
and the build would still have been green. Same trap as the roster-names overlap.

## WHAT IS ACTUALLY BLOCKING NOW: prod is two migrations behind

Prod is alive and healthy but still serving the pre-0009 surface. Route-probed Aug 15:

```
GET /auth/me                      401   (auth gate works)
GET /docs                         200
GET /campuses/{uuid}/feed         404   <- 0009's campus feed route does not exist live
```

Apply **0009 then 0010**, **then** redeploy — migrate-first is a hard rule here, see
below. Board card **c60** carries the full runbook, including a pre-flight check that
0010 specifically needs: it creates a UNIQUE index on the already-populated
`ledger_entries` table, so a duplicate `dues_payment` row would abort the migration
mid-flight. Expect zero rows; if not, stop and reconcile by hand rather than deleting
from an append-only ledger.

Verify the redeploy by **route probe, not by the deploy log**: `/campuses/{uuid}/feed`
must go 404 → 401, and `alembic current` must read `0010`.

## Current state

| Where | What's there | State |
| --- | --- | --- |
| `main` | Everything through **PR #10** (head `1aa19ba`): Yak on real API, dashboards + CSV export, Stripe dues + the reserve-before-charging fix, role-meta, events batch endpoint, sign-in UX, screens on real session data, `GET /campuses/{id}`, the campus FYP, EAS build blockers fixed, and NO `USE_MOCKS` layer. Migrations **0001-0010** | Canonical |
| Cloud Run (prod) | `chirp-api-593616178468.us-central1.run.app`, rev **00008-4sw** | `alembic_version` **0008** — **TWO BEHIND main**. `/campuses/{id}/feed` 404s live. Migrate + redeploy, board c60 |
| `q/feed`, `jose/auth-orgs`, `q/eas-prep` | all merged | Retired. New work cuts from `main` |
| `web/` | The public website (c56) | **Does not exist yet.** No `firebase.json` anywhere in the repo; Hosting is greenfield |
| CI | `.github/workflows/ci.yml`: backend pytest vs PG16 + mobile tsc on every push/PR | Green. No branch protection (deliberate, Aug 13) — CI is advisory |

Test counts: **139 backend tests green** on `jose/auth-orgs` before it merged, tsc clean;
CI green on all three merge commits. Nobody has re-run the suite against merged `main`
locally yet — CI has, which is what the green check on `1aa19ba` means.

Creds, runbooks, QA account, and live fixture ids: `INFRA-PRIVATE.html` at the repo root
(gitignored — get a copy from Jose, never commit it).

## The review (Aug 15) — read this before touching payments

PRs #2, #3, #7 and #8 had all merged **without any code review** (absorbed reviews exist
only for PRs #4/#5/#6), and `SECURITY-REVIEW.md` is dated Aug 13, predating the entire
Stripe money path. A five-lens review of `42d8e35..HEAD` raised 6 findings; **all 6
survived two independent adversarial verifiers**, and all 6 are fixed in PR #11.

The critical one: **a member could be charged twice for one dues cycle.** `already_paid`
only queried the ledger, which stays empty until a webhook settles; ACH sits in
`processing` for days so the cycle still looked unpaid; and the Stripe idempotency key
was scoped **per rail** (`dues:{cycle}:{user}:{rail}`), so retrying on card minted a
genuinely different PaymentIntent. Both settled, both appended, and the ledger is
append-only with no reversal path.

Fix, now the standing rule for anything touching money: **reserve before charging.** A
`dues_payment_intents` row is written BEFORE Stripe is called, and a partial unique index
(`uq_dues_intent_live`) holds one live reservation per (cycle, member) across BOTH rails.
`payment_failed`/`canceled` release it so genuine retries still work; same-rail retries
stay idempotent (an existing contract test caught a blunter first version that broke
them). The ledger gets `uq_ledger_dues_payment_once` as an independent backstop.

The authorization lens found **nothing** — org and campus scoping held everywhere.

Creds, runbooks, QA account, and live fixture ids: `INFRA-PRIVATE.html` at the repo root
(gitignored — get a copy from Jose, never commit it).

## Next human steps (in order)

_(Step 1, the 0008 migration + redeploy, is DONE as of Aug 14 — see the top of this file.
The rule it established, now also in INFRA-PRIVATE.html: **migrate first, then deploy.**
The Cloud Run deploy does not run migrations, and post-#3 code selects
`ledger_entries.stripe_payment_intent_id`, which does not exist until 0008 runs —
deploying first would have 500'd the treasurer ledger. Two gotchas worth keeping:
`cloud-sql-proxy` is NOT part of the gcloud SDK and has to be installed separately
(command is in the runbook), and the prod DB is human-only — agent sessions are
permission-blocked from it, though the redeploy itself is not.)_

1. **Apply migrations 0009 AND 0010, then redeploy** (board c60). The merges are done;
   this is all that is left of that chain. Migrate first, then deploy.
2. **Build the public website (board c56)** — DECIDED Aug 15, not started. It lives in
   **this repo** under `web/` as plain static HTML/CSS (never the Expo bundle: it must
   load fast and work with no auth), deployed to **Firebase Hosting** on the existing
   `chirps-prod` project. Pages: landing, features, how it works, about, **privacy
   policy**, **terms**, disclosure/contact — plus two functional pages the backend
   already depends on (`/stripe/connect/return`, `/stripe/connect/refresh`) and an
   invite bounce page that converts an https link into `chirp://join-chapter?code=...`.
   Design direction: borrow the STRUCTURE and polish bar of a great app landing page
   (split hero, dark canvas, real product imagery, one clear CTA, quiet footer) but NOT
   another product's look — `app-mobile/DESIGN.md` is binding and Chirp keeps its own
   identity. Do not ship a clone.
3. **Then set the Stripe TEST keys (c40)** — `STRIPE_SECRET_KEY`,
   `STRIPE_PUBLISHABLE_KEY`, `STRIPE_WEBHOOK_SECRET`, `APP_PUBLIC_BASE_URL` in Secret
   Manager + Cloud Run, then register the webhook at `/webhooks/stripe`.
4. **Rebuild the EAS dev build (c39)** — `expo-file-system`, `expo-sharing` and
   `@stripe/stripe-react-native` were all added after the current build was cut, so CSV
   export and the dues PaymentSheet can't be exercised on device until then. PR #10
   fixes the two blockers that would have failed this build.
5. **Then, and only then, a test-mode dues payment on both card and ACH.** Until that
   runs, treat the entire dues flow as unverified (see below).

### Why the website comes BEFORE the Stripe keys (board c57)

This ordering is not aesthetic. `backend/app/routers/payments.py:36-38` raises
`503 app_public_base_url_not_configured` when `APP_PUBLIC_BASE_URL` is unset, and Stripe
**rejects custom schemes**, so it can never be a `chirp://` link — Connect onboarding
needs a real https return/refresh URL that exists. Two more forcing functions: the App
Store will not accept a submission without a publicly reachable privacy-policy URL, and
`app-mobile/app/(auth)/sign-in.tsx:198` **already tells every user** "you agree to
Chirp's Terms of Service and acknowledge our Privacy Policy" — neither document exists
today, and the sentence is not even a link.

**Domain decided Aug 15: `https://chirps-prod.web.app`**, the free Firebase Hosting
domain on the existing project. Stripe accepts it, it costs nothing, and it unblocks
`APP_PUBLIC_BASE_URL` the day the site deploys — waiting on a domain purchase would have
parked the entire dues chain behind a shopping decision. A custom domain is its own card
and only becomes load-bearing when iOS universal links do; `app-mobile/app.json:14,26`
still declares the placeholder `applinks:chirp.example.com`, which is wrong either way.

One thing not to miss: **the invite bounce page fixes nothing on its own.**
`app-mobile/app/(tabs)/chapter/index.tsx:499` currently shares the raw
`withInviteCode("chirp://join-chapter", code)` string, so invites still go out as a
`chirp://` link that dies when pasted into a text message and nothing will ever point at
the new page. That one-line mobile change ships with c56.

## The de-mock sweep is NOT complete (board c59) — correction

PR #8 was reported as finishing the de-mock work, and c55 specifically claimed "the last
`mockUserById` call site in the app is gone." **Both overstated it.** Re-grepped on
`main` (`1aa19ba`), three screens still render fake identity against live data:

- `app-mobile/app/(tabs)/chapter/index.tsx:53,597,666,673` — `MOCK_CAMPUS.name`
  hardcoded into the Orgs hero subtitle, screen eyebrow and empty state, plus a literal
  `· SPARTANS`. Every user sees UNC Greensboro and UNCG's mascot.
- `app-mobile/app/(tabs)/yak/index.tsx:28,211` — same, with a stale `TODO` claiming no
  campus endpoint exists. `GET /campuses/{id}` shipped with c46.
- `app-mobile/app/(tabs)/chapter/alumni/index.tsx:9,64` — `mockUserById(job.posted_by)`
  resolves a **real** UUID through the mock table, so it never matches and every job
  renders "Posted by Alumni".

The fix pattern for the first two is already in the repo: `feed/index.tsx:86,145` calls
`getCampus(campusId)` and fails soft to an absent eyebrow. Four other `@/mocks` imports
on `main` are legitimate and are listed on c59 so the cleanup does not overshoot.

The failure mode is worth naming: **a claim about the whole repo was made from the scope
of one PR's diff.** When closing a card that asserts something repo-wide, re-grep the
repo, not the branch.

## Live browser QA has NEVER been done (board c58)

This is the largest unverified surface in the project. Everything shipped Aug 14–15 —
the session provider, Orgs on real memberships, role metadata, the sign-in UX change,
the batch endpoints, and the entire de-mock sweep — is proven by `pytest` and `tsc`
only. Nobody has driven the real app against real prod. The proven loop is
`cloud-sql-proxy` on 5433 + local uvicorn with `AUTH_MODE=firebase` + Metro on 8082 with
`EXPO_PUBLIC_API_URL` pointed at prod, using the three QA accounts in INFRA-PRIVATE.html.

## The Stripe migration number (board c41) — RESOLVED

PR #3 originally shipped the Stripe migration as `0006`. That number was Jose's events
migration, already applied to prod, with `0007` taken by `is_platform_admin`.

Left alone, prod (already past `0006`) would have had Alembic mark the Stripe migration
as already-applied and never run it — no `processed_stripe_events`, no unique partial
index on `ledger_entries.stripe_payment_intent_id`. Those are the webhook-replay guards,
so a Stripe retry would have appended a second dues payment to an append-only ledger.
Silent, and on the money path.

**Fixed**: renamed to `0008_stripe_dues.py`, `revision = "0008"`,
`down_revision = "0007"`. Verified the chain is linear with a single head
(`0001` → … → `0008`), not merely that the file parses.

**Rule that came out of this (now in CLAUDE.md): claim your migration number on the board
before you write the file. Next free number: `0009`.**

## The roster-names overlap (came out of the PR #6 catch-up)

PR #6 and the dashboards work solved the same problem independently — "the roster is a
list of bare UUIDs, and there is no `GET /users/{id}` to resolve names." Git did **not**
flag most of it as a text conflict, so it needed catching by hand.

Resolution: kept main's `MemberOut` (real INNER JOIN, non-null `display_name`, plus
`avatar_url`) and deleted the `MembershipOut.display_name` field the dashboards branch
had added. `GET /me/memberships` still returns the plain `MembershipOut`; only
`GET /chapters/{id}/members` returns `MemberOut`. If you find yourself re-adding a
nullable `display_name` to `MembershipOut`, that's the regression.

## Verification status

- Both branches were green at merge: `family-tree` 92 backend tests, `q/social-msg` 116
  (the extra ones are the Stripe suite), each run fresh against postgres:16 through the
  full 0001–0008 chain. tsc clean on both. CI agreed.
- PR #7 adds 6 tests (role-meta anti-drift asserts against permissions.py itself;
  events-with-rsvps grouping + org-scoping): **122 green** on Jose's local PG14, tsc
  clean. No migration — `0009` stays free.
- Stripe is code-complete but **has never talked to real Stripe**. Nothing in the dues
  flow is proven until step 2 above is done and a test-mode payment runs on card AND ACH.
  Treat it as unverified — merged is not the same as working.
- Yak, the dashboards, and CSV export are exercised only against mocks and the test
  suite; they have not been driven on a device (the iOS Simulator was broken on Q's Mac
  this session — `launchd_sim` failing to bind a session after an `xcode-select` switch;
  a reboot is the known fix, not yet confirmed).

## Environment

Two very different Macs, and the differences bite. Both are written up in **CLAUDE.md**:
Jose's Intel Mac (no Docker, local PG14) and Q's Apple Silicon Mac (Docker, postgres:16
on port 5434, venv on Python 3.12 — note `backend/.venv` itself is still Python 3.9 and
unusable against `pyproject.toml`'s `>=3.11`, so every test run builds a throwaway venv
from Homebrew `python@3.12`; worth fixing, it's pure friction). Prod credentials and the
redeploy runbook live in INFRA-PRIVATE.html at the repo root — gitignored, never commit it.

Run the backend suite from **inside `backend/`** — `pyproject.toml`'s
`asyncio_mode = "auto"` isn't picked up from the repo root, and every async test errors
out confusingly (76 errors, all "async fixture" complaints) if you forget.

Every backend change that should go live needs a Cloud Run redeploy. It is a human step.
