# HANDOFF — where everything actually is

_Last updated: Aug 14 2026, night. **PR #2 is MERGED to main.** PR #3 (Stripe dues)
is merging right behind it. Everything through PR #6 (session provider + /auth/me,
Orgs on real memberships + invite UI + roster names) was already on main and
live-verified at prod rev chirp-api-00006-smv._

**board.html is the source of truth for tasks.** This file only answers the question the
board can't: which copy of Chirp you are looking at.

## Current state

| Where | What's there | State |
| --- | --- | --- |
| `main` | Everything through **PR #6** + **PR #2** (Yak on real API, treasurer/secretary dashboards, CSV export), and PR #3 (Stripe dues, migration **0008**) landing now | Canonical |
| Cloud Run (prod) | Live backend at `chirp-api-593616178468.us-central1.run.app` (rev 00006-smv) | `alembic_version` at **0007** — **BEHIND main once PR #3 lands.** Next redeploy runs migration 0008 |
| `jose/auth-orgs` | Synced to main, kept for Jose's future work | Merged via PR #6 |
| `family-tree` | Merged via PR #2 | Done |
| `q/social-msg` | Merged via PR #3 | Done |
| CI | `.github/workflows/ci.yml`: backend pytest vs PG16 + mobile tsc on every push/PR | Green on both PRs at merge time. No branch protection (deliberate, Aug 13) — CI is advisory |

Creds, runbooks, QA account, and live fixture ids: `INFRA-PRIVATE.html` at the repo root
(gitignored — get a copy from Jose, never commit it).

## Next human steps (in order)

1. **Cloud Run redeploy.** Prod is at `alembic_version` 0007; main now carries **0008**
   (Stripe: `chapter_stripe_customers`, `processed_stripe_events`, and the unique partial
   index on `ledger_entries.stripe_payment_intent_id`). Nothing Stripe works until this
   runs, and until it does prod is running older code than main.
2. **Set the Stripe keys (c40)** — `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`,
   `STRIPE_WEBHOOK_SECRET`, `APP_PUBLIC_BASE_URL` in Secret Manager + Cloud Run, then
   register the webhook endpoint at `/webhooks/stripe`. `APP_PUBLIC_BASE_URL` must be
   https — Stripe rejects custom schemes, so Connect onboarding cannot return to a
   `chirp://` deep link; it needs a web page that bounces back into the app.
3. **Rebuild the EAS dev build (c39)** — `expo-file-system`, `expo-sharing` and
   `@stripe/stripe-react-native` were all added after the current build was cut, so CSV
   export and the dues PaymentSheet can't be exercised on device until then.
4. **Then, and only then, a test-mode dues payment on both card and ACH.** Until that
   runs, treat the entire dues flow as unverified (see below).

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
