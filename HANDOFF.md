# HANDOFF — where everything actually is

_Last updated: Aug 14 2026, night. PR #5 (session provider + /auth/me) and PR #6
(Orgs on real memberships + invite UI + roster names) are both MERGED; prod
redeployed to rev chirp-api-00006-smv and live-verified. **Both dev branches are now
caught up to main through PR #6**: PR #2's 8-file conflict is resolved and pushed,
and PR #3's Stripe migration is renumbered to 0008. Neither is blocked anymore._

**board.html is the source of truth for tasks.** This file only answers the question the
board can't: which copy of Chirp you are looking at.

## Current state: main, prod, and CI all agree

| Where | What's there | State |
| --- | --- | --- |
| `main` | Everything through **PR #6**: Firebase live, session provider + /auth/me, Orgs on real memberships + invite UI + roster names, events backend, platform-admin chapter gating, CI workflow, migrations **0001-0007** | Canonical; prod matches it |
| Cloud Run (prod) | Live backend at `chirp-api-593616178468.us-central1.run.app` (rev 00006-smv) | `alembic_version` at **0007** = main's head (PR #6 shipped no migration) |
| `jose/auth-orgs` | Synced to main, kept for Jose's future work | Merged via PR #6 |
| `family-tree` | Yak on real API + treasurer/secretary dashboards + CSV export | **PR #2**, merged with main through PR #6, 92 tests green, **pushed** |
| `q/social-msg` | Everything in PR #2, plus Stripe Connect dues (migration **0008**) | **PR #3**, merged with main through PR #6 — ready to come off draft |
| CI | `.github/workflows/ci.yml`: backend pytest vs PG16 + mobile tsc on every push/PR | First run green. No branch protection (deliberate, Aug 13) — CI is advisory |

Creds, runbooks, QA account, and live fixture ids: `INFRA-PRIVATE.html` at the repo root
(gitignored — get a copy from Jose, never commit it).

## The Stripe migration number (board c41) — RESOLVED

PR #3 shipped the Stripe migration as `0006`. That number was Jose's events migration,
already applied to prod, with `0007` taken by `is_platform_admin`.

Left alone, prod (already past `0006`) would have had Alembic mark the Stripe migration
as already-applied and never run it — no `processed_stripe_events`, no unique partial
index on `ledger_entries.stripe_payment_intent_id`. Those are the webhook-replay guards,
so a Stripe retry would have appended a second dues payment to an append-only ledger.
Silent, and on the money path.

**Fixed**: renamed to `0008_stripe_dues.py`, `revision = "0008"`,
`down_revision = "0007"`. Verified the resulting chain is linear with a single head
(`0001` → … → `0008`), not merely that the file parses.

**Rule that came out of this (now in CLAUDE.md): claim your migration number on the board
before you write the file.**

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

- `family-tree`: **92 backend tests green**, tsc clean, post-merge with main through PR #6.
- `q/social-msg`: **116 backend tests green** (the extra ones are the Stripe suite), tsc
  clean, run fresh through the full 0001–0008 migration chain.
- Stripe is code-complete but **has never talked to real Stripe**. Nothing in the dues
  flow is proven until the keys are set (c40) and a test-mode payment runs on both card
  and ACH. Treat it as unverified.
- The mobile half of Stripe additionally can't be exercised until the EAS dev build is
  rebuilt (c39) — `expo-file-system`, `expo-sharing` and `@stripe/stripe-react-native`
  were all added after the current build was cut.

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
