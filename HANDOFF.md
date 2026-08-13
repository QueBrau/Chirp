# HANDOFF — where everything actually is

_Last updated: Aug 13 2026, late night (post PR #4 merge)._

**board.html is the source of truth for tasks.** This file only answers the question the
board can't: which copy of Chirp you are looking at.

## Current state: main, prod, and CI all agree

| Where | What's there | State |
| --- | --- | --- |
| `main` | Everything through **PR #4**: Firebase live, app off mocks + auth guard, events backend, platform-admin chapter gating, review fixes, CI workflow, migrations **0001-0007** | Canonical; prod matches it |
| Cloud Run (prod) | Live backend at `chirp-api-593616178468.us-central1.run.app` (rev 00004) | `alembic_version` at **0007** = main's head; firebase-init-at-boot hardening deployed |
| `jose/auth-orgs` | Synced to main, kept for Jose's future work | Merged via PR #4 |
| `q/social-msg` | Stripe Connect dues on top of merged main | **PR #3**, DRAFT — one blocker left, see below |
| CI | `.github/workflows/ci.yml`: backend pytest vs PG16 + mobile tsc on every push/PR | First run green. No branch protection (deliberate, Aug 13) — CI is advisory |

Creds, runbooks, QA account, and live fixture ids: `INFRA-PRIVATE.html` at the repo root
(gitignored — get a copy from Jose, never commit it).

## Blocked right now: the Stripe migration number (board c41)

PR #3 ships the Stripe migration as `0006`. That number is taken by Jose's events
migration, which is already applied to prod, and `0007` is taken by `is_platform_admin`.

If PR #3 merged as-is, prod is already past `0006`, so Alembic would mark the Stripe
migration applied and never run it — no `processed_stripe_events`, no unique partial
index on `ledger_entries.stripe_payment_intent_id`. Those are the webhook-replay guards,
so a Stripe retry would append a second dues payment to an append-only ledger. Silent,
and on the money path.

PR #3 is a draft so it can't be merged by accident. The fix, once `jose/auth-orgs` is on
main: rename to `0008_stripe_dues.py`, set `revision = "0008"` / `down_revision = "0007"`,
re-run the suite, mark ready. The collision is isolated to that one file.

**Rule that came out of this (now in CLAUDE.md): claim your migration number on the board
before you write the file.**

## Verification status

- `q/social-msg`: **107 backend tests green** against postgres:16, which matches prod's
  PG16. tsc clean, web bundle exports.
- Stripe is code-complete but **has never talked to real Stripe**. Nothing in the dues
  flow is proven until the keys are set (c40) and a test-mode payment runs on both card
  and ACH. Treat it as unverified.
- The mobile half of Stripe additionally can't be exercised until the EAS dev build is
  rebuilt (c39) — `expo-file-system`, `expo-sharing` and `@stripe/stripe-react-native`
  were all added after the current build was cut.

## Environment

Two very different Macs, and the differences bite. Both are written up in **CLAUDE.md**:
Jose's Intel Mac (no Docker, local PG14) and Q's Apple Silicon Mac (Docker, postgres:16
on port 5434, venv on Python 3.12). Prod credentials and the redeploy runbook live in
INFRA-PRIVATE.html at the repo root — gitignored, never commit it.

Every backend change that should go live needs a Cloud Run redeploy. It is a human step.
