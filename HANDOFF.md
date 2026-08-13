# HANDOFF — where everything actually is

_Last updated: Aug 13 2026, evening._

**board.html is the source of truth for tasks.** This file only answers the question the
board can't: which of the three copies of Chirp you are looking at, and why prod does
not match main.

## The one thing to internalize: work is split three ways, and prod is ahead of main

| Where | What's there | State |
| --- | --- | --- |
| `main` | Redesign, Firebase auth scaffolding, lineage tree + alumni (PR #1), migrations **0001-0005** | Behind both dev branches |
| `jose/auth-orgs` | Firebase live config, app off mocks, events backend (**0006**), platform-admin chapter gating (**0007**) | Not merged; **0006/0007 already applied to PROD** |
| `family-tree` | Yak on real API, treasurer/secretary dashboards + CSV export | **PR #2**, ready to merge |
| `q/social-msg` | Everything in PR #2, plus Stripe Connect dues | **PR #3**, DRAFT — blocked, see below |
| Cloud Run (prod) | Live backend at `chirp-api-593616178468.us-central1.run.app` | `alembic_version` at **0006**, 0007 landing |

So prod's database schema is ahead of `main`'s migration folder. That is not a mistake —
Jose applies migrations from his branch and redeploys before the PR merges. It does mean
you cannot reason about prod by reading `main`.

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
